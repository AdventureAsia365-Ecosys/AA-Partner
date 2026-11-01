"""``crm_workflow_run``: one workflow run's due steps (AA-959), in one :func:`app.tenancy.system_tx` of its agency
(actor ``job:crm_workflow_run``). Payload ``{tenant_id, workflow_run_id}``; result ids and codes only.

Queued by :func:`app.workflows.triggers.start` (after the Undo window for a member's action) and by the hourly tick
for a run whose wait has ended. Idempotent: :func:`app.workflows.engine.advance` locks the run and does nothing when it
is not due or no longer active, so a duplicate or late job is harmless. A locked or offboarding agency is skipped
(its runs wait); a value that does not open fails the job for good (audited by the context)."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app.errors import Forbidden, NotFound
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.tenancy import system_tx
from app.vault import KeyProvider, Vault
from app.vault.errors import DecryptionDenied
from app.workflows import engine


def _ids(payload: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        return uuid.UUID(str(payload["tenant_id"])), uuid.UUID(str(payload["workflow_run_id"]))
    except (KeyError, ValueError):
        raise PermanentError("bad_payload") from None


def build_handler(keys: KeyProvider | None) -> Handler:
    async def crm_workflow_run(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        tenant_id, run_id = _ids(job.payload)
        if keys is None:
            raise RuntimeError("key provider not configured")  # retried; the health check reports it
        try:
            async with system_tx(pool, tenant_id, actor=engine.ACTOR) as ctx:
                return await engine.advance(ctx, Vault(keys, ctx), run_id)
        except DecryptionDenied as exc:
            raise PermanentError(type(exc).__name__) from None
        except (Forbidden, NotFound):
            return {"workflow_run_id": str(run_id), "status": "agency_skipped"}

    return crm_workflow_run
