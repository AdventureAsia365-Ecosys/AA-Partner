#!/usr/bin/env python3
"""AA-969 (CRM-37a): the runner of the bounded load test of the AA-Partner API on Dev (loadtest/README.md).

Standard library only, plus the ``aws`` CLI (read-only calls) and ``k6``. It never writes anything to AWS. Run by the
orchestrating agent (or the tech lead) from a workstation with an MFA session of the Dev account.

    python3 loadtest/crm_loadtest.py precheck --base-url https://partner-dev.<zone> --tenants-file T --token-file F \\
        --expect-account <acc2 id> [--profile aa365-admin]
    python3 loadtest/crm_loadtest.py run      (same arguments) [--duration-min 8] [--calm-rps 6] [--noisy-rps 12]

``precheck`` refuses (exit 2) unless every condition holds; ``run`` does the precheck, starts k6 and checks the
same guards every ``--guard-every`` seconds while it runs. Any guard failing stops k6 at once (SIGINT, then SIGKILL
after 30 s) and the run exits 3. A wall clock of the duration + 2 minutes kills k6 whatever it does.

Guards (the abort conditions of the README):

1. the credentials are the Dev account (``--expect-account``);
2. limits: duration <= 10 min, calm + noisy RPS <= 30, noisy VUs <= 30, 2..50 tenant ids (UUIDs, no duplicates);
3. the access token (a file of mode 0600, never printed) is valid for the whole run plus 5 minutes;
4. the CRM API answers ``/crm/health/ready`` 200 ``status: ok``;
5. ``shared.job`` idle: the newest CIS ``ops_metrics_sampled`` line (log group of the CIS API) shows no due queued
   row, nothing running, nothing starved and no ops check raised, and is at most 11 minutes old (the sampler runs
   every 5 minutes); a missing or stale line is a failure (fail closed);
6. ``booking.job`` idle enough: the newest Booking ``ops_metrics_sampled`` line shows no due row older than 120 s, no
   overdue running job and ``checks=ok`` (its always-queued ``bk_inquiry_pull`` runs every minute, so a running row is
   not by itself a failure; the exact idle check is the Infra idle gate run before, README step 3);
7. the CIS job-lag signal: the same CIS line's ``starved`` / ``ops_checks`` (the in-app ``jobs_starved`` check that
   replaced the CloudWatch job-lag alarm in the S263 alert diet); of the raised ops checks only the job-queue and
   database-capacity ones (:data:`GUARD_CHECK_PREFIXES`) stop the run; and no CloudWatch alarm in ALARM whose name
   starts with one of ``--alarm-prefix`` (default the CIS, Booking and CRM Dev prefixes);
8. the CRM's own line: no ops check raised, ``oldest_queued_age_s`` < 900;
9. ``--extra-guard`` (optional): a command that must exit 0 (e.g. the orchestrator's own job-table probe).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
K6_SCRIPT = HERE / "crm_load.js"
HARD_MAX_RPS = 30
HARD_MAX_MINUTES = 10
MIN_MINUTES = 4
MAX_TENANTS = 50
MAX_NOISY_VUS = 30
TOKEN_MARGIN_S = 300
SAMPLE_MAX_AGE_S = 11 * 60
BOOKING_MAX_DUE_AGE_S = 120
CRM_MAX_LAG_S = 900
REGION = "us-west-1"
LOG_GROUPS = {"cis": "/ecs/aa-cis-dev", "booking": "/ecs/aa-booking-dev", "crm": "/ecs/aa-crm-dev"}
ALARM_PREFIXES = ("aa-cis-dev-", "aa-booking-dev-", "aa-crm-dev-")
BASE_URL_RE = re.compile(r"^https://[a-z0-9.-]*-dev\.[a-z0-9.-]+$")
SAMPLE_EVENT = "ops_metrics_sampled"

Runner = Callable[[Sequence[str]], str]


class Refused(Exception):
    """A guard failed: the run must not start, or must stop now."""


@dataclass(frozen=True)
class Limits:
    duration_min: int = 8
    calm_rps: int = 6
    noisy_rps: int = 12
    noisy_max_vus: int = 12


@dataclass
class Report:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, why: str) -> None:
        self.ok = False
        self.failures.append(why)


# ── pure checks (unit-tested in api/tests/unit/test_aa969_loadtest_runner.py) ───────────────────────────────────────


def check_limits(limits: Limits) -> list[str]:
    out = []
    if not MIN_MINUTES <= limits.duration_min <= HARD_MAX_MINUTES:
        out.append(f"duration must be {MIN_MINUTES}-{HARD_MAX_MINUTES} minutes")
    if limits.calm_rps < 1 or limits.noisy_rps < 0 or limits.calm_rps + limits.noisy_rps > HARD_MAX_RPS:
        out.append(f"calm + noisy RPS must be 1..{HARD_MAX_RPS}")
    if not 1 <= limits.noisy_max_vus <= MAX_NOISY_VUS:
        out.append(f"noisy VUs must be 1..{MAX_NOISY_VUS}")
    return out


def check_base_url(url: str) -> list[str]:
    return [] if BASE_URL_RE.match(url.rstrip("/")) else ["the base URL must be the https Dev partner origin"]


def load_tenants(text: str) -> list[str]:
    try:
        raw = json.loads(text)
    except ValueError:
        raise Refused("the tenants file is not JSON") from None
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_TENANTS:
        raise Refused(f"the tenants file must list 2..{MAX_TENANTS} agency ids")
    try:
        ids = [str(uuid.UUID(str(t))) for t in raw]
    except ValueError:
        raise Refused("the tenants file holds a value that is not a UUID") from None
    if len(set(ids)) != len(ids):
        raise Refused("the tenants file repeats an agency id")
    return ids


def token_seconds_left(token: str, now: float) -> float:
    """Seconds until the JWT's ``exp`` (no signature check: the API checks it; this only plans the run)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"]) - now
    except (IndexError, ValueError, KeyError, TypeError):
        raise Refused("the token file does not hold a JWT") from None


def newest_sample(events: list[dict[str, Any]]) -> tuple[dict[str, Any], int] | None:
    """The newest ``ops_metrics_sampled`` event of a ``filter-log-events`` answer: (fields, timestamp ms)."""
    best: tuple[dict[str, Any], int] | None = None
    for e in events:
        ts = int(e.get("timestamp", 0))
        fields = parse_sample(str(e.get("message", "")))
        if fields is not None and (best is None or ts > best[1]):
            best = (fields, ts)
    return best


_KV = re.compile(r"([a-z_]+)=([^\s]+)")


def parse_sample(message: str) -> dict[str, Any] | None:
    """One sampler line: CIS writes structlog JSON (``event`` + fields); Booking and the CRM write
    ``ops_metrics_sampled k=v ...`` as the JSON ``event`` (or as plain text)."""
    text = message.strip()
    data: dict[str, Any] = {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        event = str(data.get("event", ""))
    else:
        event = text
    if event != SAMPLE_EVENT and not event.startswith(SAMPLE_EVENT + " "):
        return None
    for k, v in _KV.findall(event):
        data.setdefault(k, v)
    return data


def _num(fields: dict[str, Any], key: str) -> float:
    value = fields.get(key)
    if value is None or value == "":
        return float("inf")  # absent = not idle (fail closed)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")  # unreadable = not idle (fail closed)


# The fields each sampler line must carry for its guard to judge it. A line without them was written by a sampler
# whose read failed (CIS writes ``jobs_error`` / ``ops_checks_error`` instead when the shared DB is under pressure,
# exactly when this run must stop): missing = unreadable = fail closed, never "idle".
CIS_REQUIRED = ("queued_due", "running", "oldest_queued_age_seconds")
CIS_ERROR_FIELDS = ("jobs_error", "ops_checks_error")
BOOKING_REQUIRED = ("due_queued_age_s", "overdue_running_jobs", "checks")
CRM_REQUIRED = ("oldest_queued_age_s", "overdue_running_jobs", "checks")


def _missing(fields: dict[str, Any], required: Sequence[str]) -> list[str]:
    return [k for k in required if fields.get(k) in (None, "")]


# Ops checks that stop the run: job queues and database capacity. Other checks (LLM stage coverage, spend, audit
# export, ...) may be raised for days on Dev and say nothing about this load; they are reported, not acted on.
GUARD_CHECK_PREFIXES = ("jobs_", "rds_", "cis_app_too_many_connections", "tenant_pool_unavailable")


def raised_checks(value: Any) -> list[str]:
    """The check names a sampler line raised. CIS: ``ops_checks`` is a dict check -> result (absent when nothing was
    raised). Booking / CRM: the ``checks`` text is ``ok`` or ``name:result,...``."""
    if value in (None, "", "ok", {}, []):
        return []
    if isinstance(value, dict):
        return sorted(str(k) for k in value)
    return sorted({part.split(":", 1)[0] for part in str(value).split(",") if part and part != "ok"})


def _checks_raised(value: Any) -> bool:
    return any(name.startswith(GUARD_CHECK_PREFIXES) for name in raised_checks(value))


def judge_cis(sample: tuple[dict[str, Any], int] | None, now_ms: int) -> list[str]:
    if sample is None:
        return ["shared.job: no CIS sampler line in the last 11 minutes (fail closed)"]
    fields, ts = sample
    out = []
    if now_ms - ts > SAMPLE_MAX_AGE_S * 1000:
        out.append("shared.job: the newest CIS sampler line is older than 11 minutes")
    errors = [k for k in CIS_ERROR_FIELDS if fields.get(k)]
    if errors:
        out.append(f"shared.job: the CIS sampler could not read its checks ({', '.join(errors)}; fail closed)")
    missing = _missing(fields, CIS_REQUIRED)
    if missing:
        out.append(f"shared.job: the CIS sampler line lacks {', '.join(missing)} (fail closed)")
        return out
    if _num(fields, "queued_due") > 0 or _num(fields, "running") > 0:
        out.append(f"shared.job not idle (queued_due={fields.get('queued_due')}, running={fields.get('running')})")
    if fields.get("starved"):
        out.append("CIS job lag: starved kinds")
    if _num(fields, "oldest_queued_age_seconds") >= CRM_MAX_LAG_S:
        out.append("CIS job lag: oldest queued job >= 900 s")
    if _checks_raised(fields.get("ops_checks")):
        out.append("CIS ops checks raised a job / database finding")
    return out


def judge_booking(sample: tuple[dict[str, Any], int] | None, now_ms: int) -> list[str]:
    if sample is None:
        return ["booking.job: no Booking sampler line in the last 11 minutes (fail closed)"]
    fields, ts = sample
    out = []
    if now_ms - ts > SAMPLE_MAX_AGE_S * 1000:
        out.append("booking.job: the newest Booking sampler line is older than 11 minutes")
    missing = _missing(fields, BOOKING_REQUIRED)
    if missing:
        out.append(f"booking.job: the Booking sampler line lacks {', '.join(missing)} (fail closed)")
        return out
    if _num(fields, "due_queued_age_s") > BOOKING_MAX_DUE_AGE_S:
        out.append(f"booking.job not idle (a due row waits {fields.get('due_queued_age_s')} s)")
    if _num(fields, "overdue_running_jobs") > 0:
        out.append("booking.job: an overdue running job")
    if _checks_raised(fields.get("checks")):
        out.append("Booking ops checks raised a job / database finding")
    return out


def judge_crm(sample: tuple[dict[str, Any], int] | None, now_ms: int) -> list[str]:
    if sample is None:
        return ["crm.job: no CRM sampler line in the last 11 minutes (fail closed)"]
    fields, ts = sample
    out = []
    if now_ms - ts > SAMPLE_MAX_AGE_S * 1000:
        out.append("crm.job: the newest CRM sampler line is older than 11 minutes")
    missing = _missing(fields, CRM_REQUIRED)
    if missing:
        out.append(f"crm.job: the CRM sampler line lacks {', '.join(missing)} (fail closed)")
        return out
    if _num(fields, "oldest_queued_age_s") >= CRM_MAX_LAG_S or _num(fields, "overdue_running_jobs") > 0:
        out.append("crm.job lag above its SLO page threshold")
    if _checks_raised(fields.get("checks")):
        out.append("CRM ops checks raised a job / database finding")
    return out


def judge_alarms(alarm_names: list[str], prefixes: Sequence[str]) -> list[str]:
    hit = sorted(n for n in alarm_names if n.startswith(tuple(prefixes)))
    return [f"CloudWatch alarm in ALARM: {n}" for n in hit]


# ── AWS / HTTP reads (read-only) ──────────────────────────────────────────────────────────────────────────────────


AWS_TIMEOUT_S = 60
EXTRA_GUARD_TIMEOUT_S = 120


def aws_runner(profile: str) -> Runner:
    """Read-only ``aws`` calls. stdin is closed, so an expired MFA session fails at once instead of waiting for a code
    on the terminal; a timeout or a missing CLI is a :class:`Refused` like a non-zero exit (the guard fails closed)."""

    def run(args: Sequence[str]) -> str:
        cmd = ["aws", *args, "--profile", profile, "--region", REGION, "--output", "json"]
        try:
            done = subprocess.run(  # noqa: S603
                cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=AWS_TIMEOUT_S, check=False
            )
        except subprocess.TimeoutExpired:
            raise Refused(f"aws {args[0]} {args[1]} timed out after {AWS_TIMEOUT_S} s") from None
        except OSError as exc:
            raise Refused(f"aws {args[0]} {args[1]} could not start ({type(exc).__name__})") from None
        if done.returncode != 0:
            raise Refused(f"aws {args[0]} {args[1]} failed (exit {done.returncode})")
        return done.stdout

    return run


def account_of(run: Runner) -> str:
    return str(json.loads(run(["sts", "get-caller-identity"])).get("Account", ""))


def samples(run: Runner, log_group: str, now_ms: int) -> tuple[dict[str, Any], int] | None:
    out = run(
        [
            "logs",
            "filter-log-events",
            "--log-group-name",
            log_group,
            "--start-time",
            str(now_ms - SAMPLE_MAX_AGE_S * 1000),
            "--filter-pattern",
            f'"{SAMPLE_EVENT}"',
            "--max-items",
            "200",
        ]
    )
    return newest_sample(json.loads(out or "{}").get("events", []))


def alarms_in_alarm(run: Runner) -> list[str]:
    out = run(["cloudwatch", "describe-alarms", "--state-value", "ALARM", "--max-items", "200"])
    data = json.loads(out or "{}")
    return [a["AlarmName"] for a in data.get("MetricAlarms", []) + data.get("CompositeAlarms", [])]


def ready(base_url: str, opener: Callable[..., Any] = urllib.request.urlopen) -> list[str]:
    try:
        with opener(f"{base_url.rstrip('/')}/crm/health/ready", timeout=10) as resp:  # noqa: S310 — https checked
            body = json.loads(resp.read(65536) or b"{}")
            if resp.status != 200 or body.get("status") != "ok":
                return [f"CRM not ready (HTTP {resp.status})"]
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [f"CRM readiness unreachable ({type(exc).__name__})"]
    return []


def read_token(path: Path) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise Refused("the token file must be mode 0600 (owner only)")
    return path.read_text(encoding="utf-8").strip()


# ── the guard pass ─────────────────────────────────────────────────────────────────────────────────────────────────


def guard_pass(args: argparse.Namespace, run: Runner, now: float | None = None) -> Report:
    report = Report()
    now = time.time() if now is None else now
    now_ms = int(now * 1000)
    for why in ready(args.base_url):
        report.fail(why)
    try:
        report.failures += judge_cis(samples(run, LOG_GROUPS["cis"], now_ms), now_ms)
        report.failures += judge_booking(samples(run, LOG_GROUPS["booking"], now_ms), now_ms)
        report.failures += judge_crm(samples(run, LOG_GROUPS["crm"], now_ms), now_ms)
        report.failures += judge_alarms(alarms_in_alarm(run), args.alarm_prefix or ALARM_PREFIXES)
    except Refused as exc:
        report.failures.append(str(exc))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        report.failures.append(f"unreadable AWS answer ({type(exc).__name__})")
    if args.extra_guard:
        report.failures += extra_guard(args.extra_guard)
    report.ok = not report.failures
    return report


def extra_guard(command: str) -> list[str]:
    """``--extra-guard``: must exit 0. A timeout, a command that cannot start or cannot be parsed is a failure."""
    try:
        done = subprocess.run(  # noqa: S603
            shlex.split(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=EXTRA_GUARD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [f"extra guard timed out after {EXTRA_GUARD_TIMEOUT_S} s"]
    except (OSError, ValueError) as exc:
        return [f"extra guard could not run ({type(exc).__name__})"]
    return [f"extra guard exited {done.returncode}"] if done.returncode != 0 else []


def precheck(args: argparse.Namespace, run: Runner, now: float | None = None) -> Report:
    report = Report()
    now = time.time() if now is None else now
    limits = Limits(args.duration_min, args.calm_rps, args.noisy_rps, args.noisy_max_vus)
    for why in check_limits(limits) + check_base_url(args.base_url):
        report.fail(why)
    try:
        tenants = load_tenants(Path(args.tenants_file).read_text(encoding="utf-8"))
        report.notes.append(f"{len(tenants)} agencies (noisy = the first)")
        left = token_seconds_left(read_token(Path(args.token_file)), now)
        if left < args.duration_min * 60 + TOKEN_MARGIN_S:
            report.fail("the access token expires before the run ends (+5 min): sign in again")
    except (Refused, OSError) as exc:
        report.fail(str(exc))
    try:
        account = account_of(run)
        if not args.expect_account or account != args.expect_account:
            report.fail("the AWS credentials are not the expected Dev account")
    except (Refused, ValueError) as exc:
        report.fail(str(exc))
    if report.ok:
        guards = guard_pass(args, run, now)
        report.failures += guards.failures
    report.ok = not report.failures
    return report


def _emit(what: str, report: Report, **extra: Any) -> None:
    print(json.dumps({"step": what, "ok": report.ok, "failures": report.failures, "notes": report.notes, **extra}))


def run_load(args: argparse.Namespace, run: Runner) -> int:
    pre = precheck(args, run)
    _emit("precheck", pre)
    if not pre.ok:
        return 2
    env = {
        **os.environ,
        "LT_BASE_URL": args.base_url.rstrip("/"),
        "LT_TOKEN_FILE": str(Path(args.token_file).resolve()),
        "LT_TENANTS_FILE": str(Path(args.tenants_file).resolve()),
        "LT_DURATION_MIN": str(args.duration_min),
        "LT_CALM_RPS": str(args.calm_rps),
        "LT_NOISY_RPS": str(args.noisy_rps),
        "LT_NOISY_MAX_VUS": str(args.noisy_max_vus),
        "LT_SUMMARY_FILE": str(Path(args.summary_file).resolve()),
    }
    started = time.monotonic()
    wall_s = args.duration_min * 60 + 120
    proc = subprocess.Popen([args.k6, "run", "--quiet", str(K6_SCRIPT)], env=env)  # noqa: S603
    aborted: list[str] = []
    # Whatever happens from here on (a guard crashing, an interrupt, the runner itself failing), k6 never outlives the
    # runner: the finally stops it, and a crash is an abort (exit 3), not a traceback with k6 still running unguarded.
    try:
        while proc.poll() is None:
            time.sleep(min(args.guard_every, max(1.0, wall_s - (time.monotonic() - started))))
            if proc.poll() is not None:
                break
            if time.monotonic() - started > wall_s:
                aborted = ["wall clock: duration + 2 minutes passed"]
            else:
                guards = guard_pass(args, run)
                _emit("guard", guards, elapsed_s=int(time.monotonic() - started))
                aborted = guards.failures
            if aborted:
                _stop(proc)
                break
    except KeyboardInterrupt:
        aborted = ["stopped by hand"]
    except Exception as exc:  # noqa: BLE001 — fail closed: any runner error stops the load
        aborted = [f"the runner failed ({type(exc).__name__}); k6 stopped"]
    finally:
        if proc.poll() is None:
            _stop(proc)
    code = proc.wait()
    if aborted:
        print(json.dumps({"step": "aborted", "reasons": aborted, "k6_exit": code}))
        return 3
    print(json.dumps({"step": "done", "k6_exit": code, "summary_file": args.summary_file}))
    return 0 if code == 0 else 4  # k6 exits 99 when a threshold (an SLO) failed


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except ProcessLookupError:  # already gone
        pass


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="crm_loadtest.py", description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["precheck", "run"])
    p.add_argument("--base-url", required=True)
    p.add_argument("--tenants-file", required=True)
    p.add_argument("--token-file", required=True)
    p.add_argument("--expect-account", default="", help="the Dev account id (never written in this public repo)")
    p.add_argument("--profile", default="aa365-admin")
    p.add_argument("--duration-min", type=int, default=Limits.duration_min)
    p.add_argument("--calm-rps", type=int, default=Limits.calm_rps)
    p.add_argument("--noisy-rps", type=int, default=Limits.noisy_rps)
    p.add_argument("--noisy-max-vus", type=int, default=Limits.noisy_max_vus)
    p.add_argument("--guard-every", type=float, default=30.0)
    p.add_argument("--alarm-prefix", action="append", default=[])
    p.add_argument("--extra-guard", default="")
    p.add_argument("--k6", default="k6")
    p.add_argument("--summary-file", default="loadtest-summary.json")
    return p.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run = aws_runner(args.profile)
    if args.command == "precheck":
        report = precheck(args, run)
        _emit("precheck", report)
        return 0 if report.ok else 2
    return run_load(args, run)


if __name__ == "__main__":
    sys.exit(main())
