"""AA-951 (card CRM-22): the CRM's copy of CIS AA-authored content (``catalog.http.v1`` 1.2, AA-977): "Before you
go" prep packs (R38) and starter templates (R9), in ``crm.aa_content`` (crm-022, a platform table, no tenant rows).

:func:`refresh` runs inside the hourly ``crm_catalog_sync`` after the tour pass: for each synced kind it lists every
page (``GET /aa-content?kind=``, the latest published version of each item that is not retired), fetches the body of
an item whose version or ``content_hash`` differs from the cache (``GET /aa-content/{id}/versions/{n}``, at most
:data:`MAX_FETCHES` a run; the rest next run), marks the listed ones checked and the ones CIS no longer lists
``listed = false`` (retired: the row stays). Only a complete list unlists anything. A CIS failure here never fails the
tour pass: the run reports ``content_error`` and the next run tries again.

An agency never reads CIS content live: it reads this cache inside its own transaction and copies what it uses
(``crm.prep_pack_selection``, ``crm.message_template``), so a sent proposal keeps its pack text (CRM-B5 card E).
"""

from __future__ import annotations

import logging
from collections import Counter

import asyncpg

from app.catalog import store
from app.catalog.client import CatalogUnavailable, ContentClient

log = logging.getLogger(__name__)

SYNCED_KINDS = ("prep_pack", "starter_template")
MAX_PAGES = 10
MAX_FETCHES = 60


async def refresh(pool: asyncpg.Pool, client: ContentClient, counts: Counter[str]) -> None:
    """One bounded pass over the synced kinds; counts go into the job result (``content_*``)."""
    fetches = 0
    for kind in SYNCED_KINDS:
        known = await store.content_known(pool, kind)
        listed: list[str] = []
        unchanged: list[str] = []
        cursor: str | None = None
        complete = False
        for _ in range(MAX_PAGES):
            items, cursor = await client.list_content(kind, cursor)
            counts["content_pages"] += 1
            for item in items:
                listed.append(item.content_id)
                if known.get(item.content_id) == (item.version, item.content_hash):
                    unchanged.append(item.content_id)
                    continue
                if fetches >= MAX_FETCHES:
                    counts["content_deferred"] += 1
                    continue
                fetches += 1
                body = await client.get_content(item.content_id, item.version)
                if body is None or body.kind != kind:
                    counts["content_skipped"] += 1
                    continue
                await store.content_upsert(pool, body)
                counts["content_fetched"] += 1
            if cursor is None:
                complete = True
                break
        await store.content_touch(pool, unchanged)
        counts["content_unchanged"] += len(unchanged)
        if complete:
            counts["content_unlisted"] += await store.content_unlist(pool, kind, listed)


async def refresh_safely(pool: asyncpg.Pool, client: ContentClient, counts: Counter[str]) -> None:
    try:
        await refresh(pool, client, counts)
    except CatalogUnavailable as exc:
        counts["content_error"] += 1
        log.warning("crm_catalog_sync_content_failed error=%s", exc.error_class)
