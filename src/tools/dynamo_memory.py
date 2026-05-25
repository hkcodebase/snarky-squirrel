"""
DynamoDB-backed shared memory for inter-agent communication.

Table schema (single table):
  PK (string):         thread_id
  SK (string):         key / record type  e.g. "lineage_run"
  value (string):      JSON or plain string value
  ttl (number):        Unix TTL (72 hours)
  created_at (string): ISO-8601 UTC timestamp — GSI range key for time ordering

GSI "SK-index":
  hash_key  = SK          — query by record type without a full table scan
  range_key = created_at  — results in reverse-chronological order

Also provides a LangGraph-compatible checkpointer backed by DynamoDB.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from langgraph.checkpoint.base import ( BaseCheckpointSaver, CheckpointTuple )

_TTL_SECONDS = 72 * 3600  # 72 hours
_GSI_NAME    = "SK-index"


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

    def put(self, thread_id: str, key: str, value: str, ttl_seconds: int = _TTL_SECONDS) -> None:
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "PK":         {"S": thread_id},
                    "SK":         {"S": key},
                    "value":      {"S": value},
                    "ttl":        {"N": str(int(time.time()) + ttl_seconds)},
                    "created_at": {"S": datetime.now(timezone.utc).isoformat()},
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

    def query_by_type(
        self,
        record_type: str,
        limit: int = 20,
        cursor: dict | None = None,
        newest_first: bool = True,
    ) -> tuple[list[dict], dict | None]:
        """Query items by SK (record type) via the SK-index GSI.

        Uses a targeted Query instead of a full-table Scan — reads only items
        matching record_type, O(result_set) not O(table_size).

        Returns:
            (items, next_cursor) — next_cursor is None when no further pages exist.
        """
        try:
            kwargs: dict[str, Any] = {
                "TableName":                 self.table_name,
                "IndexName":                 _GSI_NAME,
                "KeyConditionExpression":    "SK = :t",
                "ExpressionAttributeValues": {":t": {"S": record_type}},
                "Limit":                     limit,
                "ScanIndexForward":          not newest_first,  # False = descending
            }
            if cursor:
                kwargs["ExclusiveStartKey"] = cursor
            resp = self.client.query(**kwargs)
            return resp.get("Items", []), resp.get("LastEvaluatedKey")
        except ClientError as e:
            print(f"[DynamoMemoryStore] query_by_type error: {e}")
            return [], None

    def query_all_by_type(self, record_type: str) -> list[dict]:
        """Fetch every item of a given record type by paginating through the GSI.

        Used for aggregate computations (metrics) where all pages are needed
        but query is still far cheaper than scan (reads only matching items).
        """
        items: list[dict] = []
        cursor = None
        while True:
            page, cursor = self.query_by_type(
                record_type, limit=100, cursor=cursor, newest_first=False
            )
            items.extend(page)
            if cursor is None:
                break
        return items


# ──────────────────────────────────────────────────────────────────────────────
# LangGraph DynamoDB Checkpointer
# ──────────────────────────────────────────────────────────────────────────────


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
