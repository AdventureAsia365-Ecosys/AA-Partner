"""AA-993 V3 (ADR 0016 D3) — the CRM web post-deploy verify (the Content studio inside it since AA-1036) is red/green,
and so is its deploy.

- ``post_deploy_verify.sh crm-web`` runs for real with the real rows file against fixture JSON (``curl`` and ``aws``
  are PATH stubs from ``test_post_deploy_verify``): all good exits 0; a stale ``git_sha``/``sha``, a wrong version, the
  API rule lost, the API not ready or ``/api/healthz`` not the merged studio each exit 1.
- ``edge_verify.sh`` runs for real against a stub ``curl`` that also answers ``%{redirect_url}``: the signed-out
  redirects, "/" landing on Content, the retired API-key sign-in URL (308, AA-979 §4.5), the studio's old legal path
  (308), a public studio page and its ``/_next`` asset are each red when wrong.
- The workflow is checked as text, like V2: ``deploy-web-dev.yml`` calls the verify after ``deploy`` (no inline smoke
  left), ``config`` fails instead of skipping, ``result`` needs every job and is red on any non-success; it calls
  ``ui-smoke-portal.yml`` with ``require_tenant_login`` and the e2e studio user's three sign-in secrets, and the smoke
  step is red when one is missing. AA-1036 R3 slice 2 retired the separate studio service: no ``crm-portal`` app, no
  Deploy Portal Dev.
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
SMOKE_WF = WORKFLOWS / "ui-smoke-portal.yml"
BASE = "https://host.test"

APPS = {
    "crm-web": {"svc": "aa-crm-dev-web", "rows": {"healthz", "api-health", "api-ready", "studio-healthz"}},
}


def _good_http(app: str) -> dict:
    api = {
        "/crm/health": {
            "status": 200,
            "body": {"status": "ok", "service": "aa-crm-api", "version": "0.4.0", "git_sha": OLD_SHA},
        },  # the API runs an older commit: still green
        "/crm/health/ready": {"status": 200, "body": {"status": "ok", "db": "ok", "schema_version": "crm-019"}},
    }
    assert app == "crm-web"
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
        },
        # AA-1036: the agency host sends /api/* to the web, whose studio BFF answers its liveness there.
        "/api/healthz": {
            "status": 200,
            "body": {"ok": True, "service": "aa-crm-web", "studio": "merged", "version": VERSION, "sha": SHA},
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


# ── rows (post_deploy_verify.sh with the real crm-web.rows) ───────────────────────────────────────────────────────


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
        # AA-1036: /api/* still reaching the old studio service (a crm_agency_studio rule not cut over), the studio not
        # merged, or an older web commit.
        ("crm-web", _set("/api/healthz", service="aa-partner-portal"), {"studio-healthz"}),
        ("crm-web", _set("/api/healthz", studio="separate"), {"studio-healthz"}),
        ("crm-web", _set("/api/healthz", sha=OLD_SHA), {"studio-healthz"}),
        ("crm-web", _status("/api/healthz", 404), {"studio-healthz"}),
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
def test_desired_zero_is_red_for_the_web(run_rows, app: str) -> None:
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
    # AA-1036: the web's own /healthz and the merged studio's /api/healthz.
    assert "|GET /healthz|" in text and '|GET /api/healthz|200|.ok == true and .service == "aa-crm-web"' in text


def test_the_merged_web_serves_the_studio_health_body_under_api() -> None:
    lib = (REPO / "web" / "lib" / "studio" / "health.ts").read_text()
    assert 'service: "aa-crm-web"' in lib and 'studio: "merged"' in lib
    assert "sha: env.GIT_SHA" in lib and "version: env.APP_VERSION" in lib
    src = (REPO / "web" / "app" / "api" / "healthz" / "route.ts").read_text()
    assert "healthBody()" in src and "export function GET" in src and "fetch(" not in src


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

WEB_ASSET = "/_next/static/chunks/main-def456.js"
STUDIO_NEXT = f"{BASE}/auth/refresh?next=%2Fportal%2Fdashboard"


def _edge_good(app: str) -> dict:
    if app == "crm-web":
        return {
            "/pipeline": {"status": 307, "location": f"{BASE}/auth/refresh?next=%2Fpipeline"},
            "/legal/terms": {"status": 307, "location": f"{BASE}/auth/refresh?next=%2Flegal%2Fterms"},
            # AA-1036: the studio inside the web on the agency host.
            "/": {"status": 307, "location": f"{BASE}/portal/dashboard"},
            "/portal/dashboard": {"status": 307, "location": STUDIO_NEXT},
            "/tenant-login": {"status": 308, "location": STUDIO_NEXT},
            "/terms": {"status": 308, "location": f"{BASE}/legal/partner-terms"},
            "/legal/partner-terms": {"status": 200, "body": f'<html><script src="{WEB_ASSET}" async></script></html>'},
            WEB_ASSET: {"status": 200, "body": "js"},
        }
    raise AssertionError(app)


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
        {
            "signin-redirect",
            "crm-terms",
            "content-home",
            "studio-signin",
            "api-key-signin-off",
            "studio-legal",
            "studio-page",
            "web-assets",
        }
        if app == "crm-web"
        else set()
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
        # AA-1033: the CRM terms answered by another service (the studio's own page) instead of the CRM web's gate.
        ("crm-web", _edge_break("crm-web", "/legal/terms", status=200, body="<html>"), {"crm-terms"}),
        # AA-1036: WEB_CONTENT_HOME off ("/" is still the CRM home behind the sign-in).
        (
            "crm-web",
            _edge_break("crm-web", "/", status=307, location=f"{BASE}/auth/refresh?next=%2F"),
            {"content-home"},
        ),
        # A studio rule still forwarding to the old studio service: its /terms page instead of the web's 308.
        ("crm-web", _edge_break("crm-web", "/terms", status=200, body="<html>"), {"studio-legal"}),
        ("crm-web", _edge_break("crm-web", "/tenant-login", status=200, body="<html>"), {"api-key-signin-off"}),
        (
            "crm-web",
            _edge_break("crm-web", "/portal/dashboard", status=307, location=f"{BASE}/tenant-login"),
            {"studio-signin"},
        ),
        ("crm-web", _edge_break("crm-web", "/legal/partner-terms", status=404, body=""), {"studio-page", "web-assets"}),
        ("crm-web", _edge_break("crm-web", WEB_ASSET, status=404, body=""), {"web-assets"}),
        # The old studio's asset prefix is not the web's: a page linking only /portal-assets is red.
        (
            "crm-web",
            _edge_break("crm-web", "/legal/partner-terms", status=200, body="<html>no assets</html>"),
            {"web-assets"},
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
@pytest.mark.parametrize("args", [(), ("crm",), ("crm-portal",), ("../x",)])
def test_edge_needs_the_web_app(args: tuple[str, ...]) -> None:
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
        "VERIFY_APP": "crm-portal",  # AA-1036: retired with the separate studio service
    }
    proc = subprocess.run([str(BASH), "-c", script], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert proc.returncode == 1 and "::error::app must be crm or crm-web" in proc.stdout


def test_verify_runs_the_edge_checks_for_the_web_even_after_a_red_row() -> None:
    text = VERIFY_WF.read_text()
    edge = text[text.index("      - name: Edge checks") :]
    assert "if: ${{ !cancelled() && steps.app.outputs.edge == 'true' }}" in edge
    assert 'run: bash .github/scripts/edge_verify.sh "$VERIFY_APP"' in edge


# ── deploy workflows (text + their config/result scripts) ──────────────────────────────────────────────────────────

DEPLOYS = {
    WEB_WF: {"jobs": ("config", "deploy", "verify", "smoke"), "app": "crm-web", "title": "Deploy Web Dev"},
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
    assert "concurrency:\n  group: deploy-web-dev\n  cancel-in-progress: false" in text


@needs_tools
@pytest.mark.parametrize("wf", list(DEPLOYS), ids=lambda p: p.name)
@pytest.mark.parametrize(
    ("role", "enabled", "code"), [("", "true", 1), ("arn", "", 1), ("arn", "false", 1), ("arn", "true", 0)]
)
def test_config_fails_instead_of_skipping(wf: Path, role: str, enabled: str, code: int) -> None:
    config = _block(_block(wf.read_text(), "jobs:", 0), "config:", 2)
    assert "GITHUB_OUTPUT" not in config and not re.search(r"^    outputs:", config, re.M)
    flag = "WEB_ENABLED"
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


def test_web_deploy_smokes_the_studio_with_its_sign_in_required() -> None:
    """AA-1036: the studio's Playwright smoke runs after the web deploy (the studio is part of the web)."""
    jobs = _block(WEB_WF.read_text(), "jobs:", 0)
    smoke = _block(jobs, "smoke:", 2)
    assert "    needs: [deploy, verify]" in smoke
    assert "    uses: ./.github/workflows/ui-smoke-portal.yml" in smoke
    for line in (
        "base_url: ${{ needs.deploy.outputs.base_url }}",
        "sha: ${{ github.sha }}",
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
    assert "base_url: ${{ vars.CRM_WEB_BASE_URL || 'https://portal.lumiguides.it.com' }}" in deploy


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


def test_the_separate_studio_app_is_retired() -> None:
    """AA-1036 R3 slice 2: no Deploy Portal Dev, no crm-portal rows; the web verify reads the merged studio."""
    assert not (WORKFLOWS / "deploy-portal-dev.yml").exists()
    assert not (REPO / ".github" / "verify" / "crm-portal.rows").exists()
    assert "crm-portal" not in VERIFY_WF.read_text().split("\non:", 1)[1]
    assert "crm-portal" not in EDGE.read_text()
