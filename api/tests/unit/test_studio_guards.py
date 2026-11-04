"""AA-1036 (R3 slice 1, root ADR 0019 d6): the Content studio's static guards on its new home, the one agency app.

``test_portal_guards.py`` keeps guarding ``portal/`` until R3 slice 2 deletes it; these carry the same rules over to
the studio inside ``web/`` (``app/(studio)/``, ``app/api/``, ``app/_kit/local/studio/``, ``lib/studio/``):

- no admin secret or staff session code in the studio parts; one session: no byte copy of ``session.ts`` /
  ``seal.ts`` (the studio reads the app's own ``lib/session.ts``) and no studio code writes the session cookie;
- no people-facing API-key sign-in: no ``/tenant-login`` page or BFF, ``/tenant-login*`` answers 308 in the gate,
  only ``amr=partner`` tokens are used; the gate order host -> /staff -> /c -> /portal -> CRM;
- the old T-number slugs and the old legal paths answer 308 (``next.config.ts``) to pages that exist;
- the draft legal pages are noindex, render the draft shell, stay outside the session gate, and the AI section of the
  partner terms still names the register providers and links the policies;
- every studio BFF write route refuses a cross-site request before it reads a cookie or a body;
- studio cookies are bound to the live partner sign-in (``cis_partner_ref``), Sentry sends no personal data, and the
  studio's links to the CRM are plain ``<a>`` (a full page load: the studio stylesheet never stays on a CRM page).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_portal_guards import (
    FIRST_WORK,
    OLD_PORTAL_SLUGS,
    POLICY_HREFS,
    PROVIDER_PHRASES,
    WRITE_EXPORT,
    WRITING_FALLBACK_PHRASES,
    _strip_comments,
    _write_handlers,
)

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "web"
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
    m = re.search(r'<LegalSection id="ai".*?</LegalSection>', _read(rel), re.S)
    assert m, f'{rel}: section id="ai" not found'
    return re.sub(r"\s+", " ", m.group(0))


def test_partner_terms_ai_section_keeps_the_schedule_and_the_providers() -> None:
    ai = _section("web/app/(studio)/legal/partner-terms/page.tsx")
    assert "AI_PROVIDER_POLICIES.map" in ai and "{AI_SCHEDULE_VERSION}" in ai
    assert 'from "@/lib/aiTerms"' in _read("web/app/(studio)/legal/partner-terms/page.tsx")  # one copy (the CRM's)
    assert re.findall(r'href: "([^"]+)"', _read("web/lib/aiTerms.ts")) == list(POLICY_HREFS)
    for phrase in (*PROVIDER_PHRASES, *WRITING_FALLBACK_PHRASES):
        assert phrase in ai, f"/legal/partner-terms §3 must name: {phrase}"
    privacy = _section("web/app/(studio)/legal/privacy/page.tsx")
    for phrase in (*PROVIDER_PHRASES, "Jev"):
        assert phrase in privacy, f"/legal/privacy §5 must name: {phrase}"


def _write_routes() -> list[Path]:
    return [p for p in sorted((WEB / "app" / "api").rglob("route.ts")) if WRITE_EXPORT.search(p.read_text())]


WRITE_ROUTES = _write_routes()


def test_write_routes_found() -> None:
    rels = {str(p.relative_to(WEB)) for p in WRITE_ROUTES}
    assert {
        "app/api/tenant/[...path]/route.ts",
        "app/api/playground/proxy/route.ts",
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
