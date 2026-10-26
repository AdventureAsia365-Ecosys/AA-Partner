"""AA-979 (CRM-P2a): the tenant portal's static guards, moved with ``portal/`` from AA-CIS-App.

Ported from the portal side of the AA-CIS-App unit tests (the admin side stays there):

- test_aa885_portal_split: no admin secret or staff session code in the portal; the gate (``proxy.ts``) and the BFF
  are tenant-only; security headers; the 308 map of the old T-number slugs; the lockfile is tracked; the deploy
  workflow deploys the portal project on ``portal/**`` only and nothing else redeploys for a portal change.
- test_aa886_portal_legal_pages: ``/privacy`` and ``/terms`` are draft, noindex pages outside the gate.
- test_aa820_portal_terms_ai: ``/terms`` section 3 keeps the AI acceptable-use summary (draft v0.2).
- test_aa760_muted_contrast (portal tokens) and test_aa760_portal_calls_not_metered (the sign-in asks for a portal
  session, ``client: "portal"``, so portal page loads are not metered as API calls).
- test_aa911_bff_cross_site: every portal BFF write route refuses a cross-site request before it reads a cookie or a
  body.
- test_login_pages_no_cross_links: the tenant sign-in page does not link the staff console.
- S251 (portal UI check D7): link text uses the accent-deep token, which reads at AA in both themes (the accent itself
  is 2.3:1 on the cream page), and placeholders use muted2 in both themes.

The vendored UI-kit byte checks are in ``scripts/check_vendored_copies.py`` (``PORTAL_COPIES``). These tests only
read files; the behaviour is checked live by the portal UI smoke (``e2e/smoke-portal/``).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PORTAL = REPO / "portal"
WORKFLOWS = REPO / ".github" / "workflows"

FORBIDDEN_IN_PORTAL = ["ADMIN_SECRET", "INTERNAL_API_KEY", "x-admin-secret", "COGNITO_", "cis_admin_token"]

SECURITY_HEADERS = [
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Content-Security-Policy",
    "Permissions-Policy",
]

# AA-759: every old T-number (and flat utility) slug answers 308 to its new path.
OLD_PORTAL_SLUGS = {
    "/portal/t1-rewrite": "/portal/tours",
    "/portal/t4-pool": "/portal/tours/mine",
    "/portal/marketplace": "/portal/tours/marketplace",
    "/portal/t0-brand": "/portal/brand",
    "/portal/t7-planning": "/portal/create/social",
    "/portal/t8-angle-gate": "/portal/create/write",
    "/portal/t10-review": "/portal/content",
    "/portal/t11-publish": "/portal/content/publish",
    "/portal/t11-publish/connection": "/portal/account/integrations",
    "/portal/activity": "/portal/account/activity",
    "/portal/billing": "/portal/account/billing",
    "/portal/settings": "/portal/account/settings",
    "/portal/api": "/portal/account/api",
}


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _portal_source_files():
    for p in PORTAL.rglob("*"):
        rel = p.relative_to(PORTAL).parts
        if not p.is_file() or rel[0] in {"node_modules", ".next"}:
            continue
        if p.suffix in {".ts", ".tsx", ".mjs", ".js", ".json", ".css"} and p.name != "package-lock.json":
            yield p


# --- AA-885: the portal app ------------------------------------------------------------------------------------------


def test_portal_holds_no_admin_secret_or_staff_session() -> None:
    hits = []
    for p in _portal_source_files():
        text = p.read_text(errors="ignore")
        hits += [f"{p.relative_to(REPO)}: {word}" for word in FORBIDDEN_IN_PORTAL if word in text]
    assert hits == [], "the portal app must not carry admin secrets or staff session code:\n" + "\n".join(hits)


def test_portal_redirects_old_slugs_with_308() -> None:
    cfg = _read("portal/next.config.ts")
    assert "...OLD_SLUG_REDIRECTS.map((r) => ({ ...r, permanent: true }))" in cfg
    for source, dest in OLD_PORTAL_SLUGS.items():
        pattern = r'\{\s*source:\s*"' + re.escape(source) + r'",\s*destination:\s*"' + re.escape(dest) + r'"\s*\}'
        assert re.search(pattern, cfg), f"portal/next.config.ts: missing 308 {source} -> {dest}"
        assert (PORTAL / "app" / dest.lstrip("/") / "page.tsx").is_file(), f"no page for {dest}"
        assert not (PORTAL / "app" / source.lstrip("/") / "page.tsx").exists(), f"old page {source} still exists"


def test_portal_nav_has_no_t_number_slug() -> None:
    nav = _read("portal/app/portal/_shell/portalNav.tsx")
    assert not re.search(r'"/portal/t\d+-', nav), "portalNav.tsx still links a T-number slug"
    for dest in set(OLD_PORTAL_SLUGS.values()) - {"/portal/create/write"}:
        assert f'"{dest}"' in nav, f"portalNav.tsx ROUTES misses {dest}"


def test_portal_proxy_is_tenant_only() -> None:
    gate = _read("portal/proxy.ts")
    assert "export async function proxy" in gate
    # AA-979 P2b: the gate sees every path but the Next assets (both prefixes), the health check and the favicon, so
    # the Host guard covers the BFF and the legal pages too.
    assert 'matcher: ["/((?!_next/|portal-assets/|healthz|favicon.ico).*)"]' in gate
    route = _read("portal/app/api/tenant/[...path]/route.ts")
    assert "requireStudio" in route and "proxyPath" in route


def test_portal_sets_security_headers() -> None:
    cfg = _read("portal/next.config.ts")
    for header in SECURITY_HEADERS:
        assert f'key: "{header}"' in cfg, f"portal/next.config.ts: missing {header}"
    assert "frame-ancestors 'none'" in cfg
    assert 'source: "/(.*)"' in cfg


def test_portal_lockfile_is_tracked() -> None:
    out = subprocess.run(  # noqa: S603 — fixed argv, no user input
        ["git", "ls-files", "--error-unmatch", "portal/package-lock.json"],  # noqa: S607 — git on PATH
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, "portal/package-lock.json must be tracked (Vercel and CI run npm ci in portal/)"


def test_portal_vercel_git_deployments_stay_off() -> None:
    """Deployments come only from vercel-deploy-portal.yml (one deployer; the Hobby cap counts every deployment)."""
    assert re.search(r'"deploymentEnabled":\s*false', _read("portal/vercel.json"))


def test_portal_workflow_deploys_its_own_project_on_portal_paths_only() -> None:
    portal = (WORKFLOWS / "vercel-deploy-portal.yml").read_text()
    portal_id = re.search(r"VERCEL_PROJECT_ID:\s*(.+?)\s*(#|$)", portal, re.M)
    assert portal_id and portal_id.group(1) == "${{ vars.VERCEL_PORTAL_PROJECT_ID }}"
    assert '"portal/**"' in portal and '"web/**"' not in portal and '"api/**"' not in portal
    assert "./.github/workflows/ui-smoke-portal.yml" in portal
    # A portal change never redeploys ECS (the API or the CRM web).
    for name in ("deploy-dev.yml", "deploy-web-dev.yml"):
        assert "portal/" not in (WORKFLOWS / name).read_text(), f"{name} must not watch portal/"


def test_portal_deploy_hands_the_smoke_only_its_two_secrets() -> None:
    """No `secrets: inherit` (it would pass VERCEL_TOKEN to the smoke); the secrets sit on the smoke step only."""
    portal = (WORKFLOWS / "vercel-deploy-portal.yml").read_text()
    assert "secrets: inherit" not in portal
    passed = re.search(r"^    secrets:\n((?:      \S.*\n)+)", portal, re.M)
    assert passed, "vercel-deploy-portal.yml: the smoke call must pass an explicit secrets block"
    names = set(re.findall(r"^\s+(\w+):", passed.group(1), re.M))
    assert names == {"VERCEL_PORTAL_BYPASS_SECRET", "E2E_TENANT_API_KEY"}, names
    assert '--token "$VERCEL_TOKEN"' not in portal, "the CLI reads VERCEL_TOKEN from env; keep it off the command line"
    smoke = (WORKFLOWS / "ui-smoke-portal.yml").read_text()
    job_env = smoke[smoke.index("    env:\n") : smoke.index("    steps:\n")]
    assert "secrets." not in job_env, "ui-smoke-portal.yml: secrets belong on the smoke step, not the job env"


def test_portal_version_is_not_bumped_by_release_please() -> None:
    """The portal never reads its package version at runtime, so release-please must not edit anything in portal/."""
    cfg = json.loads(_read("release-please-config.json"))
    under_portal = [
        f["path"]
        for pkg in cfg["packages"].values()
        for f in pkg.get("extra-files", [])
        if (f["path"] if isinstance(f, dict) else f).startswith("portal/")
    ]
    assert under_portal == [], f"release-please extra-files under portal/: {under_portal}"
    assert all(not path.startswith("portal") for path in cfg["packages"]), "portal/ is not a release-please package"


def test_portal_build_reads_nothing_outside_portal() -> None:
    """The root .vercelignore drops the other apps from the upload; that is safe only while no portal import leaves
    portal/. Every ignore line is anchored, so `/api` never matches portal/app/api/ (the BFF routes)."""
    escapes = []
    for p in _portal_source_files():
        for m in re.finditer(r"""(?:from|import|require\(|url\()\s*['"]?(\.{1,2}/[^'")\s]+)""", p.read_text()):
            target = (p.parent / m.group(1)).resolve()
            if PORTAL.resolve() not in target.parents:
                escapes.append(f"{p.relative_to(REPO)}: {m.group(1)}")
    assert escapes == [], "portal imports a file outside portal/ (fix .vercelignore first):\n" + "\n".join(escapes)
    lines = [ln.strip() for ln in _read(".vercelignore").splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines, ".vercelignore is empty"
    for ln in lines:
        assert ln.startswith("/") and not ln.startswith("/portal") and "*" not in ln, f".vercelignore: {ln!r}"


def test_portal_smoke_uploads_no_auth_state() -> None:
    smoke = (WORKFLOWS / "ui-smoke-portal.yml").read_text()
    upload = smoke[smoke.index("actions/upload-artifact") : smoke.index("if-no-files-found")]
    paths = upload[upload.index("path: |") :]
    assert ".auth" not in paths and "e2e/**" not in paths
    assert "rm -rf .auth" in smoke


# --- AA-886 / AA-820: legal pages ------------------------------------------------------------------------------------


@pytest.mark.parametrize("page", ["portal/app/privacy/page.tsx", "portal/app/terms/page.tsx"])
def test_legal_page_uses_the_draft_shell_and_is_noindex(page: str) -> None:
    src = _read(page)
    assert "<LegalPage" in src, f"{page}: must render the LegalPage shell (it carries the draft banner)"
    assert re.search(r"robots:\s*\{\s*index:\s*false", src), f"{page}: a draft legal page must be noindex"


def test_legal_shell_always_renders_the_draft_banner() -> None:
    src = _read("portal/app/_legal/LegalPage.tsx")
    assert 'LEGAL_DRAFT_LABEL = "Draft — pending legal review"' in src
    assert "<DraftBanner />" in src


def test_legal_pages_are_not_redirected_and_stay_outside_the_gate() -> None:
    cfg = _read("portal/next.config.ts")
    assert not re.search(r"source:\s*\"/privacy\"", cfg)
    assert not re.search(r"source:\s*\"/terms\"", cfg)
    matcher = re.search(r"matcher:\s*\[([^\]]*)\]", _read("portal/proxy.ts"))
    assert matcher, "proxy.ts: matcher not found"
    for path in ("/privacy", "/terms"):
        assert path not in matcher.group(1), f"proxy.ts must not gate {path}"


@pytest.mark.parametrize("rel", ["portal/app/tenant-login/page.tsx", "portal/app/portal/_shell/PortalTopbar.tsx"])
def test_sign_in_form_and_top_bar_link_both_legal_pages(rel: str) -> None:
    src = _read(rel)
    assert "href={PRIVACY_PATH}" in src and "href={TERMS_PATH}" in src, f"{rel}: legal links missing"


def _ai_section() -> str:
    m = re.search(r'<LegalSection id="ai".*?</LegalSection>', _read("portal/app/terms/page.tsx"), re.S)
    assert m, 'terms/page.tsx: section id="ai" not found'
    return m.group(0)


def test_terms_ai_section_links_the_policies_and_lists_the_prohibited_uses() -> None:
    ai = _ai_section()
    assert 'href="https://www.anthropic.com/legal/aup"' in ai
    assert 'href="https://aws.amazon.com/aup/"' in ai
    assert 'title="3. AI-generated content and acceptable use"' in ai
    assert "This content was generated with AI." in ai
    assert "[to be confirmed]" not in ai
    flat = re.sub(r"\s+", " ", ai)
    for phrase in (
        "fake reviews, ratings or testimonials",
        "sockpuppet",
        "astroturfing",
        "sponsorship",
        "search engines or AI systems",
        "spam",
        "visas and entry rules",
        "qualified person reviews it",
        "content checks, limits or other safeguards",
        "labelled as AI-generated",
        "you are its publisher",
    ):
        assert phrase in flat, f"/terms §3 must mention: {phrase}"


# --- AA-760: muted text contrast and the unmetered portal session -----------------------------------------------------


def _block(theme: str) -> str:
    src = _read("portal/app/globals.css")
    start = src.index(f'[data-theme="{theme}"] {{')
    return src[start : src.index("\n}", start)]


def _token(block: str, name: str) -> str:
    m = re.search(rf"--aa-{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})\b", block)
    assert m, f"--aa-{name} not found as a #RRGGBB literal"
    return m.group(1)


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_helper_matches_known_values() -> None:
    assert round(contrast("#000000", "#FFFFFF"), 1) == 21.0
    assert round(contrast("#9099A6", "#FFFFFF"), 2) == 2.88


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("token", ["muted", "muted2"])
@pytest.mark.parametrize("surface", ["card", "bg", "line2", "neutral-bg"])
def test_muted_text_reads_at_aa_on_its_surfaces(theme: str, token: str, surface: str) -> None:
    block = _block(theme)
    fg, bg = _token(block, token), _token(block, surface)
    ratio = contrast(fg, bg)
    assert ratio >= 4.5, f"portal {theme}: --aa-{token} {fg} on --aa-{surface} {bg} is {ratio:.2f}:1 (< 4.5:1)"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_neutral_pill_text_reads_at_aa(theme: str) -> None:
    block = _block(theme)
    for fg_name in ("neutral-fg", "neutral-fg2"):
        fg, bg = _token(block, fg_name), _token(block, "neutral-bg")
        assert contrast(fg, bg) >= 4.5, f"portal {theme}: --aa-{fg_name} {fg} on --aa-neutral-bg {bg}"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_muted2_stays_the_quieter_tone(theme: str) -> None:
    block = _block(theme)
    card = _token(block, "card")
    assert contrast(_token(block, "muted"), card) >= contrast(_token(block, "muted2"), card)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("surface", ["card", "bg", "accent-tint"])
def test_link_text_token_reads_at_aa(theme: str, surface: str) -> None:
    block = _block(theme)
    fg, bg = _token(block, "accent-deep"), _token(block, surface)
    ratio = contrast(fg, bg)
    assert ratio >= 4.5, f"portal {theme}: --aa-accent-deep {fg} on --aa-{surface} {bg} is {ratio:.2f}:1 (< 4.5:1)"


# Decorative or non-text uses of the accent colour that may stay: list bullets, the spinner icon and icons on the
# dark panel (which is the same dark navy in both themes).
_GOLD_OK = ("•", "Loader2", "T.darkPanel")


def test_portal_text_never_uses_the_bare_accent_colour() -> None:
    pattern = re.compile(r"color:\s*(?:[^,}]*\?\s*)?(?:T\.gold|K\.accent)\b(?!Deep|Tint|Soft|Border)")
    offenders = []
    for path in sorted((PORTAL / "app").rglob("*.tsx")):
        if "_kit" in path.parts or "_brand" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not any(ok in line for ok in _GOLD_OK):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()[:120]}")
    assert not offenders, "use T.goldDeep / K.accentDeep for text (S251 D7):\n" + "\n".join(offenders)


def test_placeholders_use_muted2_in_both_themes() -> None:
    css = _read("portal/app/globals.css")
    m = re.search(r"(?m)^input::placeholder,\s*\ntextarea::placeholder,[^{]*\{([^}]*)\}", css)
    assert m, "globals.css needs a theme-independent input/textarea ::placeholder rule"
    assert "var(--aa-muted2)" in m.group(1) and "opacity: 1" in m.group(1)


def test_brand_ts_matches_the_light_tokens() -> None:
    src = _read("portal/app/_brand/tokens.ts")
    light = _block("light")
    for name in ("muted", "muted2"):
        m = re.search(rf'^\s*{name}:\s*"(#[0-9A-Fa-f]{{6}})"', src, re.M)
        assert m, f"BRAND.{name} not found"
        assert m.group(1).upper() == _token(light, name).upper(), name


def test_portal_sign_in_bff_requests_a_portal_session() -> None:
    src = _read("portal/app/api/auth/tenant-login/route.ts")
    assert 'JSON.stringify({ api_key: apiKey, client: "portal" })' in src


def test_billing_page_says_what_api_calls_count() -> None:
    src = _read("portal/app/portal/_components/PlaceholderTabs.tsx")
    assert "Using this" in src and "portal, including the API playground, is not counted" in src


# --- AA-911: cross-site writes ---------------------------------------------------------------------------------------

WRITE_EXPORT = re.compile(r"export\s+(?:const\s+|(?:async\s+)?function\s+)(POST|PUT|PATCH|DELETE)\b")
REEXPORT = re.compile(r"export\s*\{[^}]*\bas\s+(POST|PUT|PATCH|DELETE)\b")
# AA-979 P2b (critic C2): the studio's own guard (lib/writeGuard.ts) hands the vendored rule the PUBLIC origin, so a
# same-origin POST behind CloudFront -> ALB is not refused for the standalone server's bind address.
GUARD_CALL = "refuseForeignWrite(req)"
GUARD_IMPORT = 'from "@/lib/writeGuard";'
FIRST_WORK = re.compile(
    r"requireAdmin\(|requireTenant\(|requireStudio\(|partnerSession\(|\.cookies\.get\(|"
    r"req\.(?:text|json|arrayBuffer|formData)\(|handleRefreshKeepAlive\("
)
# Sign-in reads no session cookie, so a cross-site POST cannot act as a signed-in user (AA-911 notes).
EXEMPT = {"portal/app/api/auth/tenant-login/route.ts"}


def _write_routes() -> list[Path]:
    return [p for p in sorted((PORTAL / "app" / "api").rglob("route.ts")) if WRITE_EXPORT.search(p.read_text())]


WRITE_ROUTES = _write_routes()


def _strip_comments(src: str) -> str:
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def _function_body(src: str, header: re.Match) -> str:
    rest = src[header.end() :]
    nxt = re.search(r"\n(?:export\s|async\s+function\s|function\s|const\s)", rest)
    return rest[: nxt.start()] if nxt else rest


def _write_handlers(src: str) -> list[tuple[str, str]]:
    assert not REEXPORT.search(src), "write handler exported through `export { … as … }`: export it directly"
    handlers = [
        (m.group(1), _function_body(src, m))
        for m in re.finditer(r"export\s+(?:async\s+)?function\s+(POST|PUT|PATCH|DELETE)\s*\(", src)
    ]
    aliased = {m.group(2) for m in re.finditer(r"export\s+const\s+(POST|PUT|PATCH|DELETE)\s*=\s*(\w+)\s*;", src)}
    for name in sorted(aliased):
        m = re.search(rf"async\s+function\s+{name}\s*\(", src)
        assert m, f"handler {name} not found"
        handlers.append((name, _function_body(src, m)))
    return handlers


def test_write_routes_found() -> None:
    rels = {str(p.relative_to(REPO)) for p in WRITE_ROUTES}
    assert {"portal/app/api/tenant/[...path]/route.ts", "portal/app/api/playground/proxy/route.ts"} <= rels


@pytest.mark.parametrize("route", WRITE_ROUTES, ids=lambda p: str(p.relative_to(REPO)))
def test_every_write_handler_refuses_cross_site_first(route: Path) -> None:
    rel = str(route.relative_to(REPO))
    if rel in EXEMPT:
        return
    src = _strip_comments(route.read_text())
    assert GUARD_IMPORT in src, f"{rel}: write route without {GUARD_IMPORT}"
    handlers = _write_handlers(src)
    assert handlers, f"{rel}: no write handler found"
    for name, body in handlers:
        assert GUARD_CALL in body, f"{rel}: {name} does not call {GUARD_CALL}"
        first_work = FIRST_WORK.search(body)
        assert first_work, f"{rel}: {name}: no auth/body read found"
        assert body.index(GUARD_CALL) < first_work.start(), (
            f"{rel}: {name}: {GUARD_CALL} must run before {first_work.group(0)!r}"
        )


# --- S245: no cross link between the sign-in pages --------------------------------------------------------------------


def test_tenant_login_has_no_link_to_the_staff_console() -> None:
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", _read("portal/app/tenant-login/page.tsx"), flags=re.S)
    for needle in ("STAFF_LOGIN_URL", "ADMIN_BASE_URL", "aa-cis.lumiguides", "Staff login", 'href="/login"'):
        assert needle not in text, needle


# --- AA-979 P2b: the studio on Fargate behind the agency host ---------------------------------------------------------


def test_no_route_calls_the_vendored_cross_site_guard_directly() -> None:
    """The vendored rule compares with the request's own origin, which on Fargate is the bind address (critic C2)."""
    offenders = [
        str(p.relative_to(REPO))
        for p in (PORTAL / "app").rglob("route.ts")
        if "refuseCrossSiteWrite" in _strip_comments(p.read_text())
    ]
    assert offenders == []
    guard = _read("portal/lib/writeGuard.ts")
    assert "cfg.publicOrigin || req.nextUrl.origin" in guard


@pytest.mark.parametrize("name", ["session.ts", "seal.ts"])
def test_the_studio_reads_the_crm_web_session_with_byte_copies(name: str) -> None:
    """portal/lib/{session,seal}.ts are byte copies of web/lib/ (one sealed cookie format, read only by the studio)."""
    assert (PORTAL / "lib" / name).read_bytes() == (REPO / "web" / "lib" / name).read_bytes(), (
        f"portal/lib/{name} differs from web/lib/{name}: copy it byte for byte (cp)"
    )


def test_the_studio_never_writes_the_crm_session() -> None:
    for p in _portal_source_files():
        text = _strip_comments(p.read_text())
        for writer in ("sessionCookie(", "refreshCookie(", "txCookie(", "clearedCookies("):
            if p.name == "session.ts":
                continue
            assert writer not in text, f"{p.relative_to(REPO)} writes the CRM web session ({writer})"


def test_the_fargate_build_is_standalone_with_its_own_asset_prefix() -> None:
    cfg = _read("portal/next.config.ts")
    assert 'output: "standalone" as const' in cfg and 'ASSET_PREFIX = "/portal-assets"' in cfg
    assert 'process.env.PORTAL_STANDALONE === "1"' in cfg
    docker = _read("portal/Dockerfile")
    assert "PORTAL_STANDALONE=1" in docker
    assert """CMD ["node", "-e", "process.env.HOSTNAME='0.0.0.0'; require('./server.js')"]""" in docker  # S249
    assert "USER 10001" in docker and ".npmrc" not in docker.split("COPY package.json", 1)[1].split("\n", 1)[0]


def test_healthz_is_public_and_outside_the_gate() -> None:
    route = _read("portal/app/healthz/route.ts")
    assert "export function GET" in route and "fetch(" not in route  # no dependency: a CIS outage keeps the target
    assert "healthz" in _read("portal/proxy.ts").split("matcher:", 1)[1]


def test_api_key_sign_in_only_behind_its_flag() -> None:
    route = _read("portal/app/api/auth/tenant-login/route.ts")
    assert "if (!config().apiKeySignIn)" in route and "status: 404" in route
    assert 'viewerKey(req.headers.get("cloudfront-viewer-address"))' in route  # critic S2
    gate = _read("portal/proxy.ts")
    assert "cfg.apiKeySignIn" in gate
    assert 'apiKeySignIn: text(env, "PORTAL_API_KEY_SIGNIN").toLowerCase() !== "false"' in _read("portal/lib/config.ts")


def test_the_gate_keeps_api_key_sessions_before_the_partner_path() -> None:
    """Critic C3: an API-key session that existed at the DNS flip survives it; the partner path comes second."""
    gate = _strip_comments(_read("portal/proxy.ts"))
    api_key = gate.index("!isPartnerToken(token) && cfg.apiKeySignIn")
    partner = gate.index("if (partner) {")
    refresh = gate.index("if (cfg.partnerSignIn) return toPartnerSignIn(request);")
    assert api_key < partner < refresh
    assert "publicUrl(" in gate and "request.url)" in gate and "new URL(LOGIN_PATH, request.url)" not in gate


def test_portal_fargate_deploy_watches_the_portal_only() -> None:
    deploy = (WORKFLOWS / "deploy-portal-dev.yml").read_text()
    assert '"portal/**"' in deploy and '"web/**"' not in deploy and '"api/**"' not in deploy
    assert "CRM_PORTAL_ENABLED" in deploy and "aa-crm-dev-portal" in deploy
    assert "--connect-to" in deploy  # the smoke reaches the agency distribution before the DNS cutover
    ci = (WORKFLOWS / "ci.yml").read_text()
    assert "name: Portal Docker Build Check" in ci and "/portal-assets/_next/static/" in ci


def test_sentry_sends_no_personal_data() -> None:
    for name in ("portal/instrumentation.ts", "portal/instrumentation-client.ts"):
        src = _read(name)
        assert "beforeSend: scrubEvent" in src and "userInfo: false" in src and "cookies: false" in src, name
        assert "tracesSampleRate: 0" in src, name
