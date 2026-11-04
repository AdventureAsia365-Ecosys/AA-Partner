"""AA-969 (CRM-37a): the bounded load test runner (loadtest/crm_loadtest.py) and its sign-in helper
(loadtest/get_token.py): the limits, the fail-closed guards and the parsing of the samplers' log lines. No AWS, no
network: the AWS CLI runner and the identity provider are fakes."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

LOADTEST = Path(__file__).resolve().parents[3] / "loadtest"
TENANTS = [f"00000000-0000-4000-8000-0000000001{n:02d}" for n in range(1, 51)]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"aa969_{name}", LOADTEST / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lt = _load("crm_loadtest")
tok = _load("get_token")
NOW = 1_800_000_000.0
NOW_MS = int(NOW * 1000)


def _jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def _cis(ts_ms: int = NOW_MS - 60_000, **fields: Any) -> dict:
    base = {"event": "ops_metrics_sampled", "queued_due": 0, "running": 0, "starved": {}}
    return {"timestamp": ts_ms, "message": json.dumps({**base, **fields})}


def _line(text: str, ts_ms: int = NOW_MS - 60_000, as_json: bool = True) -> dict:
    return {"timestamp": ts_ms, "message": json.dumps({"event": text}) if as_json else text}


BOOKING_OK = "ops_metrics_sampled oldest_queued_age_s=0 overdue_running_jobs=0 running=1 due_queued_age_s=4 checks=ok"
CRM_OK = (
    "ops_metrics_sampled oldest_queued_age_s=0 overdue_running_jobs=0 running=0 due_queued_age_s=0 reveal_peak=0"
    " mail_dropped=0 checks=ok"
)


class FakeAws:
    def __init__(self, account: str = "acc-dev-test", alarms: list[str] | None = None, **groups: list[dict]) -> None:
        self.account = account
        self.alarms = alarms or []
        self.groups = {
            "/ecs/aa-cis-dev": groups.get("cis", [_cis()]),
            "/ecs/aa-booking-dev": groups.get("booking", [_line(BOOKING_OK)]),
            "/ecs/aa-crm-dev": groups.get("crm", [_line(CRM_OK)]),
        }
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[:2] == ["sts", "get-caller-identity"]:
            return json.dumps({"Account": self.account})
        if args[:2] == ["logs", "filter-log-events"]:
            return json.dumps({"events": self.groups[args[args.index("--log-group-name") + 1]]})
        if args[:2] == ["cloudwatch", "describe-alarms"]:
            return json.dumps({"MetricAlarms": [{"AlarmName": n} for n in self.alarms], "CompositeAlarms": []})
        raise AssertionError(args)


@pytest.fixture
def files(tmp_path: Path) -> argparse.Namespace:
    tenants = tmp_path / "tenants.json"
    tenants.write_text(json.dumps(TENANTS))
    token = tmp_path / "token"
    token.write_text(_jwt(NOW + 3600))
    os.chmod(token, 0o600)
    return lt.parse_args(
        [
            "precheck",
            "--base-url",
            "https://partner-dev.example.test",
            "--tenants-file",
            str(tenants),
            "--token-file",
            str(token),
            "--expect-account",
            "acc-dev-test",
        ]
    )


@pytest.fixture(autouse=True)
def api_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lt, "ready", lambda base_url: [])


# ── limits ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_limits_hold_the_hard_ceilings() -> None:
    assert lt.check_limits(lt.Limits()) == []
    assert lt.check_limits(lt.Limits(duration_min=11))
    assert lt.check_limits(lt.Limits(duration_min=3))
    assert lt.check_limits(lt.Limits(calm_rps=20, noisy_rps=11))
    assert lt.check_limits(lt.Limits(calm_rps=0))
    assert lt.check_limits(lt.Limits(noisy_max_vus=31))
    assert lt.check_limits(lt.Limits(calm_rps=10, noisy_rps=20)) == []


def test_only_the_https_dev_partner_origin() -> None:
    assert lt.check_base_url("https://partner-dev.example.test") == []
    assert lt.check_base_url("https://partner.example.test")  # prod-like host refused
    assert lt.check_base_url("http://partner-dev.example.test")
    assert lt.check_base_url("https://partner-dev.example.test/crm")


def test_tenants_file_rules() -> None:
    assert lt.load_tenants(json.dumps(TENANTS)) == TENANTS
    for bad in ("nope", json.dumps(TENANTS[:1]), json.dumps([*TENANTS, TENANTS[0]]), json.dumps(["x", "y"])):
        with pytest.raises(lt.Refused):
            lt.load_tenants(bad)
    with pytest.raises(lt.Refused, match="repeats"):
        lt.load_tenants(json.dumps([TENANTS[0], TENANTS[0]]))


def test_token_lifetime_is_read_without_trusting_it() -> None:
    assert lt.token_seconds_left(_jwt(NOW + 100), NOW) == 100
    with pytest.raises(lt.Refused):
        lt.token_seconds_left("not-a-jwt", NOW)


def test_token_file_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "t"
    path.write_text("x")
    os.chmod(path, 0o644)
    with pytest.raises(lt.Refused, match="0600"):
        lt.read_token(path)
    os.chmod(path, 0o600)
    assert lt.read_token(path) == "x"


# ── sampler lines ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_sampler_lines_of_the_three_apps_parse() -> None:
    cis = lt.parse_sample(_cis()["message"])
    assert cis is not None and cis["queued_due"] == 0
    booking = lt.parse_sample(json.dumps({"event": BOOKING_OK}))
    assert booking is not None and booking["due_queued_age_s"] == "4" and booking["checks"] == "ok"
    assert lt.parse_sample(BOOKING_OK) is not None  # plain text too
    assert lt.parse_sample(json.dumps({"event": "request_completed"})) is None
    assert lt.parse_sample("{broken") is None
    newest = lt.newest_sample([_cis(NOW_MS - 300_000, running=5), _cis(NOW_MS - 1000), _line("other")])
    assert newest is not None and newest[1] == NOW_MS - 1000 and newest[0]["running"] == 0


def test_raised_checks_formats() -> None:
    assert lt.raised_checks("ok") == [] and lt.raised_checks(None) == [] and lt.raised_checks({}) == []
    assert lt.raised_checks("jobs_stuck:sent,bulk_reveal:bell") == ["bulk_reveal", "jobs_stuck"]
    assert lt.raised_checks({"jobs_starved": {"bell": True}}) == ["jobs_starved"]


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        (None, "fail closed"),
        ((dict(queued_due=0, running=0), NOW_MS - 12 * 60_000), "older than 11 minutes"),
        ((dict(queued_due=3, running=0), NOW_MS), "shared.job not idle"),
        ((dict(queued_due=0, running=1), NOW_MS), "shared.job not idle"),
        ((dict(starved={"cis_kind": {"age_s": 950}}), NOW_MS), "starved"),
        ((dict(oldest_queued_age_seconds=901), NOW_MS), ">= 900"),
        ((dict(ops_checks={"jobs_starved": {"bell": True}}), NOW_MS), "job / database finding"),
        ((dict(running="?"), NOW_MS), "not idle"),
    ],
)
def test_cis_guard(sample: Any, reason: str) -> None:
    assert any(reason in why for why in lt.judge_cis(sample, NOW_MS))


def test_cis_guard_ignores_unrelated_findings() -> None:
    assert lt.judge_cis(({"ops_checks": {"stage_coverage": {"bell": False}}}, NOW_MS), NOW_MS) == []


def test_booking_guard_tolerates_its_always_queued_pull_but_not_a_backlog() -> None:
    ok = lt.parse_sample(BOOKING_OK)
    assert lt.judge_booking((ok, NOW_MS), NOW_MS) == []
    backlog = {**ok, "due_queued_age_s": "300"}
    assert any("booking.job not idle" in w for w in lt.judge_booking((backlog, NOW_MS), NOW_MS))
    overdue = {**ok, "overdue_running_jobs": "1"}
    assert lt.judge_booking((overdue, NOW_MS), NOW_MS)
    raised = {**ok, "checks": "jobs_overdue:sent"}
    assert lt.judge_booking((raised, NOW_MS), NOW_MS)
    assert lt.judge_booking(None, NOW_MS)


def test_crm_guard() -> None:
    ok = lt.parse_sample(CRM_OK)
    assert lt.judge_crm((ok, NOW_MS), NOW_MS) == []
    assert lt.judge_crm(({**ok, "oldest_queued_age_s": "900"}, NOW_MS), NOW_MS)
    assert lt.judge_crm(({**ok, "checks": "jobs_stuck:sent"}, NOW_MS), NOW_MS)
    assert lt.judge_crm(({**ok, "checks": "audit_export_lag:sent"}, NOW_MS), NOW_MS) == []
    assert lt.judge_crm(None, NOW_MS)


def test_alarm_guard_by_prefix() -> None:
    names = ["aa-crm-dev-api-5xx-rate", "other-alarm", "aa-cis-dev-api-no-healthy-task"]
    assert len(lt.judge_alarms(names, lt.ALARM_PREFIXES)) == 2
    assert lt.judge_alarms(["other-alarm"], lt.ALARM_PREFIXES) == []


# ── precheck and guard pass ───────────────────────────────────────────────────────────────────────────────────────


def test_precheck_passes_when_everything_is_quiet(files: argparse.Namespace) -> None:
    aws = FakeAws()
    report = lt.precheck(files, aws, NOW)
    assert report.ok, report.failures
    assert report.notes == ["50 agencies (noisy = the first)"]
    used = {tuple(c[:2]) for c in aws.calls}
    assert used == {("sts", "get-caller-identity"), ("logs", "filter-log-events"), ("cloudwatch", "describe-alarms")}


def test_precheck_refuses_another_account_and_a_short_token(files: argparse.Namespace) -> None:
    Path(files.token_file).write_text(_jwt(NOW + 600))
    report = lt.precheck(files, FakeAws(account="acc-other-test"), NOW)
    assert not report.ok
    assert any("expected Dev account" in f for f in report.failures)
    assert any("expires before the run ends" in f for f in report.failures)


def test_precheck_refuses_without_an_expected_account(files: argparse.Namespace) -> None:
    files.expect_account = ""
    assert not lt.precheck(files, FakeAws(), NOW).ok


def test_precheck_refuses_busy_job_tables_and_alarms(files: argparse.Namespace) -> None:
    aws = FakeAws(cis=[_cis(queued_due=2)], alarms=["aa-booking-dev-no-healthy-task"])
    report = lt.precheck(files, aws, NOW)
    assert not report.ok
    assert any("shared.job not idle" in f for f in report.failures)
    assert any("aa-booking-dev-no-healthy-task" in f for f in report.failures)


def test_guard_pass_fails_closed_on_an_aws_error(files: argparse.Namespace) -> None:
    def broken(args: list[str]) -> str:
        raise lt.Refused("aws logs filter-log-events failed (exit 255)")

    report = lt.guard_pass(files, broken, NOW)
    assert not report.ok and report.failures == ["aws logs filter-log-events failed (exit 255)"]


def test_guard_pass_runs_the_extra_guard(files: argparse.Namespace) -> None:
    files.extra_guard = f"{sys.executable} -c 'raise SystemExit(7)'"
    report = lt.guard_pass(files, FakeAws(), NOW)
    assert report.failures == ["extra guard exited 7"]
    files.extra_guard = f"{sys.executable} -c 'pass'"
    assert lt.guard_pass(files, FakeAws(), NOW).ok


def test_guard_pass_reports_an_unready_api(files: argparse.Namespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lt, "ready", lambda base_url: ["CRM not ready (HTTP 503)"])
    assert lt.guard_pass(files, FakeAws(), NOW).failures == ["CRM not ready (HTTP 503)"]


def test_ready_reads_the_readiness_body() -> None:
    class Resp:
        def __init__(self, status: int, body: dict) -> None:
            self.status, self._body = status, json.dumps(body).encode()

        def read(self, n: int) -> bytes:
            return self._body

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    src = _load("crm_loadtest")  # a fresh copy: the autouse fixture replaced lt.ready
    assert src.ready("https://x-dev.example.test", lambda url, timeout: Resp(200, {"status": "ok"})) == []
    assert src.ready("https://x-dev.example.test", lambda url, timeout: Resp(200, {"status": "not_ready"}))

    def down(url: str, timeout: float) -> Resp:
        raise OSError("refused")

    assert src.ready("https://x-dev.example.test", down) == ["CRM readiness unreachable (OSError)"]


def test_run_refuses_before_k6_when_the_precheck_fails(files: argparse.Namespace, capsys) -> None:  # noqa: ANN001
    files.k6 = "/nonexistent/k6"  # never started
    assert lt.run_load(files, FakeAws(cis=[_cis(running=1)])) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[0])["step"] == "precheck"


def test_run_stops_k6_when_a_guard_fails(
    files: argparse.Namespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stand-in k6 that would run for a minute; the second guard pass sees shared.job busy and stops it."""
    fake_k6 = tmp_path / "k6"
    fake_k6.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    os.chmod(fake_k6, 0o700)
    files.k6, files.guard_every = str(fake_k6), 0.2
    files.summary_file = str(tmp_path / "summary.json")
    monkeypatch.setattr(lt, "time", _Clock())
    aws = FakeAws()
    calls = {"n": 0}

    def guard(args: argparse.Namespace, run: Any, now: float | None = None) -> Any:
        calls["n"] += 1
        report = lt.Report()
        if calls["n"] > 1:  # the precheck's own guard pass is the first call
            report.fail("shared.job not idle (queued_due=1, running=0)")
        return report

    monkeypatch.setattr(lt, "guard_pass", guard)
    started = time.monotonic()
    assert lt.run_load(files, aws) == 3
    assert time.monotonic() - started < 30


class _Clock:
    """``time`` for run_load: real monotonic and sleep, a fixed wall clock for the token check."""

    def time(self) -> float:
        return NOW

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(min(s, 0.2))


# ── get_token ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_totp_matches_rfc_6238() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert tok.totp(secret, at=59) == "287082"
    assert tok.totp(secret, at=1111111109) == "081804"


class FakeIdp:
    def __init__(self, first: str = "SOFTWARE_TOKEN_MFA") -> None:
        self.first = first
        self.calls: list[tuple[str, dict]] = []

    def initiate_auth(self, **kw: Any) -> dict:
        self.calls.append(("initiate_auth", kw))
        return {"ChallengeName": self.first, "Session": "s1"}

    def respond_to_auth_challenge(self, **kw: Any) -> dict:
        self.calls.append((kw["ChallengeName"], kw))
        if kw["ChallengeName"] == "SELECT_CHALLENGE":
            return {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "s2"}
        return {"AuthenticationResult": {"AccessToken": "access-token-value"}}


def test_sign_in_with_password_then_authenticator() -> None:
    who = {"email": "lt@example.test", "password": "pw-for-test", "totp_secret": base64.b32encode(b"k" * 20).decode()}
    for first in ("SOFTWARE_TOKEN_MFA", "SELECT_CHALLENGE"):
        idp = FakeIdp(first)
        assert tok.sign_in(idp, "client", "client-secret-for-test", who, now=NOW) == "access-token-value"
        mfa = [kw for name, kw in idp.calls if name == "SOFTWARE_TOKEN_MFA"][0]
        assert mfa["ChallengeResponses"]["SOFTWARE_TOKEN_MFA_CODE"] == tok.totp(who["totp_secret"], NOW)
        assert idp.calls[0][1]["AuthFlow"] == "USER_AUTH"


def test_sign_in_that_does_not_finish_stops() -> None:
    class Stuck(FakeIdp):
        def respond_to_auth_challenge(self, **kw: Any) -> dict:
            return {"ChallengeName": "NEW_PASSWORD_REQUIRED"}

    with pytest.raises(SystemExit, match="did not finish"):
        tok.sign_in(Stuck(), "c", "s", {"email": "e", "password": "p", "totp_secret": "GEZDGNBV"}, now=NOW)


def test_token_file_is_written_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "out"
    tok.write_private(path, "abc")
    assert path.read_text() == "abc" and (path.stat().st_mode & 0o777) == 0o600
