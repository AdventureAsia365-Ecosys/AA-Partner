"""``/crm/ops/v1``: the L1 platform console contract ``crm.ops.v1`` (route class OPS; root ``docs/contracts``).

WORKFORCE ``cis-admin`` tokens only (the CIS admin BFF forwards the staff token); group ``cis-admin`` reads,
``crm-platform-admin`` writes (``require_group``). The contract's operations arrive with CRM-13a (AA-944); this
router exists now so the guard is in place before the first route.
"""

from __future__ import annotations

from app.auth.identity_config import OPS
from app.auth.route_guard import class_router

router = class_router(OPS, tags=["ops"])
