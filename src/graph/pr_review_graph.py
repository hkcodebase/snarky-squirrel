"""
PR Review LangGraph — multi-agent graph with shared memory via DynamoDB.

Graph topology:
  START → supervisor → [code_quality | security | pr_reviewer] → supervisor → summary → END

Shared state is persisted in DynamoDB so agents can read each other's findings
and the graph can be resumed on Lambda timeout / retries.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Sequence, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.agents.code_quality_agent import CodeQualityAgent
from src.agents.pr_reviewer_agent import PRReviewerAgent
from src.agents.security_agent import SecurityAgent
from src.agents.summary_agent import SummaryAgent
from src.tools.dynamo_memory import DynamoMemoryStore

# ─────────────────────────── shared state ────────────────────────────────────


class PRReviewState(TypedDict):
    """Shared state threaded through every node in the graph."""

    messages: Annotated[Sequence[BaseMessage], add_messages]

    # PR metadata injected at the start
    pr_metadata: dict[str, Any]  # repo, number, sha, diff_url, title, author
    diff_content: str  # raw unified diff
    file_list: list[str]  # files changed

    # Per-agent findings written by each agent, read by summary
    code_quality_findings: list[dict]
    security_findings: list[dict]
    pr_review_findings: list[dict]

    # Routing control
    next_agent: str  # supervisor decision
    completed_agents: list[str]

    # Final output
    summary_report: str
    overall_score: float  # 0–10
    should_block: bool  # true if critical security issue found
    score_breakdown: dict  # per-severity deduction breakdown


# ─────────────────────────── LLM factory ─────────────────────────────────────


def get_llm():
    """
    Return an LLM instance based on LLM_PROVIDER env var.

    Supported providers:
      ollama        — Ollama running locally (default). Set OLLAMA_BASE_URL.
      docker-model  — Docker Desktop Model Runner (OpenAI-compatible endpoint).
                      Set DOCKER_MODEL_ENDPOINT (default: localhost:12434).
      bedrock       — AWS Bedrock (production). Requires langchain-aws installed
                      and real AWS credentials with Bedrock access.
    """
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    model = os.environ.get("LLM_MODEL", "gemma4:4b")

    if provider == "bedrock":
        from langchain_aws import ChatBedrock  # noqa: PLC0415

        model_id = os.environ.get(
            "BEDROCK_MODEL_ID",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        )
        return ChatBedrock(
            model_id=model_id,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            provider="anthropic",
            model_kwargs={"max_tokens": 4096, "temperature": 0.1},
        )

    if provider == "docker-model":
        # Docker Desktop Model Runner exposes an OpenAI-compatible API.
        # Run `docker model pull ai/gemma4` first (Docker Desktop 4.40+).
        from langchain_openai import ChatOpenAI  # noqa: PLC0415

        base_url = os.environ.get(
            "DOCKER_MODEL_ENDPOINT",
            "http://localhost:12434/engines/llama.cpp/v1",
        )
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key="docker-model",  # required by the client; value is ignored
            temperature=0.1,
            max_tokens=4096,
        )

    # Default: local Ollama instance (started via docker-compose)
    from langchain_ollama import ChatOllama  # noqa: PLC0415

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    return ChatOllama(
        model=model,
        base_url=base_url,
        temperature=0.1,
        num_predict=4096,
    )


# ─────────────────────────── supervisor node ─────────────────────────────────

SUPERVISOR_SYSTEM = """You are an orchestrating supervisor for a PR review system.
Your job is to decide which specialist agent should run next based on what has already completed.
Respond ONLY with a JSON object: {"next": "<agent_name>"}
Agent names: code_quality | security | pr_reviewer | summary | FINISH
Rules:
- Always run code_quality, security, and pr_reviewer before summary.
- Run summary only when all three specialist agents are in completed_agents.
- Return FINISH only after summary has run.
"""


def supervisor_node(state: PRReviewState) -> dict:
    llm = get_llm()
    completed = state.get("completed_agents", [])
    pending = [
        a for a in ["code_quality", "security", "pr_reviewer"] if a not in completed
    ]

    if not pending and "summary" not in completed:
        return {"next_agent": "summary"}
    if not pending and "summary" in completed:
        return {"next_agent": "FINISH"}

    # Ask the LLM to pick the next agent from the pending list
    prompt = (
        f"Completed agents: {completed}\n"
        f"Pending agents: {pending}\n"
        f"PR title: {state['pr_metadata'].get('title', '')}\n"
        "Which agent should run next?"
    )
    messages = [SystemMessage(content=SUPERVISOR_SYSTEM), HumanMessage(content=prompt)]
    response = llm.invoke(messages)
    try:
        decision = json.loads(response.content)
        next_agent = decision.get("next", pending[0])
    except (json.JSONDecodeError, AttributeError):
        next_agent = pending[0]

    # Validate the choice is actually pending
    if next_agent not in pending and next_agent != "summary":
        next_agent = pending[0]

    return {"next_agent": next_agent}


def route_after_supervisor(state: PRReviewState) -> str:
    return state.get("next_agent", "FINISH")


# ─────────────────────────── build the graph ─────────────────────────────────


def build_pr_review_graph(use_dynamo_checkpointer: bool = True):
    """Build and compile the LangGraph StateGraph."""

    # Checkpointer: DynamoDB for production, in-memory for local dev
    if use_dynamo_checkpointer and os.environ.get("DYNAMODB_TABLE"):
        from src.tools.dynamo_memory import DynamoCheckpointer

        checkpointer = DynamoCheckpointer(
            table_name=os.environ["DYNAMODB_TABLE"],
            region=os.environ.get("AWS_REGION", "us-east-1"),
        )
    else:
        checkpointer = MemorySaver()

    llm = get_llm()
    memory_store = DynamoMemoryStore()

    # Instantiate agents (each is a callable node)
    code_quality = CodeQualityAgent(llm, memory_store)
    security = SecurityAgent(llm, memory_store)
    pr_reviewer = PRReviewerAgent(llm, memory_store)
    summary = SummaryAgent(llm, memory_store)

    graph = StateGraph(PRReviewState)

    # Add nodes
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("code_quality", code_quality.run)
    graph.add_node("security", security.run)
    graph.add_node("pr_reviewer", pr_reviewer.run)
    graph.add_node("summary", summary.run)

    # Edges
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "code_quality": "code_quality",
            "security": "security",
            "pr_reviewer": "pr_reviewer",
            "summary": "summary",
            "FINISH": END,
        },
    )
    # Each agent returns to supervisor after completing
    for agent in ["code_quality", "security", "pr_reviewer", "summary"]:
        graph.add_edge(agent, "supervisor")

    return graph.compile(checkpointer=checkpointer)


# ─────────────────────────── entrypoint ──────────────────────────────────────


def run_pr_review(pr_metadata: dict, diff_content: str, file_list: list[str]) -> dict:
    """Run the full PR review graph and return the final state."""
    started_at = datetime.now(tz=timezone.utc).isoformat()
    t0 = time.monotonic()

    # Unique thread_id per run so the same PR can be reviewed multiple times.
    repo_safe = pr_metadata["repo"].replace("/", "-")
    run_id = uuid.uuid4().hex[:8]
    thread_id = f"pr-{repo_safe}-{pr_metadata['number']}-{run_id}"

    # Inject into metadata so every agent can read it from state
    pr_metadata = {**pr_metadata, "thread_id": thread_id}

    app = build_pr_review_graph()

    initial_state: PRReviewState = {
        "messages": [
            HumanMessage(content=f"Review PR: {pr_metadata.get('title', '')}")
        ],
        "pr_metadata": pr_metadata,
        "diff_content": diff_content,
        "file_list": file_list,
        "code_quality_findings": [],
        "security_findings": [],
        "pr_review_findings": [],
        "next_agent": "",
        "completed_agents": [],
        "summary_report": "",
        "overall_score": 0.0,
        "should_block": False,
        "score_breakdown": {},
    }

    config = {"configurable": {"thread_id": thread_id}}
    final_state = app.invoke(initial_state, config=config)

    # Write run-level lineage record
    memory = DynamoMemoryStore()
    memory.put(thread_id, "lineage_run", json.dumps({
        "pr_title": pr_metadata.get("title", ""),
        "pr_repo": pr_metadata.get("repo", ""),
        "pr_number": pr_metadata.get("number"),
        "started_at": started_at,
        "completed_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_duration_ms": int((time.monotonic() - t0) * 1000),
        "agent_order": final_state.get("completed_agents", []),
        "final_score": final_state.get("overall_score", 0),
        "should_block": final_state.get("should_block", False),
        "score_breakdown": final_state.get("score_breakdown", {}),
        "diff_chars": len(diff_content),
        "files_count": len(file_list),
    }))

    return final_state
