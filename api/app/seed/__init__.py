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
- AA-936: ``--apply`` also makes sure each agency has one inquiry form (``Website (synthetic)``; its public key is
  printed, it is in the hosted URL anyway). ``--apply --travellers`` stores the agency's Faker EN/VI travellers
  (``app.seed.travellers``) as Stage 1 inquiries through the same path as the public form
  (``app.intake.public.store_submission``: encrypted submission + ``crm_intake_process`` job, idempotent per
  traveller), so they reach ``crm.traveler`` only through the vault. Needs the key provider (KMS on Dev).
- Output is one JSON object per agency plus a summary line: ids, labels, counts, never a traveller's data.
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
from app.intake import forms as intake_forms
from app.intake import stage1
from app.intake.public import store_submission
from app.seed.catalogue import AGENCIES, BY_KEY, SyntheticAgency
from app.seed.travellers import inquiry_for, nfc, travellers_for
from app.settings import Settings, SettingsError, load_settings
from app.tenancy import provisioning_tx, system_tx
from app.vault import KeyProvider, Vault, build_key_provider

ACTOR_APPLY = "seed:aa-935"
ACTOR_READ = "seed:aa-935-dry-run"
ACTOR_INTAKE = "seed:aa-936"
SEED_FORM_LABEL = "Website (synthetic)"
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
    travellers: bool = False


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
    parser.add_argument("--travellers", action="store_true", help="with --apply: store the synthetic inquiries")
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
    if args.travellers and not args.apply:
        raise SeedRefused("--travellers writes: use it with --apply")
    return Plan(agencies, {k: tuple(v) for k, v in members.items()}, args.apply, issuer, args.travellers)


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
        "write_status": "not requested (--apply --travellers stores them as inquiries)",
    }


async def ensure_seed_form(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> tuple[intake_forms.FormView, bool]:
    """The agency's synthetic inquiry form, created once (AA-936)."""
    async with provisioning_tx(pool, tenant_id, actor=ACTOR_APPLY) as ctx:
        found = [f for f in await intake_forms.list_forms(ctx) if f.label == SEED_FORM_LABEL]
        if found:
            return found[0], False
        return await intake_forms.create_form(ctx, label=SEED_FORM_LABEL, lead_source="synthetic_seed"), True


def seed_answers(agency: SyntheticAgency) -> list[stage1.Stage1Answers]:
    """Validated Stage 1 answers of the agency's travellers; a fake phone the validator refuses is left out."""
    out = []
    for person in travellers_for(agency.key, agency.traveller_locales, agency.traveller_count):
        raw = inquiry_for(person)
        try:
            out.append(stage1.validate(raw, honeypot_field="website"))
        except stage1.Stage1Invalid:
            raw["phone"] = None
            out.append(stage1.validate(raw, honeypot_field="website"))
    return out


async def write_travellers(pool: asyncpg.Pool, keys: KeyProvider, agency: SyntheticAgency, form_id: uuid.UUID) -> int:
    """Store the agency's synthetic inquiries (idempotent per traveller); returns how many were new."""
    written = 0
    async with system_tx(pool, agency.tenant_id, actor=ACTOR_INTAKE) as ctx:
        vault = Vault(keys, ctx)
        for index, answers in enumerate(seed_answers(agency)):
            stored = await store_submission(
                ctx, vault, form_id, answers, idempotency_key=f"seed:{agency.key}:{index}", created_by=ACTOR_INTAKE
            )
            written += int(stored is not None)
    return written


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


async def run_plan(
    plan: Plan, pool: asyncpg.Pool | None, environment: str, out: TextIO, keys: KeyProvider | None = None
) -> int:
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
            form, form_created = await ensure_seed_form(pool, agency.tenant_id)
            line["intake_form"] = {
                "intake_form_id": form.intake_form_id,
                "public_key": form.public_key,
                "created": form_created,
            }
            if plan.travellers:
                if keys is None:
                    out.write(
                        json.dumps({"summary": "refused", "reason": "--travellers needs the key provider"}) + "\n"
                    )
                    return 2
                line["travellers"]["written"] = await write_travellers(
                    pool, keys, agency, uuid.UUID(form.intake_form_id)
                )
                line["travellers"]["write_status"] = "queued for crm_intake_process"
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
        keys = build_key_provider(settings) if plan.travellers else None
        return await run_plan(plan, pool, settings.environment, out, keys)
    except ServiceError as exc:
        out.write(json.dumps({"summary": "refused", "reason": exc.code, "status": exc.status_code}) + "\n")
        return 2
    finally:
        await db.close()


def cli(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(main(sys.argv[1:] if argv is None else argv))
