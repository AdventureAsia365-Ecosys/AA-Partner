"""AA-1036 (R3 slice 1, root ADR 0019 d6): the Content studio's static guards on its new home, the one agency app.

These carry the rules of the former ``test_portal_guards.py`` (deleted with ``portal/`` in R3 slice 2) over to the
studio inside ``web/`` (``app/(studio)/``, ``app/api/``, ``app/_kit/local/studio/``, ``lib/studio/``):

- no admin secret or staff session code in the studio parts; one session: no byte copy of ``session.ts`` /
  ``seal.ts`` (the studio reads the app's own ``lib/session.ts``) and no studio code writes the session cookie;
- no people-facing API-key sign-in: no ``/tenant-login`` page or BFF, ``/tenant-login*`` answers 308 in the gate,
  only ``amr=partner`` tokens are used; the gate order host -> /staff -> /c -> /portal -> CRM; R3 slice 3: no tenant
  API-key code at all (no CIS ``/auth/tenant-login`` call, no API key card, no API page);
- the old T-number slugs and the old legal paths answer 308 (``next.config.ts``) to pages that exist;
- the draft legal pages are noindex, render the draft shell, stay outside the session gate, and the AI section of the
  partner terms still names the register providers and links the policies;
- every studio BFF write route refuses a cross-site request before it reads a cookie or a body;
- studio cookies are bound to the live partner sign-in (``cis_partner_ref``), Sentry sends no personal data, and the
  studio's links to the CRM are plain ``<a>`` (a full page load: the studio stylesheet never stays on a CRM page);
- R3 slice 2: the studio smoke (after Deploy Web Dev) gets only the e2e studio user's sign-in and uploads no auth
  state; the studio's text colours read at AA (kit tokens, the accent-deep link token, muted2 placeholders).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "web"
WORKFLOWS = REPO / ".github" / "workflows"

# AA-759: every old T-number (and flat utility) slug answers 308 to its new path.
OLD_PORTAL_SLUGS = {
    "/portal/t1-rewrite": "/portal/tours",
    "/portal/t4-pool": "/portal/tours/mine",
    # AA-671 — the Tour overview page is the Marketplace view ?view=mine (one page with the catalog).
    "/portal/marketplace": "/portal/tours?view=mine",
    "/portal/tours/marketplace": "/portal/tours?view=mine",
    "/portal/t0-brand": "/portal/brand",
    "/portal/t7-planning": "/portal/create/social",
    "/portal/t8-angle-gate": "/portal/create/write",
    "/portal/t10-review": "/portal/content",
    "/portal/t11-publish": "/portal/content/publish",
    "/portal/t11-publish/connection": "/portal/account/integrations",
    "/portal/activity": "/portal/account/activity",
    "/portal/billing": "/portal/account/billing",
    "/portal/settings": "/portal/account/settings",
    "/portal/api": "/portal/account/settings",
    "/portal/account/api": "/portal/account/settings",  # AA-1036: the API page went with the tenant API keys
}

# AA-820 / S262 / S264: the AI section of the partner terms links the providers' policies and names the AI Use
# Register's providers and the writing fallback. S271 (register v1.11, route control version 6): Claude goes to the
# Anthropic API first, so the pages name the direct APIs as providers that may process the content, never "only as a
# fallback", and say the order is AA's setting.
POLICY_HREFS = (
    "https://www.anthropic.com/legal/aup",
    "https://openai.com/policies/usage-policies/",
    "https://aws.amazon.com/aup/",
)
PROVIDER_PHRASES = (
    "Claude",
    "Amazon Bedrock",
    "OpenAI models",
    "Anthropic API",
    "OpenAI API",
    "may be processed by any of them",
    "Claude through the Anthropic API first and through Amazon Bedrock (Amazon Web Services) second",
    "through Amazon Bedrock or the OpenAI API",
    "Adventure Asia sets the order in which these providers are used and may change",
    "Amazon Nova",  # S263: the acc2 Bedrock fallback of the review / check steps (CIS migrations 331-333)
    "Meta Llama",
)
WRITING_FALLBACK_PHRASES = (
    "as a last resort",
    "an OpenAI model through the OpenAI API",
)
# S271: wording of the route before version 6 that must not come back (the direct APIs are not only a fallback).
STALE_ROUTE_PHRASES = (
    "may be used as a fallback",
    "directly only as a fallback",
    "If Amazon Bedrock is unavailable",
    "When Claude is unavailable on Amazon Bedrock",
)

# AA-911: write handlers and the work a handler must not do before the cross-site guard.
WRITE_EXPORT = re.compile(r"export\s+(?:const\s+|(?:async\s+)?function\s+)(POST|PUT|PATCH|DELETE)\b")
REEXPORT = re.compile(r"export\s*\{[^}]*\bas\s+(POST|PUT|PATCH|DELETE)\b")
FIRST_WORK = re.compile(
    r"requireAdmin\(|requireTenant\(|requireStudio\(|partnerSession\(|\.cookies\.get\(|"
    r"req\.(?:text|json|arrayBuffer|formData)\(|handleRefreshKeepAlive\("
)


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


STUDIO_DIRS = [
    WEB / "app" / "(studio)",
    WEB / "app" / "api",
    WEB / "app" / "_kit" / "local" / "studio",
    WEB / "lib" / "studio",
]
FORBIDDEN = ["ADMIN_SECRET", "INTERNAL_API_KEY", "x-admin-secret", "cis_admin_token", "PORTAL_API_KEY_SIGNIN"]
GUARD_CALL = re.compile(r"refuseForeignWrite\((?:cfg|requestConfig\(req\)), req\)")
GUARD_IMPORT = 'from "@/lib/writeGuard";'


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _studio_files() -> list[Path]:
    out: list[Path] = []
    for root in STUDIO_DIRS:
        out += [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in {".ts", ".tsx", ".css"}]
    return out


def test_the_studio_parts_exist() -> None:
    files = {str(p.relative_to(WEB)) for p in _studio_files()}
    assert len(files) > 90
    assert "app/(studio)/layout.tsx" in files and "app/(studio)/portal/layout.tsx" in files
    assert "lib/studio/auth-server.ts" in files and "app/api/tenant/[...path]/route.ts" in files


def test_the_studio_holds_no_admin_secret_or_api_key_sign_in() -> None:
    hits = []
    for p in _studio_files():
        text = p.read_text(errors="ignore")
        hits += [f"{p.relative_to(REPO)}: {w}" for w in FORBIDDEN if w in text]
    assert hits == []
    assert not (WEB / "app" / "tenant-login").exists() and not (WEB / "app" / "api" / "auth" / "tenant-login").exists()


# AA-1036 R3 slice 3 (root ADR 0019 d6): no tenant API keys. CIS answers POST /auth/tenant-login with 410 and has no
# GET /v1/account/api-key; the studio must not call either, show a key card or document an X-API-Key header.
RETIRED_KEY_PATHS = ["/auth/tenant-login", "/v1/account/api-key", "X-API-Key", "api-key-card", "ApiKeyCard"]


def test_the_studio_has_no_tenant_api_key_code() -> None:
    hits = []
    for p in _studio_files():
        text = _strip_comments(p.read_text(errors="ignore"))
        hits += [f"{p.relative_to(REPO)}: {w}" for w in RETIRED_KEY_PATHS if w in text]
    assert hits == []
    assert not (WEB / "app" / "(studio)" / "portal" / "account" / "api").exists(), "the API page went with the keys"
    nav = _strip_comments(_read("web/app/_kit/local/studio/portalNav.tsx"))
    assert "/portal/account/api" not in nav


def test_one_session_no_byte_copies() -> None:
    for name in ("session.ts", "seal.ts", "config.ts", "csp.ts", "writeGuard.ts", "sameOrigin.ts"):
        assert not (WEB / "lib" / "studio" / name).exists(), f"web/lib/studio/{name}: use web/lib/{name}"
    for p in _studio_files():
        text = _strip_comments(p.read_text())
        for writer in ("sessionCookie(", "refreshCookie(", "txCookie(", "clearedCookies("):
            assert writer not in text, f"{p.relative_to(REPO)} writes the agency session ({writer})"


def test_the_gate_order_and_the_retired_api_key_sign_in() -> None:
    gate = _strip_comments(_read("web/proxy.ts"))
    body = gate[gate.index("export function gate(") :]
    order = [
        body.index('if (pathname === "/healthz")'),
        body.index('if (kind === "unknown") return notFound();'),
        body.index("if (clientOnlyPath(pathname) && !localDev) return notFound();"),
        body.index("if (isStaffPath(pathname)) return staffGate(request, cfg);"),
        body.index("if (isApiKeySignIn(pathname))"),
        body.index("if (isStudioBff(pathname))"),
        body.index("accessValid(readSession(cfg, request.cookies))"),
    ]
    assert order == sorted(order)
    assert "encodeURIComponent(STUDIO_HOME_PATH)}`, 308)" in body
    assert "isStudioPage(request.nextUrl.pathname)) return studioGate(request);" in gate


def test_only_partner_tokens_bound_to_the_live_sign_in_are_used() -> None:
    gate = _strip_comments(_read("web/proxy.ts"))
    step = gate[gate.index("export async function studioGate") :]
    assert "const ref = await partnerRef(session.iss, session.sub);" in step
    assert "if (bound && token && isPartnerToken(token) && !needsRenewal(token))" in step
    assert 'const chosen = bound ? (request.cookies.get("cis_tenant_id")?.value ?? null) : null;' in step
    require = _strip_comments(_read("web/lib/studio/auth-server.ts"))
    require = require[require.index("export async function requireStudio") :]
    assert "if (!token || !isPartnerToken(token) || !bound || needsRenewal(token))" in require
    assert "studioCookies(result.session, ref)" in require
    assert "studioCookies(result.session, ref)" in _read("web/app/api/auth/tenant-select/route.ts")
    assert '"cis_partner_ref"' in _read("web/lib/studioCookies.ts")


def test_old_slugs_and_legal_paths_redirect_with_308_to_real_pages() -> None:
    cfg = _read("web/next.config.ts")
    assert "STUDIO_REDIRECTS.map((r) => ({ ...r, permanent: true }))" in cfg
    moved = {**OLD_PORTAL_SLUGS, "/terms": "/legal/partner-terms", "/privacy": "/legal/privacy"}
    for source, dest in moved.items():
        pattern = r'\{\s*source:\s*"' + re.escape(source) + r'",\s*destination:\s*"' + re.escape(dest) + r'"\s*\}'
        assert re.search(pattern, cfg), f"web/next.config.ts: missing 308 {source} -> {dest}"
        page = WEB / "app" / "(studio)" / dest.split("?", 1)[0].lstrip("/") / "page.tsx"
        assert page.is_file(), f"no page for {dest}"
        assert not (WEB / "app" / "(studio)" / source.lstrip("/") / "page.tsx").exists(), f"old page {source} exists"


def test_studio_nav_has_no_t_number_slug() -> None:
    nav = _read("web/app/_kit/local/studio/portalNav.tsx")
    assert not re.search(r'"/portal/t\d+-', nav)


LEGAL_PAGES = ["web/app/(studio)/legal/privacy/page.tsx", "web/app/(studio)/legal/partner-terms/page.tsx"]


@pytest.mark.parametrize("page", LEGAL_PAGES)
def test_legal_page_uses_the_draft_shell_and_is_noindex(page: str) -> None:
    src = _read(page)
    assert "<LegalPage" in src
    assert re.search(r"robots:\s*\{\s*index:\s*false", src)


def test_legal_pages_stay_outside_the_gate_and_the_top_bar_links_them() -> None:
    hosts = _read("web/lib/hosts.ts")
    public = hosts[hosts.index("export const PUBLIC_PARTNER_PATHS") :]
    public = public[: public.index("] as const;")]
    assert '"/legal/partner-terms"' in public and '"/legal/privacy"' in public and '"/legal/terms"' not in public
    legal = _read("web/lib/studio/legal.ts")
    assert 'TERMS_PATH = "/legal/partner-terms"' in legal and 'PRIVACY_PATH = "/legal/privacy"' in legal
    top = _read("web/app/_kit/local/studio/PortalTopbar.tsx")
    assert "href={PRIVACY_PATH}" in top and "href={TERMS_PATH}" in top
    assert 'from "@/lib/studio/legal"' in top


def _section(rel: str) -> str:
    """The English §AI of a legal document (AA-1036 R3: the text lives in <page>.messages.ts, English first)."""
    src = _read(rel)
    english = src[: src.index("const VI: LegalDoc")]
    m = re.search(r'id: "ai".*?(?=\{ id: ")', english, re.S)
    assert m, f'{rel}: section id: "ai" not found'
    return re.sub(r"\s+", " ", m.group(0))


TERMS_TEXT = "web/app/(studio)/legal/partner-terms/terms.messages.ts"
PRIVACY_TEXT = "web/app/(studio)/legal/privacy/privacy.messages.ts"


def test_partner_terms_ai_section_keeps_the_schedule_and_the_providers() -> None:
    ai = _section(TERMS_TEXT)
    assert "policyList(" in ai and "${AI_SCHEDULE_VERSION}" in ai
    assert 'from "@/lib/aiTerms"' in _read(TERMS_TEXT)  # one copy (the CRM's)
    assert re.findall(r'href: "([^"]+)"', _read("web/lib/aiTerms.ts")) == list(POLICY_HREFS)
    for phrase in (*PROVIDER_PHRASES, *WRITING_FALLBACK_PHRASES):
        assert phrase in ai, f"/legal/partner-terms §3 must name: {phrase}"
    privacy = _section(PRIVACY_TEXT)
    for phrase in (*PROVIDER_PHRASES, *WRITING_FALLBACK_PHRASES, "Jev"):
        assert phrase in privacy, f"/legal/privacy §5 must name: {phrase}"


def test_studio_legal_pages_drop_the_pre_v6_fallback_wording() -> None:
    """S271: since route control version 6 the direct APIs are providers, not a fallback (EN and VI, whole page)."""
    for rel in (TERMS_TEXT, PRIVACY_TEXT):
        text = re.sub(r"\s+", " ", _read(rel))
        body = text[text.index("const EN: LegalDoc") :]
        for phrase in STALE_ROUTE_PHRASES:
            assert phrase not in body, f"{rel}: stale route wording: {phrase}"
        assert "làm phương án dự phòng" not in body and "chỉ gọi trực tiếp khi dự phòng" not in body, rel
    privacy = re.sub(r"\s+", " ", _read(PRIVACY_TEXT))
    assert "directly through the Anthropic API and the OpenAI API, and through Amazon Bedrock" in privacy
    frame = _read("web/app/(studio)/_legal/legal.messages.ts")
    assert 'version: "Draft v0.5"' in frame and 'version: "Bản nháp v0.5"' in frame


def _write_routes() -> list[Path]:
    return [p for p in sorted((WEB / "app" / "api").rglob("route.ts")) if WRITE_EXPORT.search(p.read_text())]


WRITE_ROUTES = _write_routes()


def test_write_routes_found() -> None:
    rels = {str(p.relative_to(WEB)) for p in WRITE_ROUTES}
    assert {
        "app/api/tenant/[...path]/route.ts",
        "app/api/auth/tenant-logout/route.ts",
        "app/api/auth/tenant-select/route.ts",
    } <= rels


@pytest.mark.parametrize("route", WRITE_ROUTES, ids=lambda p: str(p.relative_to(WEB)))
def test_every_studio_write_handler_refuses_cross_site_first(route: Path) -> None:
    src = _strip_comments(route.read_text())
    assert GUARD_IMPORT in src
    handlers = _write_handlers(src)
    assert handlers
    for name, body in handlers:
        call = GUARD_CALL.search(body)
        assert call, f"{route}: {name} does not call refuseForeignWrite with the request's public origin"
        first = FIRST_WORK.search(body)
        assert first and call.start() < first.start(), f"{route}: {name}: the guard must run first"


def test_sentry_sends_no_personal_data() -> None:
    for name in ("web/instrumentation.ts", "web/instrumentation-client.ts"):
        src = _read(name)
        assert "beforeSend: scrubEvent" in src and "userInfo: false" in src and "cookies: false" in src, name
        assert "tracesSampleRate: 0" in src, name


def test_links_between_the_studio_and_the_crm_are_full_page_loads() -> None:
    sidebar = _strip_comments(_read("web/app/_kit/local/studio/PortalSidebar.tsx"))
    apps = sidebar[sidebar.index("function StudioApps") :]
    apps = apps[: apps.index("\nfunction NavItem")]
    assert "<a\n" in apps and "<Link" not in apps
    shell = _strip_comments(_read("web/app/_kit/local/CrmShell.tsx"))
    assert '<a href={contentStudioHref} data-crm="nav-item" data-testid="crm-studio-link">' in shell
    css = _read("web/app/(studio)/studio.css")
    assert '@import "tailwindcss" source(none);' in css
    assert "studio.css" in _read("web/app/(studio)/layout.tsx") and "studio.css" not in _read("web/app/layout.tsx")


# --- R3 slice 2: the studio smoke runs after Deploy Web Dev -----------------------------------------------------------


def test_studio_smoke_gets_only_the_studio_sign_in() -> None:
    """No `secrets: inherit`; the web deploy passes the e2e studio user's three values, on the smoke step only."""
    deploy = (WORKFLOWS / "deploy-web-dev.yml").read_text()
    assert "secrets: inherit" not in deploy
    passed = re.search(r"^    secrets:\n((?:      \S.*\n)+)", deploy, re.M)
    assert passed, "deploy-web-dev.yml: the smoke call must pass an explicit secrets block"
    names = set(re.findall(r"^\s+(\w+):", passed.group(1), re.M))
    assert names == {"E2EPORTALEMAIL", "E2EPORTALPASSWORD", "E2EPORTALTOTP"}, names
    smoke = (WORKFLOWS / "ui-smoke-portal.yml").read_text()
    job_env = smoke[smoke.index("    env:\n") : smoke.index("    steps:\n")]
    assert "secrets." not in job_env, "ui-smoke-portal.yml: secrets belong on the smoke step, not the job env"
    assert "PORTAL_TARGET: fargate" in smoke


def test_studio_smoke_uploads_no_auth_state() -> None:
    smoke = (WORKFLOWS / "ui-smoke-portal.yml").read_text()
    upload = smoke[smoke.index("actions/upload-artifact") : smoke.index("if-no-files-found")]
    paths = upload[upload.index("path: |") :]
    assert ".auth" not in paths and "e2e/**" not in paths
    assert "rm -rf .auth" in smoke


def test_no_workflow_or_dependabot_entry_names_the_retired_studio_app() -> None:
    """R3 slice 2: portal/ and its deploy are gone; nothing builds, deploys or updates them any more."""
    assert not (REPO / "portal").exists()
    assert not (WORKFLOWS / "deploy-portal-dev.yml").exists()
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        text = wf.read_text()
        assert "working-directory: portal" not in text and "aa-crm-dev-portal" not in text, wf.name
    assert 'directory: "/portal"' not in (REPO / ".github" / "dependabot.yml").read_text()


# --- S251 / AA-760: the studio's text reads at AA (kit tokens of the one agency app) ------------------------------


def _theme_block(theme: str) -> str:
    src = (WEB / "app" / "_kit" / "core" / "tokens.css").read_text(encoding="utf-8")
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
    block = _theme_block(theme)
    fg, bg = _token(block, token), _token(block, surface)
    ratio = contrast(fg, bg)
    assert ratio >= 4.5, f"{theme}: --aa-{token} {fg} on --aa-{surface} {bg} is {ratio:.2f}:1 (< 4.5:1)"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("surface", ["card", "bg", "accent-tint"])
def test_link_text_token_reads_at_aa(theme: str, surface: str) -> None:
    block = _theme_block(theme)
    fg, bg = _token(block, "accent-deep"), _token(block, surface)
    ratio = contrast(fg, bg)
    assert ratio >= 4.5, f"{theme}: --aa-accent-deep {fg} on --aa-{surface} {bg} is {ratio:.2f}:1 (< 4.5:1)"


# Decorative or non-text uses of the accent colour that may stay: list bullets, the spinner icon and icons on the
# dark panel (which is the same dark navy in both themes; K.sideBg since AA-1008 retired the studio's T alias object).
_GOLD_OK = ("•", "Loader2", "K.sideBg")


def test_studio_text_never_uses_the_bare_accent_colour() -> None:
    pattern = re.compile(r"color:\s*(?:[^,}]*\?\s*)?(?:T\.gold|K\.accent)\b(?!Deep|Tint|Soft|Border)")
    offenders = []
    for path in sorted((WEB / "app" / "(studio)").rglob("*.tsx")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not any(ok in line for ok in _GOLD_OK):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()[:120]}")
    assert not offenders, "use T.goldDeep / K.accentDeep for text (S251 D7):\n" + "\n".join(offenders)


def test_studio_placeholders_use_muted2_in_both_themes() -> None:
    css = (WEB / "app" / "(studio)" / "studio.css").read_text(encoding="utf-8")
    m = re.search(r"(?m)^input::placeholder,\s*\ntextarea::placeholder,[^{]*\{([^}]*)\}", css)
    assert m, "studio.css needs a theme-independent input/textarea ::placeholder rule"
    assert "var(--aa-muted2)" in m.group(1) and "opacity: 1" in m.group(1)
