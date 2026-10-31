"""Onboarding (AA-962, card CRM-31; design v3 §2.7 "Onboarding (Fix E2)", §11.5, R43 in-app; errata S247 §1 / §2): the
agency's getting-started checklist, its Connections overview, the controller settings and the one-time owner
acknowledgement of the shared responsibility.

**Checklist.** Three required steps (Fix E2): connect a mailbox (Gmail, Microsoft 365 or SMTP), invite the team, embed
the inquiry form. Optional steps, each with "Later": import clients (clients -> relationships -> trips -> notes), review
duplicates, card payments or bank transfer, deposit rules, the EN/VI starter templates, the privacy settings, the
kickoff call, the help centre. A step is **done** when the agency's data shows it (a connected mailbox, a second
member or an invitation, a received inquiry, an imported client, ...) or when the owner marked it done by hand for
the steps the data cannot show (a one-person agency, a form embedded but not used yet, a kickoff call held). The owner
puts optional steps on "Later" (they leave the Home list) and back. No settings choice is forced (§3.3 safe defaults).

**Connections** (errata §2): the status of each bring-your-own connection in the four states ``not_set_up``,
``connected``, ``needs_attention``, ``disconnected``, read from the tables that hold them (mailboxes, the payment
methods, the integration credentials, the ICS feeds). Never a credential, an account reference or an address: status
codes and counts only, so every role may read it (support sees health only, errata §1 CRM-13b).

**Controller settings** (errata §1 CRM-31): the agency's privacy-notice link, its contact for data requests and its own
consent wording (none = the CRM default). Owner only (``DATA_SETTINGS``), one audit event naming the fields.

**Acknowledgement**: the owner confirms once that the agency is the controller of its clients' data and holds its own
vendor accounts; the version of the text (:data:`ACK_VERSION`, the text itself is in the web, EN/VI) and who / when are
kept on ``crm.tenant_settings`` and one ``acknowledge`` audit event is written. A new version asks again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.errors import Conflict, Invalid
from app.tenancy import TenantContext

# The shared-responsibility text version the web shows (web/lib/onboarding.ts ACK_VERSION must match). The wording is
# a proposal until the business owner's legal text replaces it; a new text gets a new version and asks again.
ACK_VERSION = "2026-10-31.1"


@dataclass(frozen=True)
class Step:
    key: str
    required: bool
    manual: bool  # the owner may mark it done by hand
    href: str
    help: str


STEPS: tuple[Step, ...] = (
    Step("connect_mailbox", True, False, "/settings/mailbox", "/help#mailbox"),
    Step("invite_team", True, True, "/settings/team", "/help#team"),
    Step("embed_form", True, True, "/settings/forms", "/help#form"),
    Step("import_clients", False, False, "/settings/import", "/help#import"),
    Step("review_duplicates", False, True, "/clients/duplicates", "/help#duplicates"),
    Step("payments", False, False, "/settings/payments", "/help#payments"),
    Step("deposit_rules", False, True, "/settings/payments", "/help#payments"),
    Step("templates", False, False, "/settings/templates", "/help#templates"),
    Step("privacy_settings", False, False, "/settings/connections#privacy", "/help#privacy"),
    Step("kickoff_call", False, True, "/help#kickoff", "/help#kickoff"),
    Step("help_centre", False, True, "/help", "/help"),
)
STEP_KEYS = tuple(s.key for s in STEPS)
BY_KEY = {s.key: s for s in STEPS}
STATES = ("done", "later", "open")
CONNECTION_STATES = ("not_set_up", "connected", "needs_attention", "disconnected")

SETTINGS_SQL = """
SELECT onboarding_later, onboarding_done, privacy_notice_url, data_request_contact, consent_text,
       responsibility_ack_version, responsibility_ack_at, responsibility_ack_member_id
  FROM crm.tenant_settings
"""
ENSURE_SETTINGS_SQL = "INSERT INTO crm.tenant_settings DEFAULT VALUES ON CONFLICT (tenant_id) DO NOTHING"
FACTS_SQL = """
SELECT
  (SELECT array_agg(status) FROM crm.channel_account WHERE deleted_at IS NULL) AS mailboxes,
  (SELECT count(*) FROM crm.member WHERE status = 'active' AND deleted_at IS NULL)::int AS members,
  (SELECT count(*) FROM crm.member_invitation
    WHERE status IN ('pending', 'claimed', 'accepted') AND deleted_at IS NULL)::int AS invitations,
  (SELECT count(*) FROM crm.intake_form WHERE status = 'active' AND deleted_at IS NULL)::int AS forms,
  EXISTS (SELECT 1 FROM crm.form_submission WHERE intake_form_id IS NOT NULL) AS inquiries,
  EXISTS (SELECT 1 FROM crm.traveler WHERE import_ref_bidx IS NOT NULL AND deleted_at IS NULL) AS imported,
  (SELECT array_agg(provider || ':' || status) FROM crm.payment_provider_account WHERE deleted_at IS NULL)
      AS payment_methods,
  EXISTS (SELECT 1 FROM crm.message_template WHERE deleted_at IS NULL) AS templates,
  (SELECT array_agg(provider || ':' || status) FROM crm.integration_credential
    WHERE deleted_at IS NULL AND provider IN ('twilio', 'esign')) AS credentials,
  (SELECT count(*) FROM crm.calendar_feed WHERE status = 'active')::int AS calendar_feeds
"""
SET_STEPS_SQL = "UPDATE crm.tenant_settings SET onboarding_later = $1, onboarding_done = $2"
SET_CONTROLLER_SQL = """
UPDATE crm.tenant_settings SET privacy_notice_url = $1, data_request_contact = $2, consent_text = $3
"""
SET_ACK_SQL = """
UPDATE crm.tenant_settings SET responsibility_ack_version = $1, responsibility_ack_at = now(),
       responsibility_ack_member_id = $2
RETURNING responsibility_ack_at
"""
_URL = re.compile(r"^https://[^\s]{1,490}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_CONTACT = 320
MAX_CONSENT = 2000


async def _settings(ctx: TenantContext) -> Any:
    row = await ctx.conn.fetchrow(SETTINGS_SQL)
    if row is None:
        await ctx.conn.execute(ENSURE_SETTINGS_SQL)
        row = await ctx.conn.fetchrow(SETTINGS_SQL)
    return row


def _best(statuses: list[str]) -> str:
    for state in ("connected", "needs_attention", "disconnected"):
        if state in statuses:
            return state
    return "not_set_up"


def connections(facts: Any) -> list[dict[str, Any]]:
    payments = [p.split(":", 1) for p in (facts["payment_methods"] or [])]
    stripe = [status for provider, status in payments if provider == "stripe"]
    card = "connected" if "active" in stripe else "disconnected" if stripe else "not_set_up"
    bank = any(provider == "manual" and status == "active" for provider, status in payments)
    creds = [c.split(":", 1) for c in (facts["credentials"] or [])]
    twilio = _best([s for p, s in creds if p == "twilio"])
    esign = [s for p, s in creds if p == "esign"]
    out = [
        {
            "key": "mailbox",
            "required": True,
            "status": _best(list(facts["mailboxes"] or [])),
            "href": "/settings/mailbox",
            "help": "/help#mailbox",
        },
        {
            "key": "card_payments",
            "required": False,
            "status": card,
            "bank_transfer": bank,
            "href": "/settings/payments",
            "help": "/help#payments",
        },
        {"key": "sms_whatsapp", "required": False, "status": twilio, "href": None, "help": "/help#sms"},
        {
            "key": "calendar",
            "required": False,
            "status": "connected" if facts["calendar_feeds"] else "not_set_up",
            "href": "/calendar",
            "help": "/help#calendar",
        },
    ]
    if esign:
        out.append({"key": "esign", "required": False, "status": _best(esign), "href": None, "help": "/help#esign"})
    return out


async def overview(ctx: TenantContext, *, duplicate_count: int) -> dict[str, Any]:
    settings = await _settings(ctx)
    facts = await ctx.conn.fetchrow(FACTS_SQL)
    later = set(settings["onboarding_later"] or ())
    manual = set(settings["onboarding_done"] or ())
    conns = {c["key"]: c for c in connections(facts)}
    auto = {
        "connect_mailbox": conns["mailbox"]["status"] == "connected",
        "invite_team": facts["members"] > 1 or facts["invitations"] > 0,
        "embed_form": facts["forms"] > 0 and bool(facts["inquiries"]),
        "import_clients": bool(facts["imported"]),
        "review_duplicates": bool(facts["imported"]) and duplicate_count == 0,
        "payments": conns["card_payments"]["status"] == "connected" or conns["card_payments"]["bank_transfer"],
        "deposit_rules": False,
        "templates": bool(facts["templates"]),
        "privacy_settings": bool(settings["privacy_notice_url"] and settings["data_request_contact"]),
        "kickoff_call": False,
        "help_centre": False,
    }
    steps = []
    for step in STEPS:
        done = auto[step.key] or (step.manual and step.key in manual)
        steps.append(
            {
                "key": step.key,
                "required": step.required,
                "done": done,
                "marked_done": step.manual and step.key in manual,
                "later": (not step.required) and (not done) and step.key in later,
                "can_mark_done": step.manual,
                "href": step.href,
                "help": step.help,
            }
        )
    required = [s for s in steps if s["required"]]
    accepted = settings["responsibility_ack_version"] == ACK_VERSION
    return {
        "steps": steps,
        "required_total": len(required),
        "required_done": sum(1 for s in required if s["done"]),
        "optional_open": sum(1 for s in steps if not s["required"] and not s["done"] and not s["later"]),
        "complete": all(s["done"] for s in required) and accepted,
        "acknowledgement": {
            "version": ACK_VERSION,
            "accepted": accepted,
            "accepted_version": settings["responsibility_ack_version"],
            "accepted_at": settings["responsibility_ack_at"].isoformat() if settings["responsibility_ack_at"] else None,
        },
        "controller": {
            "privacy_notice_url": settings["privacy_notice_url"],
            "data_request_contact": settings["data_request_contact"],
            "consent_text": settings["consent_text"],
        },
        "connections": list(conns.values()),
        "duplicate_count": duplicate_count,
    }


async def set_step(ctx: TenantContext, key: str, state: str) -> dict[str, Any]:
    step = BY_KEY.get(key)
    if step is None:
        raise Invalid("invalid_step", ", ".join(STEP_KEYS))
    if state not in STATES:
        raise Invalid("invalid_state", ", ".join(STATES))
    if state == "later" and step.required:
        raise Invalid("step_required", "a required step cannot wait for later")
    if state == "done" and not step.manual:
        raise Invalid("step_not_manual", "this step is done when the CRM sees it")
    settings = await _settings(ctx)
    later = [k for k in settings["onboarding_later"] or () if k in BY_KEY and k != key]
    done = [k for k in settings["onboarding_done"] or () if k in BY_KEY and k != key]
    if state == "later":
        later.append(key)
    elif state == "done":
        done.append(key)
    await ctx.conn.execute(SET_STEPS_SQL, sorted(later), sorted(done))
    ctx.audit("update", "allowed", "tenant_settings", None, fields=("onboarding",), reason=f"step_{state}")
    return {"key": key, "state": state}


def clean_controller(url: Any, contact: Any, consent: Any) -> tuple[str | None, str | None, str | None]:
    def text(value: Any, limit: int, code: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise Invalid(code)
        out = _CONTROL.sub("", value).strip()
        if len(out) > limit:
            raise Invalid(code, f"at most {limit} characters")
        return out or None

    link = text(url, 500, "invalid_privacy_notice_url")
    if link is not None and not _URL.match(link):
        raise Invalid("invalid_privacy_notice_url", "an https:// link")
    contact_text = text(contact, MAX_CONTACT, "invalid_data_request_contact")
    if contact_text is not None and "\n" in contact_text:
        raise Invalid("invalid_data_request_contact", "one line: an e-mail address or a link")
    return link, contact_text, text(consent, MAX_CONSENT, "invalid_consent_text")


async def set_controller(ctx: TenantContext, url: Any, contact: Any, consent: Any) -> dict[str, Any]:
    link, contact_text, consent_text = clean_controller(url, contact, consent)
    await _settings(ctx)
    await ctx.conn.execute(SET_CONTROLLER_SQL, link, contact_text, consent_text)
    ctx.audit(
        "update",
        "allowed",
        "tenant_settings",
        None,
        fields=("privacy_notice_url", "data_request_contact", "consent_text"),
        reason="controller_settings",
    )
    return {"privacy_notice_url": link, "data_request_contact": contact_text, "consent_text": consent_text}


async def acknowledge(ctx: TenantContext, version: str) -> dict[str, Any]:
    if version != ACK_VERSION:
        raise Conflict("version_changed", "the text changed: read it again", version=ACK_VERSION)
    await _settings(ctx)
    at = await ctx.conn.fetchval(SET_ACK_SQL, version, ctx.member_id)
    ctx.audit("acknowledge", "allowed", "tenant_settings", None, reason="shared_responsibility")
    return {"version": version, "accepted": True, "accepted_at": at.isoformat()}
