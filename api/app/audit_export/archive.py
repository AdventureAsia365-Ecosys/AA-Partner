"""The audit archive: the two Object Lock buckets (design v3 §4.1, §4.9; AA-CIS-Infra ``crm_storage.tf``).

- ``compliance`` = ``aa-crm-<env>-audit-c-*``, default retention 12 months (GOVERNANCE mode on Dev, COMPLIANCE only at
  Go-real);
- ``governance`` = ``aa-crm-<env>-audit-g-*``, default retention 7 years.

Both buckets are versioned with a default Object Lock retention and SSE-KMS under the CRM ingest key, so the app sends
no lock or encryption header: every version it writes is locked by the bucket default. Object Lock requires an
integrity checksum on PutObject; the store sends SHA-256 (``ChecksumAlgorithm``). The task role may PutObject and
GetObject under ``audit/v1/tenants/*`` only: no delete, no version or retention change, no List (AA-CIS-Infra
``crm_app.tf``). A retried PUT of the same key writes a new, identical, locked version; the previous one stays.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from app.settings import Settings

log = logging.getLogger(__name__)

TIERS = ("compliance", "governance")
# Without s3:ListBucket a missing key answers 403 AccessDenied, not 404 (the documents store has the same rule).
MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404", "AccessDenied", "403"})


class ArchiveMissing(Exception):
    """The object is not in that tier (or the role cannot read it)."""


class AuditArchive(Protocol):
    async def put(self, tier: str, key: str, body: bytes) -> None: ...

    async def get(self, tier: str, key: str) -> bytes: ...


def _check_tier(tier: str) -> None:
    if tier not in TIERS:
        raise ValueError(f"unknown audit tier {tier!r}")


class MemoryAuditArchive:
    """In-process archive for tests and local runs. ``fail_next`` makes the next PUT to a tier raise (export retry
    tests); ``versions`` keeps every PUT, like a versioned bucket."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str], list[bytes]] = {}
        self.fail_next: set[str] = set()

    async def put(self, tier: str, key: str, body: bytes) -> None:
        _check_tier(tier)
        if tier in self.fail_next:
            self.fail_next.discard(tier)
            raise ConnectionError(f"simulated {tier} outage")
        self.versions.setdefault((tier, key), []).append(bytes(body))

    async def get(self, tier: str, key: str) -> bytes:
        _check_tier(tier)
        found = self.versions.get((tier, key))
        if not found:
            raise ArchiveMissing(tier)
        return found[-1]


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    code = (response.get("Error") or {}).get("Code")
    return str(code) if code is not None else None


class S3AuditArchive:
    """The two buckets. boto3 is blocking: each call runs in a worker thread; the client is created on first use."""

    def __init__(self, compliance_bucket: str, governance_bucket: str, region: str | None = None, client: Any = None):
        self.buckets = {"compliance": compliance_bucket, "governance": governance_bucket}
        self._region = region
        self._client = client
        self._lock = threading.Lock()

    def _s3(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3

                self._client = boto3.client("s3", region_name=self._region)
            return self._client

    async def put(self, tier: str, key: str, body: bytes) -> None:
        _check_tier(tier)
        digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        await asyncio.to_thread(
            self._s3().put_object,
            Bucket=self.buckets[tier],
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=digest,
        )

    async def get(self, tier: str, key: str) -> bytes:
        _check_tier(tier)

        def read() -> bytes:
            return self._s3().get_object(Bucket=self.buckets[tier], Key=key)["Body"].read()

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            code = _error_code(exc)
            if code not in MISSING_CODES:
                raise
            log.warning("audit_archive_object_missing tier=%s code=%s", tier, code)
            raise ArchiveMissing(tier) from None


def build_audit_archive(settings: Settings) -> AuditArchive | None:
    """Both buckets when configured; an in-memory archive on test / local; else None (export and verify skip)."""
    from app.settings import INSECURE_ENVIRONMENTS

    if settings.audit_bucket_compliance and settings.audit_bucket_governance:
        return S3AuditArchive(settings.audit_bucket_compliance, settings.audit_bucket_governance)
    if settings.environment in INSECURE_ENVIRONMENTS:
        return MemoryAuditArchive()
    return None
