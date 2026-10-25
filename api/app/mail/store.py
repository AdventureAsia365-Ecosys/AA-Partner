"""The ingest bucket (AA-945; AA-CIS-Infra ``crm_mail.tf``): raw forwarded mail under ``raw/`` (written by AA's SES
receipt rule, SSE-KMS under the CRM ingest key, expired after 1 day) and history-import uploads under ``imports/``.

The task role may list both prefixes, read and delete in both, and write ``imports/`` only. No SSE header is sent: the
bucket default applies (the bucket policy refuses another key or algorithm). boto3 is blocking: each call runs in a
worker thread.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.errors import Unavailable

RAW_PREFIX = "raw/"
IMPORTS_PREFIX = "imports/"
# SES writes this object once when the receipt rule is created (a setup notice, not a mail).
SES_SETUP_KEY_SUFFIX = "AMAZON_SES_SETUP_NOTIFICATION"


@dataclass(frozen=True)
class StoredObject:
    key: str
    last_modified: datetime


class IngestStore(Protocol):
    async def list_keys(self, prefix: str, limit: int) -> list[str]: ...

    async def list_objects(self, prefix: str, limit: int, start_after: str | None = None) -> list[StoredObject]:
        """Up to ``limit`` objects in key order, after ``start_after`` (to page past objects that keep failing)."""
        ...

    async def get(self, key: str) -> bytes: ...

    async def put(self, key: str, body: bytes) -> None: ...

    async def delete(self, key: str) -> None: ...


class MemoryIngestStore:
    """In-process store for tests and local runs (never configured on dev / stg / prod)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, datetime] = {}  # a key missing here counts as written now

    async def list_keys(self, prefix: str, limit: int) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))[:limit]

    async def list_objects(self, prefix: str, limit: int, start_after: str | None = None) -> list[StoredObject]:
        keys = sorted(k for k in self.objects if k.startswith(prefix) and (start_after is None or k > start_after))
        now = datetime.now(UTC)
        return [StoredObject(k, self.modified.get(k, now)) for k in keys[:limit]]

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise Unavailable("mail_object_missing") from None

    async def put(self, key: str, body: bytes) -> None:
        self.objects[key] = bytes(body)
        self.modified[key] = datetime.now(UTC)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.modified.pop(key, None)


class S3IngestStore:
    def __init__(self, bucket: str, region: str | None = None, client: Any = None) -> None:
        self.bucket = bucket
        self._region = region
        self._client = client
        self._lock = threading.Lock()

    def _s3(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3

                self._client = boto3.client("s3", region_name=self._region)
            return self._client

    async def list_keys(self, prefix: str, limit: int) -> list[str]:
        def call() -> list[str]:
            resp = self._s3().list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=limit)
            return [str(o["Key"]) for o in resp.get("Contents", []) or []]

        return await asyncio.to_thread(call)

    async def list_objects(self, prefix: str, limit: int, start_after: str | None = None) -> list[StoredObject]:
        def call() -> list[StoredObject]:
            extra = {"StartAfter": start_after} if start_after else {}
            resp = self._s3().list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=limit, **extra)
            return [StoredObject(str(o["Key"]), o["LastModified"]) for o in resp.get("Contents", []) or []]

        return await asyncio.to_thread(call)

    async def get(self, key: str) -> bytes:
        def call() -> bytes:
            return bytes(self._s3().get_object(Bucket=self.bucket, Key=key)["Body"].read())

        return await asyncio.to_thread(call)

    async def put(self, key: str, body: bytes) -> None:
        await asyncio.to_thread(lambda: self._s3().put_object(Bucket=self.bucket, Key=key, Body=body))

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(lambda: self._s3().delete_object(Bucket=self.bucket, Key=key))


def require_ingest_store(store: IngestStore | None) -> IngestStore:
    if store is None:
        raise Unavailable("mail_ingest_not_configured", "the mail ingest bucket is not configured")
    return store
