"""AA-993 V3 (ADR 0016 D3) — the CRM web and Content studio post-deploy verify is red/green, and so are their deploys.

- ``post_deploy_verify.sh crm-web`` / ``crm-portal`` run for real with the real rows files against fixture JSON
  (``curl`` and ``aws`` are PATH stubs from ``test_post_deploy_verify``): all good exits 0; a stale ``git_sha``/``sha``,
  a wrong version, the API rule lost or the API not ready each exit 1.
- ``edge_verify.sh`` runs for real against a stub ``curl`` that also answers ``%{redirect_url}``: the signed-out
  redirect, a public studio page and its ``/portal-assets`` asset, and the retired API-key sign-in URL (308, AA-979
  §4.5) are each red when wrong.
- The workflows are checked as text, like V2: ``deploy-web-dev.yml`` and ``deploy-portal-dev.yml`` call the verify after
  ``deploy`` (no inline smoke left), ``config`` fails instead of skipping, ``result`` needs every job and is red on any
  non-success; the portal deploy calls ``ui-smoke-portal.yml`` with ``require_tenant_login`` and the e2e studio
  user's three sign-in secrets, and the smoke step is red when one is missing.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_post_deploy_verify import (
    BASH,
    FAKE_AWS,
    FAKE_CURL,
    OLD_SHA,
    REPO,
    REQUIRED,
    SCRIPT,
    SHA,
    VERSION,
    _block,
    _failed_rows,
    _run_script,
    _service,
    _stub,
    needs_tools,
)

WORKFLOWS = REPO / ".github" / "workflows"
EDGE = REPO / ".github" / "scripts" / "edge_verify.sh"
VERIFY_WF = WORKFLOWS / "post-deploy-verify.yml"
WEB_WF = WORKFLOWS / "deploy-web-dev.yml"
PORTAL_WF = WORKFLOWS / "deploy-portal-dev.yml"
SMOKE_WF = WORKFLOWS / "ui-smoke-portal.yml"
BASE = "https://host.test"

APPS = {
    "crm-web": {"svc": "aa-crm-dev-web", "rows": {"healthz", "api-health", "api-ready"}},
    "crm-portal": {"svc": "aa-crm-dev-portal", "rows": {"studio-healthz", "api-health"}},
}


def _good_http(app: str) -> dict:
    api = {
        "/crm/health": {
            "status": 200,
            "body": {"status": "ok", "service": "aa-crm-api", "version": "0.4.0", "git_sha": OLD_SHA},
        },  # the API runs an older commit: still green
        "/crm/health/ready": {"status": 200, "body": {"status": "ok", "db": "ok", "schema_version": "crm-019"}},
    }
    if app == "crm-web":
        own = {
            "/healthz": {
                "status": 200,
                "body": {
                    "status": "ok",
                    "service": "aa-crm-web",
                    "version": VERSION,
                    "git_sha": SHA,
                    "environment": "dev",
                    "auth_configured": True,
                    "staff_auth_configured": True,
                },
            }
        }
    else:
        own = {
            "/api/healthz": {
                "status": 200,
                "body": {"ok": True, "service": "aa-partner-portal", "version": VERSION, "sha": SHA},
            },
            # On the agency host /healthz is the CRM web's: the portal rows must not read it.
            "/healthz": {
                "status": 200,
                "body": {"status": "ok", "service": "aa-crm-web", "version": VERSION, "git_sha": SHA},
            },
        }
    return {**api, **own}


def _ecs(app: str) -> dict:
    svc = APPS[app]["svc"]
    return {
        "services": {svc: _service("td:7") | {"serviceName": svc}},
        "taskDefinitions": {"td:7": {"containerDefinitions": [{"image": f"registry.example.invalid/{svc}:{SHA}"}]}},
    }


@pytest.fixture
def run_rows(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "curl", FAKE_CURL)
    _stub(bin_dir, "aws", FAKE_AWS)

    def run(app: str, http: dict, ecs: dict, **over: str) -> tuple[subprocess.CompletedProcess[str], str]:
        (tmp_path / "http.json").write_text(json.dumps(http))
        (tmp_path / "ecs.json").write_text(json.dumps(ecs))
        summary = tmp_path / "summary.md"
        summary.write_text("")
        svc = APPS[app]["svc"]
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_HTTP": str(tmp_path / "http.json"),
            "FAKE_ECS": str(tmp_path / "ecs.json"),
            "EXPECTED_SHA": SHA,
            "EXPECTED_VERSION": VERSION,
            "REQUIRED_SCHEMA": REQUIRED,
            "EXPECTED_IMAGE": f"registry.example.invalid/{svc}:{SHA}",
            "ECS_CLUSTER": "aa-cis-dev-cluster",
            "ECS_SERVICE": svc,
            "AWS_REGION": "us-west-1",
            "BASE": BASE,
            "GITHUB_STEP_SUMMARY": str(summary),
            "VERIFY_TRIES": "2",
            "VERIFY_RETRY_DELAY": "0",
            "VERIFY_INITIAL_WAIT": "0",
            **over,
        }
        proc = subprocess.run(  # noqa: S603
            [str(BASH), str(SCRIPT), app], env=env, capture_output=True, text=True, timeout=120
        )
        return proc, summary.read_text()

    return run


# ── rows (post_deploy_verify.sh with the real crm-web.rows / crm-portal.rows) ──────────────────────────────────────


@needs_tools
@pytest.mark.parametrize("app", sorted(APPS))
def test_rows_all_good_is_green(run_rows, app: str) -> None:
    r, summary = run_rows(app, _good_http(app), _ecs(app))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"### {app} post-deploy verify: PASS" in summary
    for row in ("ecs-rollout", "ecs-image", *APPS[app]["rows"]):
        assert f"| {row} | PASS |" in summary, (row, summary)


def _set(path: str, **fields: Any):
    def br(http: dict, ecs: dict) -> None:
        http[path]["body"].update(fields)

    return br


def _status(path: str, code: int):
    def br(http: dict, ecs: dict) -> None:
        http[path] = {"status": code, "body": {"detail": "x"}}

    return br


@needs_tools
@pytest.mark.parametrize(
    ("app", "breaker", "red"),
    [
        ("crm-web", _set("/healthz", git_sha=OLD_SHA), {"healthz"}),
        ("crm-web", _set("/healthz", version="0.0.1"), {"healthz"}),
        ("crm-web", _set("/healthz", auth_configured=False), {"healthz"}),
        ("crm-web", _set("/healthz", staff_auth_configured=False), {"healthz"}),  # AA-1019
        # /crm/health answered by the web default (rule order lost) or the API down.
        ("crm-web", _set("/crm/health", service="aa-crm-web"), {"api-health"}),
        ("crm-web", _status("/crm/health", 503), {"api-health"}),
        ("crm-web", _status("/crm/health/ready", 503), {"api-ready"}),
        ("crm-portal", _set("/api/healthz", sha=OLD_SHA), {"studio-healthz"}),
        ("crm-portal", _set("/api/healthz", version="0.0.1"), {"studio-healthz"}),
        # /api/* reaching the CRM web instead of the studio.
        ("crm-portal", _status("/api/healthz", 404), {"studio-healthz"}),
        ("crm-portal", _set("/crm/health", service="aa-crm-web"), {"api-health"}),
    ],
)
def test_rows_failures_are_red(run_rows, app: str, breaker, red: set[str]) -> None:
    http = copy.deepcopy(_good_http(app))
    breaker(http, None)
    r, summary = run_rows(app, http, _ecs(app))
    assert r.returncode == 1, r.stdout + r.stderr
    assert _failed_rows(summary) == red, summary


@needs_tools
@pytest.mark.parametrize("app", sorted(APPS))
def test_desired_zero_is_red_for_web_and_portal(run_rows, app: str) -> None:
    ecs = _ecs(app)
    svc = APPS[app]["svc"]
    ecs["services"][svc] = _service("td:7", desired=0, running=0) | {"serviceName": svc}
    r, summary = run_rows(app, _good_http(app), ecs)
    assert r.returncode == 1 and "ecs-rollout" in _failed_rows(summary)


@pytest.mark.parametrize("app", sorted(APPS))
def test_rows_files_are_get_only_and_public(app: str) -> None:
    rows = REPO / ".github" / "verify" / f"{app}.rows"
    lines = [ln for ln in rows.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    names = []
    for ln in lines:
        name, auth, req, want, _expr = ln.split("|", 4)
        method, _path = req.split(" ", 1)
        assert auth == "public" and method == "GET" and want.isdigit(), ln
        names.append(name)
    assert set(names) == APPS[app]["rows"] and len(names) == len(set(names))
    text = rows.read_text()
    # The studio's commit is read where the agency host routes to the studio, never /healthz (the CRM web's there).
    if app == "crm-portal":
        assert "|GET /api/healthz|" in text and "|GET /healthz|" not in text


def test_the_studio_serves_its_health_body_under_api() -> None:
    lib = (REPO / "portal" / "lib" / "health.ts").read_text()
    assert 'service: "aa-partner-portal"' in lib and "sha: env.GIT_SHA" in lib and "version: env.APP_VERSION" in lib
    for route in ("portal/app/healthz/route.ts", "portal/app/api/healthz/route.ts"):
        src = (REPO / route).read_text()
        assert "healthBody()" in src and "export function GET" in src and "fetch(" not in src, route
        assert '"Cache-Control": "no-store"' in src and 'dynamic = "force-dynamic"' in src, route


# ── edge_verify.sh (redirects and assets) ──────────────────────────────────────────────────────────────────────────

FAKE_EDGE_CURL = r"""
import json, os, sys
from urllib.parse import urlparse
args = sys.argv[1:]
out = fmt = url = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "--max-time"):
        if a == "-o":
            out = args[i + 1]
        elif a == "-w":
            fmt = args[i + 1]
        i += 2
        continue
    if not a.startswith("-"):
        url = a
    i += 1
route = json.load(open(os.environ["FAKE_HTTP"])).get(urlparse(url).path)
route = route or {"status": 404, "body": "not found"}
if out and out != "/dev/null":
    with open(out, "w") as f:
        f.write(route.get("body", ""))
if fmt:
    fmt = fmt.replace("%{http_code}", str(route["status"]))
    sys.stdout.write(fmt.replace("%{redirect_url}", route.get("location", "")))
"""

ASSET = "/portal-assets/_next/static/chunks/main-abc123.js"


def _edge_good(app: str) -> dict:
    if app == "crm-web":
        return {"/pipeline": {"status": 307, "location": f"{BASE}/auth/refresh?next=%2Fpipeline"}}
    return {
        "/terms": {"status": 200, "body": f'<html><script src="{ASSET}" async></script></html>'},
        ASSET: {"status": 200, "body": "js"},
        "/tenant-login": {"status": 308, "location": f"{BASE}/auth/refresh?next=%2Fportal%2Fdashboard"},
        "/portal/dashboard": {"status": 307, "location": f"{BASE}/auth/refresh?next=%2Fportal%2Fdashboard"},
    }


@pytest.fixture
def run_edge(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "curl", FAKE_EDGE_CURL)

    def run(app: str, http: dict, *, base: str = BASE) -> tuple[subprocess.CompletedProcess[str], str]:
        (tmp_path / "http.json").write_text(json.dumps(http))
        summary = tmp_path / "summary.md"
        summary.write_text("")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_HTTP": str(tmp_path / "http.json"),
            "BASE": base,
            "GITHUB_STEP_SUMMARY": str(summary),
            "VERIFY_TRIES": "2",
            "VERIFY_RETRY_DELAY": "0",
        }
        proc = subprocess.run(  # noqa: S603
            [str(BASH), str(EDGE), app], env=env, capture_output=True, text=True, timeout=60
        )
        return proc, summary.read_text()

    return run


@needs_tools
@pytest.mark.parametrize("app", sorted(APPS))
def test_edge_all_good_is_green(run_edge, app: str) -> None:
    r, summary = run_edge(app, _edge_good(app))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"### {app} edge checks: PASS" in summary and "**FAIL**" not in summary
    expected = (
        {"signin-redirect"}
        if app == "crm-web"
        else {"studio-page", "studio-assets", "api-key-signin-off", "signin-redirect"}
    )
    assert {ln.split("|")[1].strip() for ln in summary.splitlines() if "| PASS |" in ln} == expected


@needs_tools
def test_edge_trailing_slash_on_base_is_fine(run_edge) -> None:
    r, _ = run_edge("crm-web", _edge_good("crm-web"), base=BASE + "/")
    assert r.returncode == 0, r.stdout


def _edge_break(app: str, path: str, **route: Any) -> dict:
    http = copy.deepcopy(_edge_good(app))
    http[path] = route
    return http


@needs_tools
@pytest.mark.parametrize(
    ("app", "http", "red"),
    [
        # The web default answers the page itself (no session gate) or sends elsewhere.
        ("crm-web", _edge_break("crm-web", "/pipeline", status=200, body="<html>"), {"signin-redirect"}),
        ("crm-web", _edge_break("crm-web", "/pipeline", status=307, location=f"{BASE}/login"), {"signin-redirect"}),
        # A signed-out studio page sent anywhere but the CRM web's refresh.
        (
            "crm-portal",
            _edge_break("crm-portal", "/portal/dashboard", status=307, location=f"{BASE}/tenant-login"),
            {"signin-redirect"},
        ),
        ("crm-portal", _edge_break("crm-portal", ASSET, status=404, body=""), {"studio-assets"}),
        (
            "crm-portal",
            _edge_break("crm-portal", "/terms", status=200, body="<html>no assets</html>"),
            {"studio-assets"},
        ),
        (
            "crm-portal",
            _edge_break("crm-portal", "/terms", status=502, body="bad gateway"),
            {"studio-page", "studio-assets"},
        ),
        # AA-979 §4.5: the API-key sign-in page still served (PORTAL_API_KEY_SIGNIN on), or a temporary redirect, or
        # one back to itself (a loop).
        ("crm-portal", _edge_break("crm-portal", "/tenant-login", status=200, body="<html>"), {"api-key-signin-off"}),
        (
            "crm-portal",
            _edge_break(
                "crm-portal", "/tenant-login", status=307, location=f"{BASE}/auth/refresh?next=%2Fportal%2Fdashboard"
            ),
            {"api-key-signin-off"},
        ),
        (
            "crm-portal",
            _edge_break(
                "crm-portal", "/tenant-login", status=308, location=f"{BASE}/auth/refresh?next=%2Ftenant-login"
            ),
            {"api-key-signin-off"},
        ),
    ],
)
def test_edge_failures_are_red(run_edge, app: str, http: dict, red: set[str]) -> None:
    r, summary = run_edge(app, http)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _failed_rows(summary) == red, summary
    assert "::error::edge [" in r.stdout


@needs_tools
@pytest.mark.parametrize("base", ["", "host.test", "https://host.test/path", "https://host.test/?x=1"])
def test_edge_bad_base_is_red(run_edge, base: str) -> None:
    r, summary = run_edge("crm-web", _edge_good("crm-web"), base=base)
    assert r.returncode == 1 and "inputs" in _failed_rows(summary)


@needs_tools
@pytest.mark.parametrize("args", [(), ("crm",), ("../x",)])
def test_edge_needs_a_web_or_portal_app(args: tuple[str, ...]) -> None:
    proc = subprocess.run(  # noqa: S603
        [str(BASH), str(EDGE), *args], capture_output=True, text=True, timeout=30, env={"PATH": os.environ["PATH"]}
    )
    assert proc.returncode == 2 and "usage" in proc.stdout


# ── the verify workflow picks the service per app ──────────────────────────────────────────────────────────────────


@needs_tools
@pytest.mark.parametrize(
    ("app", "service", "base", "edge"),
    [
        ("crm", "aa-crm-dev-api", "https://api.test", "false"),
        ("crm-web", "aa-crm-dev-web", "https://web.test", "true"),
        ("crm-portal", "aa-crm-dev-portal", "https://portal.test", "true"),
    ],
)
def test_verify_resolves_each_app(tmp_path: Path, app: str, service: str, base: str, edge: str) -> None:
    job = _block(VERIFY_WF.read_text(), "verify:", 2)
    step = job[job.index("      - name: Resolve the app") : job.index("      - name: AWS_ROLE_ARN is set")]
    script = _run_script(step)
    genv, gout = tmp_path / "env", tmp_path / "out"
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_ENV": str(genv),
        "GITHUB_OUTPUT": str(gout),
        "VERIFY_APP": app,
        "API_REPO": "aa-crm-dev-api",
        "API_SERVICE": "aa-crm-dev-api",
        "API_BASE": "https://api.test/",
        "WEB_REPO": "aa-crm-dev-web",
        "WEB_SERVICE": "aa-crm-dev-web",
        "WEB_BASE": "https://web.test",
        "PORTAL_REPO": "aa-crm-dev-portal",
        "PORTAL_SERVICE": "aa-crm-dev-portal",
        "PORTAL_HOST": "portal.test",
    }
    proc = subprocess.run([str(BASH), "-c", script], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert genv.read_text().splitlines() == [
        f"VERIFY_APP={app}",
        f"ECR_REPOSITORY={service}",
        f"ECS_SERVICE={service}",
        f"BASE={base}",
    ]
    assert gout.read_text().strip() == f"edge={edge}"


@needs_tools
def test_verify_refuses_an_unknown_app(tmp_path: Path) -> None:
    job = _block(VERIFY_WF.read_text(), "verify:", 2)
    script = _run_script(
        job[job.index("      - name: Resolve the app") : job.index("      - name: AWS_ROLE_ARN is set")]
    )
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_ENV": str(tmp_path / "e"),
        "GITHUB_OUTPUT": str(tmp_path / "o"),
        "VERIFY_APP": "crm-everything",
    }
    proc = subprocess.run([str(BASH), "-c", script], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert proc.returncode == 1 and "::error::app must be crm, crm-web or crm-portal" in proc.stdout


def test_verify_runs_the_edge_checks_for_web_and_portal_even_after_a_red_row() -> None:
    text = VERIFY_WF.read_text()
    edge = text[text.index("      - name: Edge checks") :]
    assert "if: ${{ !cancelled() && steps.app.outputs.edge == 'true' }}" in edge
    assert 'run: bash .github/scripts/edge_verify.sh "$VERIFY_APP"' in edge


# ── deploy workflows (text + their config/result scripts) ──────────────────────────────────────────────────────────

DEPLOYS = {
    WEB_WF: {"jobs": ("config", "deploy", "verify"), "app": "crm-web", "title": "Deploy Web Dev"},
    PORTAL_WF: {"jobs": ("config", "deploy", "verify", "smoke"), "app": "crm-portal", "title": "Deploy Portal Dev"},
}


@pytest.mark.parametrize("wf", list(DEPLOYS), ids=lambda p: p.name)
def test_deploy_calls_verify_after_deploy_and_result_needs_every_job(wf: Path) -> None:
    spec = DEPLOYS[wf]
    text = wf.read_text()
    jobs = _block(text, "jobs:", 0)
    assert re.findall(r"^  ([a-z][a-z-]*):$", jobs, re.M) == [*spec["jobs"], "result"]
    verify = _block(jobs, "verify:", 2)
    assert "    uses: ./.github/workflows/post-deploy-verify.yml" in verify and "    needs: deploy\n" in verify
    assert "      expected_sha: ${{ github.sha }}" in verify and "      from_deploy: true" in verify
    assert f"      app: {spec['app']}" in verify
    result = _block(jobs, "result:", 2)
    assert "    if: always()" in result and f"    needs: [{', '.join(spec['jobs'])}]" in result
    for name in spec["jobs"]:
        assert not re.search(r"^    if:", _block(jobs, f"{name}:", 2), re.M), name
    deploy = _block(jobs, "deploy:", 2)
    assert "Verify the running image" not in deploy and "Smoke test" not in deploy
    assert "smoke skipped" not in text and "DESIRED_COUNT" not in text and '>> "$GITHUB_OUTPUT"' not in text
    assert deploy.rstrip().endswith('aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"')
    # Public logs: task definitions as family:revision only.
    assert 'split("/") | last' in deploy and "${NEW_TD##*/}" in deploy
    # The deploy concurrency group is unchanged (one deploy per service at a time).
    group = f"deploy-{'web' if wf is WEB_WF else 'portal'}-dev"
    assert f"concurrency:\n  group: {group}\n  cancel-in-progress: false" in text


@needs_tools
@pytest.mark.parametrize("wf", list(DEPLOYS), ids=lambda p: p.name)
@pytest.mark.parametrize(
    ("role", "enabled", "code"), [("", "true", 1), ("arn", "", 1), ("arn", "false", 1), ("arn", "true", 0)]
)
def test_config_fails_instead_of_skipping(wf: Path, role: str, enabled: str, code: int) -> None:
    config = _block(_block(wf.read_text(), "jobs:", 0), "config:", 2)
    assert "GITHUB_OUTPUT" not in config and not re.search(r"^    outputs:", config, re.M)
    flag = "WEB_ENABLED" if wf is WEB_WF else "PORTAL_ENABLED"
    proc = subprocess.run(  # noqa: S603
        [str(BASH), "-c", _run_script(config)],
        env={"PATH": os.environ["PATH"], "ROLE_ARN": role, flag: enabled},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == code, proc.stdout
    if code:
        assert "::error::" in proc.stdout


def _result(wf: Path, needs: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    script = _run_script(_block(_block(wf.read_text(), "jobs:", 0), "result:", 2))
    env = {"PATH": os.environ["PATH"], "NEEDS": json.dumps(needs), "EVENT": "push"}
    return subprocess.run([str(BASH), "-c", script], env=env, capture_output=True, text=True, timeout=60)  # noqa: S603


@needs_tools
@pytest.mark.parametrize("wf", list(DEPLOYS), ids=lambda p: p.name)
def test_result_is_green_only_when_every_job_succeeded(wf: Path) -> None:
    jobs = DEPLOYS[wf]["jobs"]
    ok = {j: {"result": "success", "outputs": {}} for j in jobs}
    assert _result(wf, ok).returncode == 0
    for job in jobs:
        for res in ("failure", "skipped", "cancelled"):
            bad = copy.deepcopy(ok)
            bad[job]["result"] = res
            r = _result(wf, bad)
            assert r.returncode == 1 and f"::error::{DEPLOYS[wf]['title']} (push) is not verified" in r.stdout, (
                job,
                res,
            )
    missing = copy.deepcopy(ok)
    del missing[jobs[-1]]
    assert _result(wf, missing).returncode == 1


def test_portal_deploy_smokes_fargate_with_the_studio_sign_in_required() -> None:
    jobs = _block(PORTAL_WF.read_text(), "jobs:", 0)
    smoke = _block(jobs, "smoke:", 2)
    assert "    needs: [deploy, verify]" in smoke
    assert "    uses: ./.github/workflows/ui-smoke-portal.yml" in smoke
    for line in (
        "base_url: ${{ needs.deploy.outputs.base_url }}",
        "sha: ${{ github.sha }}",
        "resolve_via: ${{ needs.deploy.outputs.resolve_via }}",
        "require_tenant_login: true",
        "pull-requests: write",
    ):
        assert line in smoke, line
    assert "secrets: inherit" not in smoke
    passed = re.search(r"^    secrets:\n((?:      \S.*\n?)+)", smoke, re.M)
    assert passed and set(re.findall(r"^\s+(\w+):", passed.group(1), re.M)) == {
        "E2EPORTALEMAIL",
        "E2EPORTALPASSWORD",
        "E2EPORTALTOTP",
    }
    deploy = _block(jobs, "deploy:", 2)
    assert "resolve_via: ${{ vars.CRM_AGENCY_CF_DOMAIN }}" in deploy and "base_url: ${{ format('https://{0}'" in deploy
    verify = _block(jobs, "verify:", 2)
    assert "resolve_via: ${{ needs.deploy.outputs.resolve_via }}" in verify


def test_ui_smoke_takes_resolve_via_and_the_login_requirement_when_called() -> None:
    on = _block(SMOKE_WF.read_text(), "on:", 0)
    call, dispatch = _block(on, "workflow_call:", 2), _block(on, "workflow_dispatch:", 2)
    for name in ("resolve_via:", "require_tenant_login:"):
        assert name in call and name in dispatch, name
    # AA-979 §4.5: no Vercel target any more; the smoke always checks the Fargate studio.
    assert "target:" not in call and "target:" not in dispatch
    assert "PORTAL_TARGET: fargate" in SMOKE_WF.read_text()
    assert re.search(r"require_tenant_login:\n.*\n\s+required: false\n\s+type: boolean\n\s+default: false", call)
    text = SMOKE_WF.read_text()
    # A hand-dispatched smoke never cancels a deploy's smoke (github.workflow is the caller's in a call).
    assert "group: ui-smoke-portal-${{ github.workflow }}-${{ inputs.base_url }}" in text


def _smoke_step() -> str:
    text = SMOKE_WF.read_text()
    step = text[
        text.index("      - name: Run the portal UI smoke") : text.index("      - name: Upload smoke artifacts")
    ]
    return _run_script(step)


_ALL = {"E2EPORTALEMAIL": "e2e@example.test", "E2EPORTALPASSWORD": "pw", "E2EPORTALTOTP": "JBSWY3DP"}


@needs_tools
@pytest.mark.parametrize(
    ("required", "missing", "code", "ran"),
    [
        ("true", "E2EPORTALEMAIL", 1, False),
        ("true", "E2EPORTALPASSWORD", 1, False),
        ("true", "E2EPORTALTOTP", 1, False),
        ("true", "", 0, True),
        ("false", "E2EPORTALTOTP", 0, True),
        ("false", "", 0, True),
    ],
)
def test_smoke_step_is_red_without_the_studio_sign_in_only_when_required(
    tmp_path: Path, required: str, missing: str, code: int, ran: bool
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "npx-ran"
    npx = bin_dir / "npx"
    npx.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    npx.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "REQUIRETENANTLOGIN": required}
    env.update({k: ("" if k == missing else v) for k, v in _ALL.items()})
    proc = subprocess.run([str(BASH), "-c", _smoke_step()], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert proc.returncode == code, proc.stdout + proc.stderr
    assert marker.exists() is ran
    if code:
        assert f"::error::{missing} is not set" in proc.stdout
