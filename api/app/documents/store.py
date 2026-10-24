"""Document object stores (AA-937).

The app encrypts every document with the tenant's ``docs`` DEK **before** it reaches a store (client-side encryption,
design v3 §4.1 Fix A7), so a store only ever holds ciphertext. The S3 store adds the bucket's default encryption
(SSE-KMS with the CRM data key, AA-CIS-Infra ``crm_storage.tf``) as the second layer: it sends no SSE header, so the
bucket default applies (the bucket policy refuses another key or algorithm).

Configuration (``app.settings``): ``DOCUMENTS_BUCKET`` (the ``aa-crm-<env>-documents-*`` bucket, set by Terraform).
Without it, outside test / local, document routes answer 503 ``documents_not_configured``; test / local use the
in-memory store.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Protocol

from app.errors import Unavailable


class DocumentStore(Protocol):
    async def put(self, key: str, body: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class MemoryDocumentStore:
    """In-process store for tests and local runs (never configured on dev / stg / prod)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, body: bytes) -> None:
        self.objects[key] = bytes(body)

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise Unavailable("document_missing") from None

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class S3DocumentStore:
    """The documents bucket. boto3 is blocking: each call runs in a worker thread."""

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

    async def put(self, key: str, body: bytes) -> None:
        await asyncio.to_thread(
            self._s3().put_object, Bucket=self.bucket, Key=key, Body=body, ContentType="application/octet-stream"
        )

    async def get(self, key: str) -> bytes:
        def read() -> bytes:
            return self._s3().get_object(Bucket=self.bucket, Key=key)["Body"].read()

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._s3().delete_object, Bucket=self.bucket, Key=key)


def require_store(store: DocumentStore | None) -> DocumentStore:
    if store is None:
        raise Unavailable("documents_not_configured", "the documents bucket is not configured")
    return store
