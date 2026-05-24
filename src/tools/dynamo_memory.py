"""
DynamoDB-backed shared memory for inter-agent communication.

Table schema (single table):
  PK (string): thread_id
  SK (string): key
  value (string): JSON or plain string value
  ttl (number): Unix TTL (72 hours)

Also provides a LangGraph-compatible checkpointer backed by DynamoDB.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from langgraph.checkpoint.base import ( BaseCheckpointSaver, CheckpointTuple )

_TTL_SECONDS = 72 * 3600  # 72 hours


class DynamoMemoryStore:
    """Simple key-value shared memory backed by DynamoDB."""

    def __init__(self, table_name: str | None = None, region: str | None = None):
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_TABLE", "pr-review-local-memory"
        )
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._client: Any = None

    @property
    def client(self):
        if self._client is None:
            # Empty string or unset → use real AWS DynamoDB (no endpoint override).
            # Any non-empty value (e.g. "http://localhost:8000") → local DynamoDB.
            raw_endpoint = os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8000")
            endpoint_url: str | None = raw_endpoint if raw_endpoint else None

            kwargs: dict[str, Any] = {"region_name": self.region}
            if endpoint_url:
                # Local DynamoDB — also inject dummy credentials so boto3 doesn't
                # try (and fail) to look up real AWS credentials.
                kwargs["endpoint_url"] = endpoint_url
                kwargs["aws_access_key_id"] = os.environ.get("AWS_ACCESS_KEY_ID", "test")
                kwargs["aws_secret_access_key"] = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
            # Real AWS: boto3 resolves credentials from env vars / profile / instance role.
            self._client = boto3.client("dynamodb", **kwargs)
        return self._client

    def put(self, thread_id: str, key: str, value: str) -> None:
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "PK": {"S": thread_id},
                    "SK": {"S": key},
                    "value": {"S": value},
                    "ttl": {"N": str(int(time.time()) + _TTL_SECONDS)},
                },
            )
        except ClientError as e:
            print(f"[DynamoMemoryStore] put error: {e}")

    def get(self, thread_id: str, key: str) -> str | None:
        try:
            resp = self.client.get_item(
                TableName=self.table_name,
                Key={"PK": {"S": thread_id}, "SK": {"S": key}},
            )
            item = resp.get("Item", {})
            return item.get("value", {}).get("S")
        except ClientError as e:
            print(f"[DynamoMemoryStore] get error: {e}")
            return None

    def delete(self, thread_id: str, key: str) -> None:
        try:
            self.client.delete_item(
                TableName=self.table_name,
                Key={"PK": {"S": thread_id}, "SK": {"S": key}},
            )
        except ClientError as e:
            print(f"[DynamoMemoryStore] delete error: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# LangGraph DynamoDB Checkpointer
# ──────────────────────────────────────────────────────────────────────────────


import json
from typing import Any

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple
)

class DynamoCheckpointer(BaseCheckpointSaver):

    def __init__(self, table_name=None, region=None):
        self.store = DynamoMemoryStore(
            table_name=table_name,
            region=region,
        )

    def get_tuple(self, config):

        thread_id = config["configurable"]["thread_id"]

        raw = self.store.get(
            thread_id,
            "__checkpoint__",
        )

        if not raw:
            return None

        payload = json.loads(raw)

        return CheckpointTuple(
            config=config,
            checkpoint=payload["checkpoint"],
            metadata=payload["metadata"],
            parent_config=None,
        )

    async def aget_tuple(self, config):
        return self.get_tuple(config)

    def put(
            self,
            config,
            checkpoint,
            metadata,
            new_versions,
    ):

        thread_id = config["configurable"]["thread_id"]

        payload = {
            "checkpoint": checkpoint,
            "metadata": metadata,
        }

        self.store.put(
            thread_id,
            "__checkpoint__",
            json.dumps(payload, default=str),
        )

        return config

    async def aput(
            self,
            config,
            checkpoint,
            metadata,
            new_versions,
    ):
        return self.put(
            config,
            checkpoint,
            metadata,
            new_versions,
        )

    def put_writes(
            self,
            config,
            writes,
            task_id,
            task_path="",
    ):
        return

    async def aput_writes(
            self,
            config,
            writes,
            task_id,
            task_path="",
    ):
        return

    def get_next_version(
            self,
            current,
            channel=None,
    ):
        if current is None:
            return 1
        return int(current) + 1

    def list(self, *args, **kwargs):
        return iter([])