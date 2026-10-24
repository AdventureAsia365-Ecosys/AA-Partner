"""Dev seeder for the synthetic agencies (AA-935, card CRM-07). DRY RUN by default.

Run inside the API container (it has the ``crm_app`` DSN as ``DATABASE_URL`` and the identity values), through ECS
Exec on ``aa-crm-dev-api``::

    python -m app.seed                                   # DRY RUN: the plan, read-only
    python -m app.seed --apply                           # create / converge the agencies
    python -m app.seed --apply --member sen-vang:owner:<cognito sub> --member sen-vang:agent:<sub>

- Refuses to run outside ``dev`` / ``test`` / ``local`` (exit 2), and every agency is ``is_synthetic = true``
  (``app.agencies`` refuses anything else with 422 ``synthetic_required``).
- Idempotent: fixed ids (``app.seed.catalogue``), converging writes (``app.agencies.provision_agency``); a second
  ``--apply`` reports ``unchanged``.
- Members come only from ``--member KEY:ROLE:SUB`` (the subjects the e2e user script printed). The issuer is the
  partner pool when ``COGNITO_PARTNER_ISSUER`` is set (after AA-939), else the EXTERNAL pool (the ``cis-portal`` bridge
  on Dev); ``--issuer`` overrides.
- Travellers: generated (Faker EN/VI, ``app.seed.travellers``) and reported as counts; written by the intake card
  (AA-936) through the vault (``crm.traveler``, crm-003). Output is one JSON object per agency plus a summary line:
  ids, labels, counts, never a traveller's data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TextIO

import asyncpg

from app.agencies import SYNTHETIC_ONLY_ENVIRONMENTS, AgencySpec, MemberSpec, provision_agency, validate_member
from app.db import Database
from app.errors import ServiceError
from app.seed.catalogue import AGENCIES, BY_KEY, SyntheticAgency
from app.seed.travellers import nfc, travellers_for
from app.settings import Settings, SettingsError, load_settings
from app.tenancy import provisioning_tx

ACTOR_APPLY = "seed:aa-935"
ACTOR_READ = "seed:aa-935-dry-run"
STATE_SQL = "SELECT display_name, default_locale, is_synthetic FROM crm.tenant WHERE tenant_id = $1"
MEMBER_COUNT_SQL = "SELECT count(*) FROM crm.member WHERE deleted_at IS NULL"


class SeedRefused(Exception):
    """The seeder will not run with these arguments or in this environment."""


@dataclass(frozen=True)
class Plan:
    agencies: tuple[SyntheticAgency, ...]
    members: dict[str, tuple[MemberSpec, ...]]
    apply: bool
    issuer: str


def parse_member(raw: str, issuer: str) -> tuple[str, MemberSpec]:
    parts = raw.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise SeedRefused(f"--member must be KEY:ROLE:SUB, got {raw!r}")
    key, role, sub = parts
    if key not in BY_KEY:
        raise SeedRefused(f"--member names an unknown agency {key!r} (known: {', '.join(BY_KEY)})")
    if not issuer:
        raise SeedRefused("--member needs an issuer: set COGNITO_PARTNER_ISSUER / COGNITO_EXTERNAL_ISSUER or --issuer")
    try:
        return key, validate_member(MemberSpec(cognito_iss=issuer, cognito_sub=sub, role=role))
    except ServiceError as exc:
        raise SeedRefused(f"--member {key}:{role}: {exc.code}") from None


def build_plan(argv: Sequence[str], settings: Settings) -> Plan:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write (default: DRY RUN, read-only)")
    parser.add_argument("--agency", action="append", default=[], help="only this agency key (repeatable)")
    parser.add_argument("--member", action="append", default=[], help="KEY:ROLE:SUB (repeatable)")
    parser.add_argument("--issuer", default="", help="token issuer of the --member subjects")
    args = parser.parse_args(list(argv))
    if settings.environment not in SYNTHETIC_ONLY_ENVIRONMENTS:
        raise SeedRefused(f"the seeder runs on {sorted(SYNTHETIC_ONLY_ENVIRONMENTS)} only, not {settings.environment}")
    unknown = [k for k in args.agency if k not in BY_KEY]
    if unknown:
        raise SeedRefused(f"unknown agency key(s) {unknown} (known: {', '.join(BY_KEY)})")
    agencies = tuple(a for a in AGENCIES if not args.agency or a.key in args.agency)
    issuer = args.issuer or settings.cognito_partner_issuer or settings.cognito_external_issuer
    members: dict[str, list[MemberSpec]] = {}
    for raw in args.member:
        key, member = parse_member(raw, issuer)
        if key not in {a.key for a in agencies}:
            raise SeedRefused(f"--member {key}: that agency is not selected")
        members.setdefault(key, []).append(member)
    return Plan(agencies, {k: tuple(v) for k, v in members.items()}, args.apply, issuer)


def traveller_summary(agency: SyntheticAgency) -> dict[str, Any]:
    people = travellers_for(agency.key, agency.traveller_locales, agency.traveller_count)
    by_language: dict[str, int] = {}
    for p in people:
        by_language[p.preferred_language] = by_language.get(p.preferred_language, 0) + 1
    return {
        "planned": len(people),
        "by_language": dict(sorted(by_language.items())),
        "nfc_round_trip": all(nfc(p.full_name) == p.full_name for p in people),
        "written": 0,
        "write_status": "deferred: traveller writes arrive with AA-936 (crm.traveler through app.vault)",
    }


async def read_state(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> dict[str, Any] | None:
    async with provisioning_tx(pool, tenant_id, actor=ACTOR_READ) as ctx:
        row = await ctx.conn.fetchrow(STATE_SQL, tenant_id)
        if row is None:
            return None
        members = await ctx.conn.fetchval(MEMBER_COUNT_SQL)
    return {**dict(row), "members": int(members or 0)}


def _action(agency: SyntheticAgency, state: dict[str, Any] | None) -> str:
    if state is None:
        return "create"
    if (state["display_name"], state["default_locale"]) != (nfc(agency.display_name), agency.default_locale):
        return "converge"
    return "unchanged"


async def run_plan(plan: Plan, pool: asyncpg.Pool | None, environment: str, out: TextIO) -> int:
    totals = {"agencies": 0, "created": 0, "updated": 0, "members_added": 0, "members_updated": 0}
    for agency in plan.agencies:
        members = plan.members.get(agency.key, ())
        line: dict[str, Any] = {
            "agency": agency.key,
            "tenant_id": str(agency.tenant_id),
            "display_name": nfc(agency.display_name),
            "default_locale": agency.default_locale,
            "is_synthetic": True,
            "members_given": len(members),
            "travellers": traveller_summary(agency),
        }
        if pool is None:
            line["action"] = "unknown (no DATABASE_URL: plan only)"
        elif not plan.apply:
            state = await read_state(pool, agency.tenant_id)
            line["action"] = _action(agency, state)
            line["members_now"] = state["members"] if state else 0
        else:
            spec = AgencySpec(agency.tenant_id, agency.display_name, agency.default_locale, is_synthetic=True)
            result = await provision_agency(pool, spec, members, environment=environment, actor=ACTOR_APPLY)
            state = await read_state(pool, agency.tenant_id)
            line["action"] = "created" if result.created else ("converged" if result.updated else "unchanged")
            line["members_added"] = result.members_added
            line["members_updated"] = result.members_updated
            line["members_now"] = state["members"] if state else 0
            # Card E test: the stored label is the same string, byte for byte (Vietnamese diacritics included).
            line["name_round_trip"] = bool(state and state["display_name"] == nfc(agency.display_name))
            totals["created"] += int(result.created)
            totals["updated"] += int(result.updated)
            totals["members_added"] += result.members_added
            totals["members_updated"] += result.members_updated
            if not line["name_round_trip"]:
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
                out.write(json.dumps({"summary": "failed", "reason": "name round trip"}) + "\n")
                return 1
        totals["agencies"] += 1
        out.write(json.dumps(line, ensure_ascii=False) + "\n")
    mode = "apply" if plan.apply else "dry_run"
    out.write(json.dumps({"summary": mode, "environment": environment, **totals}) + "\n")
    return 0


async def main(argv: Sequence[str], out: TextIO = sys.stdout, settings: Settings | None = None) -> int:
    try:
        settings = settings or load_settings()
        plan = build_plan(argv, settings)
    except (SeedRefused, SettingsError) as exc:
        out.write(json.dumps({"summary": "refused", "reason": str(exc)}) + "\n")
        return 2
    db = Database(settings)
    if plan.apply and not db.configured:
        out.write(json.dumps({"summary": "refused", "reason": "--apply needs DATABASE_URL"}) + "\n")
        return 2
    try:
        pool = await db.pool() if db.configured else None
        return await run_plan(plan, pool, settings.environment, out)
    except ServiceError as exc:
        out.write(json.dumps({"summary": "refused", "reason": exc.code, "status": exc.status_code}) + "\n")
        return 2
    finally:
        await db.close()


def cli(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(main(sys.argv[1:] if argv is None else argv))
