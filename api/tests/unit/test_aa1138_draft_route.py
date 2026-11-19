"""AA-1138 (crm-078): a CRM route entry names its Bedrock connection and whether it takes a temperature, and the CRM's
own monthly cap is enforced per connection of the route.

- The profile reads ``connection_key`` / ``sends_temperature`` (absent = the process's own connection, temperature on).
- The Bedrock provider plays an entry on its named connection: its own client, the role and ExternalId of that
  connection (one root per connection and process); a named connection without configuration is never called; an
  entry with ``sends_temperature`` false sends no temperature.
- ``app.settings``: the acc1 role needs its ExternalId; both are hidden from ``repr``.
- The cap: ``bedrock_acc1`` is CIS ``acc1``; its month spend is the rows of the (stage, model) pairs routed on it, the
  process's own connection keeps every other row. A route entry on a capped connection is left out (the draft goes to
  the next entry), all capped refuses, and no cap leaves the route as it was.
- Fail closed (review S285): under the dev rules a named connection without its own CRM cap is never used (the entry
  is left out, ``connection_cap_missing`` when it is the only one); the prod rules only log it.
- Through the real gateway: acc1 at its cap, or acc1 without a cap -> one answered row from the fallback entry, the
  acc1 entry never called; acc1 capped but not configured -> a free refused attempt, then the fallback answers.
Fakes only (no database, no AWS, no paid call); invented model ids, names and amounts.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from app import platform_settings as ps
from app.llm import connection_cap
from app.llm.gateway import Completion, LlmGateway
from app.llm.profiles import RouteEntry, StageProfile, profile_from_rows
from app.llm.providers import bedrock as bedrock_module
from app.llm.providers.bedrock import ACC1_CONNECTION, NOT_CONFIGURED, BedrockConverseProvider, Connection
from app.privacy.scrubber import PiiScrubber
from app.settings import SettingsError, load_settings
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa964_crm_ai import FakeBedrockClient, converse_answer
from tests.unit.test_aa1113_ai_paths_ledger import (  # noqa: F401 — the autouse isolation fixture
    PATHS,
    _isolated,
    _NoFindings,
    gateway_conn,
    ledger,
)

pytestmark = pytest.mark.asyncio

CAP_KEY = "crm.ai.connection_monthly_cap_usd"
FAST = "global.maker.fast-v1"  # the entry on the named connection (invented id)
LITE = "us.maker.lite-v1:0"  # the fallback on the process's own connection (invented id)
ACCOUNT = "1234" * 3  # built at run time: no account-shaped literal in a public repo
ROLE = f"arn:aws:iam::{ACCOUNT}:role/maker-invoker"
EXTERNAL = "example-external-id"
OWN_SQL = "FROM crm.llm_usage_daily u WHERE u.day >= $1 AND (NOT EXISTS"
NAMED_SQL = "FROM crm.llm_usage_daily u WHERE u.day >= $1 AND EXISTS"

HAIKU_LIKE = RouteEntry(0, FAST, "maker", Decimal("0.1"), Decimal("0.5"), ACC1_CONNECTION, False)
NOVA_LIKE = RouteEntry(2, LITE, "maker", Decimal("0.07"), Decimal("0.3"))


def draft_profile(*entries: RouteEntry) -> StageProfile:
    return StageProfile("crm_draft_reply", "writer", True, 2, 30, entries or (HAIKU_LIKE, NOVA_LIKE))


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    value: dict[str, Any] = {}
    real = ps.get

    def fake(key: str, tenant_id: object = None) -> Any:
        return value if key == CAP_KEY else real(key, tenant_id)

    monkeypatch.setattr(ps, "get", fake)
    return value


# ── the profile ───────────────────────────────────────────────────────────────────────────────────


def _row(position: int, model: str, **extra: Any) -> dict[str, Any]:
    return {
        "stage": "crm_draft_reply",
        "role": "writer",
        "is_active": True,
        "max_attempts": 2,
        "route_deadline_s": 30,
        "degrade_mode": "manual_reply_needed",
        "position": position,
        "model": model,
        "family": "maker",
        "price_in_per_mtok": Decimal("0.1"),
        "price_out_per_mtok": Decimal("0.5"),
        **extra,
    }


async def test_the_profile_reads_the_connection_and_the_temperature_rule() -> None:
    profile = profile_from_rows(
        [
            _row(0, FAST, connection_key=ACC1_CONNECTION, sends_temperature=False),
            _row(2, LITE, connection_key=None, sends_temperature=True),
        ]
    )
    assert profile is not None
    assert [(e.connection_key, e.sends_temperature) for e in profile.route] == [
        (ACC1_CONNECTION, False),
        (None, True),
    ]


async def test_a_row_without_the_columns_is_the_own_connection_with_a_temperature() -> None:
    profile = profile_from_rows([_row(0, LITE)])
    assert profile is not None and (profile.route[0].connection_key, profile.route[0].sends_temperature) == (None, True)


# ── the provider ──────────────────────────────────────────────────────────────────────────────────


def _provider(**kwargs: Any) -> BedrockConverseProvider:
    return BedrockConverseProvider(LITE, max_tokens=800, client=FakeBedrockClient(converse_answer("Hello.")), **kwargs)


async def test_an_entry_without_temperature_sends_none_and_the_default_still_does() -> None:
    process = _provider()
    plain = process.for_route(NOVA_LIKE)
    assert plain._request("hi", None)["inferenceConfig"] == {"maxTokens": 800, "temperature": 0.3}
    no_temp = process.for_route(dataclasses.replace(HAIKU_LIKE, connection_key=None))
    assert no_temp._request("hi", None)["inferenceConfig"] == {"maxTokens": 800}


async def test_a_named_entry_runs_on_its_own_connection_root() -> None:
    process = _provider(connections={ACC1_CONNECTION: Connection(ROLE, EXTERNAL)})
    first = process.for_route(HAIKU_LIKE)
    second = process.for_route(dataclasses.replace(HAIKU_LIKE, position=1))
    own = process.for_route(NOVA_LIKE)
    assert (first.connection_key, first.route_connection) == (ACC1_CONNECTION, ACC1_CONNECTION)
    assert (own.connection_key, own.route_connection) == ("bedrock", None)
    assert first._parent is second._parent is process._roots[ACC1_CONNECTION]  # one root per connection
    assert own._parent is process and first._parent is not process
    assert (first.role_arn, first.external_id, first.configured) == (ROLE, EXTERNAL, True)
    assert first.usd_in is None  # the process's price override never applies on another connection


async def test_a_named_entry_without_configuration_is_never_called() -> None:
    client = FakeBedrockClient(converse_answer("Hello."))
    process = BedrockConverseProvider(LITE, client=client)
    unconfigured = process.for_route(HAIKU_LIKE)
    assert unconfigured.configured is False
    with pytest.raises(bedrock_module.ProviderCallError) as caught:
        await unconfigured.complete("hi")
    assert (caught.value.error_class, caught.value.llm_error_class) == (NOT_CONFIGURED, "credential")
    assert client.requests == []


async def test_the_named_connection_assumes_its_role_with_the_external_id(monkeypatch: pytest.MonkeyPatch) -> None:
    assumed: list[dict[str, Any]] = []
    runtime = FakeBedrockClient(converse_answer("Hello."))

    class FakeSts:
        def assume_role(self, **kwargs: Any) -> dict[str, Any]:
            assumed.append(kwargs)
            import datetime as dt

            return {
                "Credentials": {
                    "AccessKeyId": "test-access",
                    "SecretAccessKey": "test-only",
                    "SessionToken": "test-token",
                    "Expiration": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
                }
            }

    import boto3

    monkeypatch.setattr(boto3, "client", lambda service, **_: FakeSts() if service == "sts" else runtime)
    process = BedrockConverseProvider(LITE, client=object(), connections={ACC1_CONNECTION: Connection(ROLE, EXTERNAL)})
    done = await process.for_route(HAIKU_LIKE).complete("hi")
    assert done.text == "Hello."
    assert [(a["RoleArn"], a["ExternalId"]) for a in assumed] == [(ROLE, EXTERNAL)]
    assert runtime.requests[0]["modelId"] == FAST and "temperature" not in runtime.requests[0]["inferenceConfig"]


async def test_the_process_provider_knows_acc1_only_when_configured() -> None:
    base = {
        "ENVIRONMENT": "test",
        "CRM_LLM_PROVIDER": "bedrock",
        "CRM_LLM_MODEL_ID": LITE,
        "CRM_LLM_USD_PER_MTOK_IN": "0.07",
        "CRM_LLM_USD_PER_MTOK_OUT": "0.3",
    }
    plain = BedrockConverseProvider.from_settings(load_settings(base), max_tokens=800)
    assert plain.connections == {}
    settings = load_settings({**base, "CRM_LLM_ACC1_ROLE_ARN": ROLE, "CRM_LLM_ACC1_EXTERNAL_ID": EXTERNAL})
    named = BedrockConverseProvider.from_settings(settings, max_tokens=800)
    assert named.connections == {ACC1_CONNECTION: Connection(ROLE, EXTERNAL)}
    assert ROLE not in repr(settings) and EXTERNAL not in repr(settings) and EXTERNAL not in repr(named.connections)


@pytest.mark.parametrize(
    "env",
    [
        {"CRM_LLM_ACC1_ROLE_ARN": ROLE},  # no ExternalId
        {"CRM_LLM_ACC1_ROLE_ARN": "not-an-arn", "CRM_LLM_ACC1_EXTERNAL_ID": EXTERNAL},
        {"CRM_LLM_ACC1_ROLE_ARN": ROLE, "CRM_LLM_ACC1_EXTERNAL_ID": "x"},  # shorter than STS allows
    ],
)
async def test_a_bad_acc1_connection_refuses_to_start(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        load_settings({"ENVIRONMENT": "test", **env})


# ── the cap per connection ────────────────────────────────────────────────────────────────────────


class Named:
    """A provider playing a route entry: its connection and the route's name for it."""

    name = "bedrock"
    family = "maker"

    def __init__(self, model: str, key: str, route_connection: str | None = None, text: str = "ok") -> None:
        self.model = model
        self.connection_key = key
        self.route_connection = route_connection
        self.text = text
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> Completion:
        self.prompts.append(prompt)
        return Completion(text=self.text, input_tokens=400, output_tokens=60, cost_usd=Decimal("0.0012"))


class Router(Named):
    """The process's provider: plays each route entry on its connection (fixed per entry, recorded)."""

    def __init__(self, text: str = "ok") -> None:
        super().__init__(LITE, "bedrock", None, text)
        self.fast = Named(FAST, ACC1_CONNECTION, ACC1_CONNECTION, text)
        self.lite = Named(LITE, "bedrock", None, text)

    def for_route(self, entry: RouteEntry) -> Named:
        return self.fast if entry.connection_key else self.lite


async def test_bedrock_acc1_is_the_cis_acc1_connection() -> None:
    assert connection_cap.CIS_CONNECTION == {"bedrock": "acc2", "bedrock_acc1": "acc1"}
    assert connection_cap.cis_connection(Named(FAST, ACC1_CONNECTION, ACC1_CONNECTION)) == "acc1"


async def test_each_connection_reads_its_own_month(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5, "acc2": 5})
    c = ScriptedConn().on(NAMED_SQL, Decimal("5")).on(OWN_SQL, Decimal("1"))
    assert await connection_cap.refusal(c, Named(FAST, ACC1_CONNECTION, ACC1_CONNECTION)) == (
        "connection_budget_exhausted",
        "budget",
    )
    assert await connection_cap.refusal(c, Named(LITE, "bedrock")) == (None, None)
    month = connection_cap.month_start()
    assert c.sql(NAMED_SQL) == [(month, ACC1_CONNECTION)] and c.sql(OWN_SQL) == [(month,)]


async def test_a_capped_entry_is_left_out_and_the_route_goes_on(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5, "acc2": 5})
    router = Router()
    c = ScriptedConn().on(NAMED_SQL, Decimal("5.01")).on(OWN_SQL, Decimal("0.2"))
    code, error, profile = await connection_cap.check_route(c, draft_profile(), router, router.for_route)
    assert (code, error) == (None, None)
    assert profile.route == (NOVA_LIKE,) and profile.max_attempts == 2


async def test_every_entry_capped_refuses_with_the_first_refusal(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5, "acc2": 0})
    router = Router()
    original = draft_profile()
    c = ScriptedConn().on(NAMED_SQL, Decimal("6")).on(OWN_SQL, Decimal("0"))
    assert await connection_cap.check_route(c, original, router, router.for_route) == (
        "connection_budget_exhausted",
        "budget",
        original,
    )


async def test_no_cap_on_the_own_connection_leaves_it_and_reads_nothing(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5})
    router = Router()
    original = draft_profile(NOVA_LIKE)
    c = ScriptedConn().on(NAMED_SQL, Decimal("600")).on(OWN_SQL, Decimal("600"))
    assert await connection_cap.check_route(c, original, router, router.for_route) == (None, None, original)
    assert c.calls == []


@pytest.mark.parametrize("value", [{}, {"acc2": 5}])
async def test_a_named_connection_without_a_cap_is_never_used_under_dev_rules(
    caps: dict[str, Any], value: dict[str, Any]
) -> None:
    """Fail closed (review S285): the acc1 entry is left out until the CRM's acc1 cap exists, whatever the order of
    the live steps; the route goes on with the own connection, and nothing is read for the uncapped acc1."""
    caps.update(value)
    router = Router()
    c = ScriptedConn().on(NAMED_SQL, Decimal("0")).on(OWN_SQL, Decimal("0"))
    code, error, profile = await connection_cap.check_route(c, draft_profile(), router, router.for_route)
    assert (code, error) == (None, None)
    assert profile.route == (NOVA_LIKE,)
    assert c.sql(NAMED_SQL) == []
    assert await connection_cap.refusal(c, Named(FAST, ACC1_CONNECTION, ACC1_CONNECTION)) == (
        "connection_cap_missing",
        "budget",
    )


async def test_a_named_connection_the_registry_cannot_name_is_refused_under_dev_rules(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5, "acc2": 5})
    c = ScriptedConn()
    assert await connection_cap.refusal(c, Named(FAST, "bedrock_elsewhere", "bedrock_elsewhere")) == (
        "connection_cap_missing",
        "budget",
    )
    assert c.calls == []


async def test_only_a_named_connection_alone_without_a_cap_refuses_the_call(caps: dict[str, Any]) -> None:
    router = Router()
    only_fast = draft_profile(HAIKU_LIKE)
    c = ScriptedConn()
    assert await connection_cap.check_route(c, only_fast, router, router.for_route) == (
        "connection_cap_missing",
        "budget",
        only_fast,
    )


async def test_prod_rules_only_log_a_named_connection_without_a_cap(
    caps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection_cap.spend_environment, "is_prod", lambda: True)
    router = Router()
    original = draft_profile()
    c = ScriptedConn()
    assert await connection_cap.check_route(c, original, router, router.for_route) == (None, None, original)
    assert c.calls == []


async def test_one_read_per_connection_of_the_route(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5})
    router = Router()
    two_fast = draft_profile(HAIKU_LIKE, dataclasses.replace(HAIKU_LIKE, position=1), NOVA_LIKE)
    c = ScriptedConn().on(NAMED_SQL, Decimal("1")).on(OWN_SQL, Decimal("1"))
    _, _, profile = await connection_cap.check_route(c, two_fast, router, router.for_route)
    assert profile is two_fast and len(c.sql(NAMED_SQL)) == 1 and c.sql(OWN_SQL) == []


async def test_without_a_route_the_gateway_provider_is_checked(caps: dict[str, Any]) -> None:
    caps.update({"acc2": 1})
    c = ScriptedConn().on(OWN_SQL, Decimal("2"))
    assert await connection_cap.check_route(c, None, Named(LITE, "bedrock"), lambda e: e) == (
        "connection_budget_exhausted",
        "budget",
        None,
    )


# ── through the real gateway (the draft path) ─────────────────────────────────────────────────────


class Source:
    def __init__(self, profile: StageProfile) -> None:
        self.profile = profile

    async def load_route(self, _db: Any, stage: str) -> StageProfile:
        return dataclasses.replace(self.profile, stage=stage)


_STAGE, _RULES, _RUN, _ANSWER = PATHS[0]


async def test_acc1_without_its_cap_sends_the_draft_to_the_fallback(caps: dict[str, Any]) -> None:
    caps.update({"acc2": 5})  # the acc1 entry exists only once the CRM's acc1 cap does (review S285)
    router = Router(_ANSWER)
    c = _RULES(gateway_conn().on(OWN_SQL, Decimal("0.5")))
    gw = LlmGateway(PiiScrubber(_NoFindings()), router, keys=object(), profiles=Source(draft_profile()))
    out = await _RUN(c, gw)
    assert router.fast.prompts == [] and len(router.lite.prompts) == 1
    [row] = ledger(c)
    assert (row[0], row[3], row[8]) == (_STAGE, LITE, "ok")
    assert out["outcome"] == "ok"


async def test_acc1_at_its_cap_sends_the_draft_to_the_fallback(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5, "acc2": 5})
    router = Router(_ANSWER)
    c = _RULES(gateway_conn().on(NAMED_SQL, Decimal("5")).on(OWN_SQL, Decimal("0.5")))
    gw = LlmGateway(PiiScrubber(_NoFindings()), router, keys=object(), profiles=Source(draft_profile()))
    out = await _RUN(c, gw)
    assert router.fast.prompts == [] and len(router.lite.prompts) == 1
    [row] = ledger(c)
    assert (row[0], row[3], row[8]) == (_STAGE, LITE, "ok")
    assert out["outcome"] == "ok"


async def test_an_unconfigured_acc1_is_a_free_refused_attempt_then_the_fallback(caps: dict[str, Any]) -> None:
    caps.update({"acc1": 5})  # capped, so the entry is tried; the process has no acc1 role
    process = BedrockConverseProvider(LITE, client=FakeBedrockClient(converse_answer(_ANSWER)))
    c = _RULES(gateway_conn().on(NAMED_SQL, Decimal("0")))
    gw = LlmGateway(PiiScrubber(_NoFindings()), process, keys=object(), profiles=Source(draft_profile()))
    out = await _RUN(c, gw)
    first, second = ledger(c)
    assert (first[3], first[6], first[8], first[10]) == (FAST, Decimal(0), "refused", "credential")
    assert (second[3], second[8]) == (LITE, "ok")
    assert out["outcome"] == "ok"
