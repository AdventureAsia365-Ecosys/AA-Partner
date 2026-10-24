"""F4 team and roles (AA-939, card CRM-11): members, invitations into the partner pool, owner recovery, the plan.

- :mod:`app.members.team`: the owner's Team page (members, pending invitations, invite, role change, removal), the
  reassign queue and the plan (entitlements), all inside ``tenant_tx()``.
- :mod:`app.members.invitations`: the invitation token, the claim step (creates or finds the partner-pool account)
  and the accept step (binds the membership to the caller's token ``(iss, sub)``), inside ``invitation_tx()``.
- :mod:`app.members.recovery`: owner recovery (Q1 6.3) for members (banner, cancel) and for the platform (open,
  complete, deactivate a member), the platform side inside ``platform_tx()`` (actor ``platform:<staff sub>``).
- :mod:`app.members.directory`: the partner pool admin calls the API may make (root ADR 0006b decision 5 allow-list).
- :mod:`app.members.mail`: AA system mail for invitations, recovery notices and AA data request notices (SES).
"""
