"""``/crm/staff/v1`` S-02 ops release for AA ground operations (route class STAFF; AA-956, card CRM-26).

WORKFORCE ``crm-staff`` client only (the class guard), then group ``crm-ops`` on every route here (``crm-support`` and
any other group: 403 ``missing_group``; card R). The work is in :mod:`app.ops_release.staff`.

==========================================================  ======================================================
Route                                                       What
==========================================================  ======================================================
``GET  /ops-releases?days=``                                the board: live releases of every active or locked
                                                            agency departing in the window (no personal data), the
                                                            Booking incident admin link (Fix B3)
``GET  /agencies/{tenant}/trips/{trip}/ops-release``        one release document (audited "AA ops viewed trip X");
                                                            404 ``release_not_found`` without a confirmed AA line
``GET  /agencies/{tenant}/trips/{trip}/ops-release/manifest``  the minimal supplier manifest (CSV text, watermarked,
                                                            logged; ``?fields=`` picks the columns)
==========================================================  ======================================================

Every route is a GET (the staff class stays read-only, AA-971): a view and a manifest write only their own audit line,
the view count and the manifest record. Separate from ``app/routers/staff.py`` (AA-971 support access) so each card
owns its file; both are the STAFF class.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, Query, Request

from app.auth.identity_config import GROUP_CRM_OPS, STAFF
from app.auth.route_guard import Caller, class_router, require_group
from app.db import request_pool
from app.ops_release import staff
from app.vault.provider import require_keys

router = class_router(STAFF, tags=["staff-ops-release"])

Ops = Annotated[Caller, Depends(require_group(GROUP_CRM_OPS))]
ManifestField = Literal["name", "age_band", "dietary", "mobility", "arrival_flight", "departure_flight"]


def _admin_url(request: Request) -> str:
    return request.app.state.settings.booking_admin_url


@router.get("/ops-releases")
async def get_board(
    request: Request, caller: Ops, days: Annotated[int, Query(ge=1, le=staff.BOARD_HORIZON_DAYS)] = 60
) -> dict[str, Any]:
    pool = await request_pool(request)
    return await staff.board(pool, caller, horizon_days=days, booking_admin_url=_admin_url(request))


@router.get("/agencies/{tenant_id}/trips/{trip_id}/ops-release")
async def get_release(request: Request, caller: Ops, tenant_id: uuid.UUID, trip_id: uuid.UUID) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    return await staff.read(pool, keys, caller, tenant_id, trip_id, booking_admin_url=_admin_url(request))


@router.get("/agencies/{tenant_id}/trips/{trip_id}/ops-release/manifest")
async def get_manifest(
    request: Request,
    caller: Ops,
    tenant_id: uuid.UUID,
    trip_id: uuid.UUID,
    fields: Annotated[list[ManifestField] | None, Query(max_length=len(staff.MANIFEST_FIELDS))] = None,
) -> dict[str, Any]:
    pool = await request_pool(request)
    keys = require_keys(request.app.state.keys)
    return await staff.manifest(pool, keys, caller, tenant_id, trip_id, list(fields or []))
