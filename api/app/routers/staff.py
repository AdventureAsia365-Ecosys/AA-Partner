"""``/crm/staff/v1``: AA staff routes (route class STAFF; ops release and owner-granted support).

WORKFORCE ``crm-staff`` client only, groups ``crm-ops`` or ``crm-support``; support also needs an active owner grant
(CRM-13b). The routes arrive with CRM-26 / CRM-13b; this router exists now so the guard is in place first.
"""

from __future__ import annotations

from app.auth.identity_config import STAFF
from app.auth.route_guard import class_router

router = class_router(STAFF, tags=["staff"])
