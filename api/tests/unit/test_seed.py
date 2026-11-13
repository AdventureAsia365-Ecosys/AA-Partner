"""The Dev seeder (AA-935, card CRM-07): DRY RUN by default, Dev only, fixed ids, members only from --member, Faker
EN/VI travellers deterministic and NFC (E test), output without traveller data."""

from __future__ import annotations

import io
import json
import unicodedata
import uuid
from datetime import UTC, datetime

import pytest

from app import seed
from app.seed.catalogue import AGENCIES, BY_KEY, agency_id, e2e_username
from app.seed.travellers import EMAIL_DOMAIN, make_traveller, travellers_for
from tests.tokens import PARTNER_ISSUER, LocalPools
from tests.unit.fakes import ScriptedConn


def settings(**kw: object):  # noqa: ANN201
    return LocalPools().settings(**kw)


def lines(out: io.StringIO) -> list[dict]:
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_catalogue_has_two_or_three_agencies_with_fixed_ids() -> None:
    assert 2 <= len(AGENCIES) <= 3
    assert len({a.tenant_id for a in AGENCIES}) == len(AGENCIES)
    assert (
        AGENCIES[0].tenant_id
        == agency_id(AGENCIES[0].key)
        == uuid.uuid5(seed.catalogue.SYNTHETIC_NAMESPACE, f"aa-crm-synthetic-agency:{AGENCIES[0].key}")
    )
    assert any(a.default_locale == "vi-VN" for a in AGENCIES)
    assert any(any(ord(c) > 127 for c in a.display_name) for a in AGENCIES)  # a Vietnamese label with diacritics
    assert e2e_username("sen-vang", "owner") == "e2e-test-crm-sen-vang-owner"


def test_travellers_are_deterministic_nfc_and_undeliverable() -> None:
    a = travellers_for("sen-vang", ("vi_VN", "en_AU"), 6)
    b = travellers_for("sen-vang", ("vi_VN", "en_AU"), 6)
    assert a == b
    assert [t.faker_locale for t in a] == ["vi_VN", "en_AU"] * 3
    for t in a:
        assert unicodedata.normalize("NFC", t.full_name) == t.full_name
        assert t.email.endswith(f"@{EMAIL_DOMAIN}") and t.email.isascii()
        assert t.phone_e164.startswith("+") and t.phone_e164[1:].isdigit()
        assert json.loads(json.dumps(t.as_dict(), ensure_ascii=False))["full_name"] == t.full_name
    vietnamese = [t for t in a if t.faker_locale == "vi_VN"]
    assert any(any(ord(c) > 127 for c in t.full_name) for t in travellers_for("sen-vang", ("vi_VN",), 10))
    assert all(t.full_name.startswith(t.family_name) for t in vietnamese)  # Vietnamese order: family name first
    assert all(t.preferred_language == "vi" for t in vietnamese)
    assert make_traveller("x", 0, "en_US").preferred_language == "en"
    with pytest.raises(ValueError):
        make_traveller("x", 0, "fr_FR")
    with pytest.raises(ValueError):
        travellers_for("x", (), 1)


@pytest.mark.asyncio
async def test_dry_run_without_a_database_prints_the_plan_only() -> None:
    out = io.StringIO()
    assert await seed.main([], out, settings()) == 0
    rows = lines(out)
    assert [r["agency"] for r in rows[:-1]] == [a.key for a in AGENCIES]
    assert rows[-1]["summary"] == "dry_run"
    for r in rows[:-1]:
        assert r["is_synthetic"] is True and r["action"].startswith("unknown")
        assert r["travellers"]["written"] == 0 and r["travellers"]["nfc_round_trip"] is True
        # Counts only: no traveller field leaves the seeder.
        assert set(r["travellers"]) == {"planned", "by_language", "nfc_round_trip", "written", "write_status"}
    assert "Lữ hành Sen Vàng" in out.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "env", "reason"),
    [
        ([], {"environment": "prod"}, "runs on"),
        (["--agency", "nope"], {}, "unknown agency"),
        (["--member", "sen-vang:owner"], {}, "KEY:ROLE:SUB"),
        (["--member", "nope:owner:sub"], {}, "unknown agency"),
        (["--member", "sen-vang:boss:sub"], {}, "invalid_role"),
        (["--agency", "southern-cross", "--member", "sen-vang:owner:sub"], {}, "not selected"),
        (["--apply"], {}, "DATABASE_URL"),
    ],
)
async def test_refusals_exit_2(argv: list[str], env: dict, reason: str) -> None:
    kw = {"db_ssl": "require"} if env.get("environment") == "prod" else {}
    out = io.StringIO()
    assert await seed.main(argv, out, settings(**env, **kw)) == 2
    assert reason in lines(out)[-1]["reason"]


def test_member_needs_an_issuer() -> None:
    with pytest.raises(seed.SeedRefused, match="issuer"):
        seed.parse_member("sen-vang:owner:sub", "")


def test_issuer_is_the_partner_pool_never_the_retired_external_pool() -> None:
    plan = seed.build_plan(["--member", "sen-vang:owner:s1"], settings())
    assert plan.issuer == PARTNER_ISSUER
    # AA-979 P2b: no EXTERNAL fallback any more; without the partner issuer a member needs --issuer.
    with pytest.raises(seed.SeedRefused, match="issuer"):
        seed.build_plan(["--member", "sen-vang:owner:s1"], settings(cognito_partner_issuer=""))
    assert plan.members["sen-vang"][0].cognito_sub == "s1"
    assert plan.apply is False


class SeedDb:
    """Scripted pool standing in for the database: one agency exists, the others do not."""

    def __init__(self, existing: dict[uuid.UUID, dict]) -> None:
        self.conn = ScriptedConn()
        self.existing = existing
        self.current: uuid.UUID | None = None
        self.conn.on("set_config('app.tenant_id'", self._set)
        self.conn.on("SELECT display_name, default_locale, is_synthetic", lambda tid: self.existing.get(tid))
        self.conn.on("INSERT INTO crm.tenant", self._insert)
        self.conn.on("INSERT INTO crm.tenant_settings", None)  # AA-1096 L4: a new agency's platform defaults
        self.conn.on("INSERT INTO crm.member", lambda *a: uuid.uuid4())
        self.conn.on("SELECT count(*) FROM crm.member", 1)
        # AA-936: the synthetic inquiry form (none yet, then created).
        self.conn.on("SELECT count(*) FROM crm.intake_form", 0)
        self.conn.on("INSERT INTO crm.form_definition", uuid.uuid4())
        self.conn.on("INSERT INTO crm.intake_form", self._form)

    @staticmethod
    def _form(*args: object) -> dict:
        return {
            "intake_form_id": uuid.uuid4(),
            "public_key": args[0],
            "label": args[1],
            "allowed_origins": [],
            "honeypot_field": args[4],
            "default_owner_member_id": None,
            "lead_source": args[6],
            "privacy_notice_url": None,
            "status": "active",
            "created_at": datetime(2026, 10, 24, tzinfo=UTC),
        }

    def _set(self, tid: str) -> None:
        self.current = uuid.UUID(tid)

    def _insert(self, tid: uuid.UUID, name: str, locale: str, synthetic: bool) -> uuid.UUID | None:
        if tid in self.existing:
            return None
        self.existing[tid] = {"display_name": name, "default_locale": locale, "is_synthetic": synthetic}
        return tid


@pytest.mark.asyncio
async def test_dry_run_with_a_database_reads_state_and_writes_nothing() -> None:
    sen = BY_KEY["sen-vang"]
    db = SeedDb({sen.tenant_id: {"display_name": sen.display_name, "default_locale": "vi-VN", "is_synthetic": True}})
    plan = seed.build_plan([], settings())
    out = io.StringIO()
    assert await seed.run_plan(plan, db.conn, "dev", out) == 0
    actions = {r["agency"]: r.get("action") for r in lines(out)[:-1]}
    assert actions == {"sen-vang": "unchanged", "southern-cross": "create", "harbour-lights": "create"}
    assert db.conn.sql("INSERT INTO") == [] and db.conn.sql("UPDATE") == []


@pytest.mark.asyncio
async def test_apply_creates_then_reports_unchanged_and_the_name_round_trips() -> None:
    db = SeedDb({})
    plan = seed.build_plan(
        ["--apply", "--member", "sen-vang:owner:sub-o", "--member", "sen-vang:agent:sub-a"], settings()
    )
    out = io.StringIO()
    assert await seed.run_plan(plan, db.conn, "dev", out) == 0
    rows = lines(out)
    first = {r["agency"]: r for r in rows[:-1]}
    assert first["sen-vang"]["action"] == "created" and first["sen-vang"]["members_added"] == 2
    assert first["sen-vang"]["name_round_trip"] is True
    assert rows[-1] == {
        "summary": "apply",
        "environment": "dev",
        "agencies": 3,
        "created": 3,
        "updated": 0,
        "members_added": 2,
        "members_updated": 0,
    }
    assert all(args[3] is True for args in db.conn.sql("INSERT INTO crm.tenant ("))
    out2 = io.StringIO()
    assert await seed.run_plan(seed.build_plan(["--apply"], settings()), db.conn, "dev", out2) == 0
    assert {r["action"] for r in lines(out2)[:-1]} == {"unchanged"}


@pytest.mark.asyncio
async def test_apply_fails_when_the_stored_name_differs() -> None:
    sen = BY_KEY["sen-vang"]
    db = SeedDb({sen.tenant_id: {"display_name": sen.display_name, "default_locale": "vi-VN", "is_synthetic": True}})
    # The read-back returns a mangled label (what a wrong client encoding would do).
    db.conn.on(
        "SELECT display_name, default_locale, is_synthetic",
        lambda tid: {"display_name": "L? hanh Sen Vang", "default_locale": "vi-VN", "is_synthetic": True},
    )
    db.conn.on("UPDATE crm.tenant", None)
    out = io.StringIO()
    plan = seed.build_plan(["--apply", "--agency", "sen-vang"], settings())
    assert await seed.run_plan(plan, db.conn, "dev", out) == 1
    assert lines(out)[-1] == {"summary": "failed", "reason": "name round trip"}


# ── AA-969 (CRM-37a): the load-test agencies ────────────────────────────────────────────────────────────────────


def test_load_test_agencies_are_fixed_invented_and_apart_from_the_e2e_ones() -> None:
    from app.seed.catalogue import MAX_LOAD_TEST_AGENCIES, load_test_agencies

    fifty = load_test_agencies(MAX_LOAD_TEST_AGENCIES)
    assert [a.key for a in fifty[:2]] == ["lt-01", "lt-02"] and fifty[-1].key == "lt-50"
    assert len({a.tenant_id for a in fifty}) == 50
    assert not {a.tenant_id for a in fifty} & {a.tenant_id for a in AGENCIES}
    assert fifty[0].tenant_id == agency_id("lt-01")  # same id on every run
    for bad in (0, 51):
        with pytest.raises(ValueError):
            load_test_agencies(bad)


def test_load_test_plan_makes_the_identity_an_agent_of_each() -> None:
    plan = seed.build_plan(["--apply", "--load-test", "3", "--load-test-sub", "sub-load"], settings())
    assert [a.key for a in plan.agencies] == ["lt-01", "lt-02", "lt-03"]
    assert {k: [(m.cognito_iss, m.cognito_sub, m.role) for m in v] for k, v in plan.members.items()} == {
        k: [(PARTNER_ISSUER, "sub-load", "agent")] for k in ("lt-01", "lt-02", "lt-03")
    }
    dry = seed.build_plan(["--load-test", "2"], settings())
    assert (dry.apply, dry.members, len(dry.agencies)) == (False, {}, 2)


@pytest.mark.parametrize(
    ("argv", "kw", "reason"),
    [
        (["--load-test", "51"], {}, "between 1 and 50"),
        (["--load-test", "0", "--load-test-sub", "s"], {}, "between 1 and 50"),
        (["--load-test", "2", "--member", "sen-vang:owner:s1"], {}, "does not mix"),
        (["--load-test", "2", "--agency", "sen-vang"], {}, "does not mix"),
        (["--load-test", "2", "--travellers"], {}, "use it with --apply"),
        (["--load-test", "2", "--load-test-sub", "s"], {"cognito_partner_issuer": ""}, "needs an issuer"),
        (["--load-test", "2", "--load-test-sub", "s"], {"environment": "prod", "db_ssl": "require"}, "runs on"),
    ],
)
def test_load_test_refusals(argv: list[str], kw: dict, reason: str) -> None:
    with pytest.raises(seed.SeedRefused, match=reason):
        seed.build_plan(argv, settings(**kw))


def test_load_test_sub_is_validated_like_a_member() -> None:
    with pytest.raises(seed.SeedRefused, match="--load-test-sub"):
        seed.build_plan(["--load-test", "1", "--load-test-sub", "x" * 300], settings())


@pytest.mark.asyncio
async def test_load_test_apply_creates_synthetic_agencies_with_the_member() -> None:
    db = SeedDb({})
    plan = seed.build_plan(["--apply", "--load-test", "2", "--load-test-sub", "sub-load"], settings())
    out = io.StringIO()
    assert await seed.run_plan(plan, db.conn, "dev", out) == 0
    rows = lines(out)
    assert [r["agency"] for r in rows[:-1]] == ["lt-01", "lt-02"]
    assert rows[-1]["created"] == 2 and rows[-1]["members_added"] == 2
    assert all(args[3] is True for args in db.conn.sql("INSERT INTO crm.tenant ("))
