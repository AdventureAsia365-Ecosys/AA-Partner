"""The agency's allowed CRM features (``crm.tenant_entitlement.allowed_features``), checked before a feature runs.

Since AA-1028 R1 slice 2 the list is the projection of the tenant's CIS entitlements (``crm.<code>`` keys of
tenant.entitlements.v1, written by ``crm_entitlement_pull``, :mod:`app.cis.entitlement_feed`); before, AA allowed
features in the L1 console. "Allowed" is AA's side; the owner still switches a feature on in Settings where the
feature has a switch (AI: ``crm.tenant_settings.ai_enabled``).

- :data:`FEATURES`: the six codes the database CHECK ``tenant_entitlement_features_chk`` accepts.
- :func:`feature_allowed`: one read on the context's connection (RLS: the context's agency); no row = nothing allowed.
- :func:`require_feature`: 403 ``feature_not_allowed`` with a ``denied`` audit event unless the feature is allowed.
"""

from __future__ import annotations

from app.errors import Forbidden
from app.tenancy import SystemContext, TenantContext

FEATURES = ("ai_assist", "whatsapp_sms", "stripe_connect", "campaigns", "custom_domain", "insights_export")
LABELS = {
    "ai_assist": "the AI assistant",
    "whatsapp_sms": "SMS and WhatsApp",
    "stripe_connect": "card payments",
    "campaigns": "campaigns",
    "custom_domain": "a custom domain",
    "insights_export": "the insights export",
}

ALLOWED_SQL = """
SELECT coalesce((SELECT $1 = ANY (allowed_features) FROM crm.tenant_entitlement WHERE deleted_at IS NULL), false)
"""


async def feature_allowed(ctx: TenantContext | SystemContext, feature: str) -> bool:
    if feature not in FEATURES:
        raise ValueError(f"unknown CRM feature {feature!r}")
    return bool(await ctx.conn.fetchval(ALLOWED_SQL, feature))


async def require_feature(
    ctx: TenantContext | SystemContext, feature: str, *, action: str, object_kind: str, object_id: str | None
) -> None:
    """403 ``feature_not_allowed`` (and a ``denied`` audit event for ``action``) unless the agency's plan allows it."""
    if await feature_allowed(ctx, feature):
        return
    ctx.audit(action, "denied", object_kind, object_id, reason="feature_not_allowed")
    raise Forbidden("feature_not_allowed", f"{LABELS[feature]} is not on this agency's plan (ask AA to add it)")
