"""The Dev-only E2E CIS admin (root ADR 0006a A10b / A10b-1, AA-816 / AA-979) as the CRM sees it.

The identity has no group of its own: the WORKFORCE pre token trigger (AA-CIS-Infra ``identity_triggers/workforce.py``,
``E2E_DEV_ONLY_TEST_STAFF``) issues its tokens with exactly ``cis-admin``, like a real CIS admin, and only on Dev. It is
recognised by the access token's ``email`` claim, which that trigger sets on every WORKFORCE access token (signed by
the pool, so a caller cannot choose it). Same address and rule as AA-CIS-App ``api/core/e2e_staff.py``.

In the CRM the identity is **read-only** on ``/crm/ops/v1`` (:func:`app.auth.route_guard.require_group` refuses it with
403 ``e2e_read_only``), with one exception: the Content studio invitation relay, on the terms of
:func:`app.members.studio.check_e2e_invitation` (synthetic agency, E2E mailbox address, an invited ``viewer`` /
``editor`` membership at CIS). TEMPORARY: removed with A10 / A10a at the end of project code.

AA-1019 (root ADR 0006a A10c): a second Dev-only WORKFORCE E2E identity, the **CRM staff** address
:data:`E2E_CRM_STAFF_EMAIL`, issued with exactly ``crm-ops`` + ``crm-support`` by the same trigger map and only on Dev.
On ``/crm/staff/v1`` it is **synthetic-only**: :class:`app.auth.route_guard.Caller` carries ``e2e_synthetic_only``,
the guard refuses its token outside ``dev`` / ``test`` / ``local`` (403 ``e2e_read_only``), and every staff context
(``ops_tx``, ``staff_tx``, ``support_tx`` in :mod:`app.tenancy`) refuses an agency whose ``crm.tenant.is_synthetic``
is not true (403 ``e2e_synthetic_only``); the board and the agency lists skip such agencies. Same removal as A10b.
"""

from __future__ import annotations

from typing import Any

E2E_CIS_ADMIN_EMAIL = "e2e-cis-admin@e2e-mail.lumiguides.it.com"
# The E2E mailbox domain (AA-CIS-Infra e2e_mailbox.tf), the domain of the A10b address. Exact: no sub-domain.
E2E_MAIL_DOMAIN = E2E_CIS_ADMIN_EMAIL.split("@", 1)[1]
# The Content studio roles the identity may send an invitation for (never ``owner``).
E2E_INVITE_ROLES = frozenset({"viewer", "editor"})
# AA-1019 (A10c): the Dev-only CRM staff E2E address (crm-ops + crm-support; synthetic agencies only).
E2E_CRM_STAFF_EMAIL = "e2e-crm-staff@e2e-mail.lumiguides.it.com"
# The 403 detail when that identity reaches an agency that is not synthetic.
E2E_SYNTHETIC_ONLY = "e2e_synthetic_only"
# The 403 detail of every refusal (the same string CIS answers, so the BFF and the E2E helpers need no new one).
E2E_READ_ONLY = "e2e_read_only"


def _normalise(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def is_e2e_cis_admin(email: Any) -> bool:
    """True for the A10b address (after trimming and lower-casing; a missing claim is False)."""
    return _normalise(email) == E2E_CIS_ADMIN_EMAIL


def is_e2e_crm_staff(email: Any) -> bool:
    """True for the A10c address (after trimming and lower-casing; a missing claim is False)."""
    return _normalise(email) == E2E_CRM_STAFF_EMAIL


def is_e2e_mailbox_address(address: Any) -> bool:
    """True when ``address`` is ``<local>@`` exactly :data:`E2E_MAIL_DOMAIN` (ASCII only, one ``@``, non-empty local
    part). The A10b address itself is not an invitee. ASCII is checked BEFORE lower-casing: U+212A KELVIN SIGN
    lower-cases to ASCII ``k`` and must not alias an E2E mailbox (the trigger's rule)."""
    if not isinstance(address, str) or not address.isascii():
        return False
    value = _normalise(address)
    if value.count("@") != 1 or value in (E2E_CIS_ADMIN_EMAIL, E2E_CRM_STAFF_EMAIL):
        return False
    local, domain = value.split("@")
    return bool(local) and domain == E2E_MAIL_DOMAIN
