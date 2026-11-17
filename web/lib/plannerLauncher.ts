// web/lib/plannerLauncher.ts — AA-1115 (root ADR 0019 d10, plan entitlement tp.launcher): the Trip planner launcher an
// agency pastes on its own website. A plain link or a link styled as a button that opens the agency's hosted planner
// (its TripPlanner /{slug}/plan page, or its own planner domain when it has one) in a new tab. No iframe and no script:
// the planner keeps its CSP (frame-ancestors 'none'), and nothing from Adventure Asia runs on the agency's site.
//
// The addresses come from CIS (GET /v1/account/add-ons, the tripplanner entry's `planner`): the hosted planner URL, the
// agency's ACTIVE custom planner domains, and whether its plan includes the launcher. Only https addresses are offered,
// and the label is HTML-escaped, so a pasted snippet can only ever be one link. Pure (no React), so `npm test` runs it.

/** CIS: the tripplanner add-on's planner (absent before CIS sends it: the launcher then stays hidden). */
export interface PlannerSite {
  /** The hosted planner, e.g. https://<planner host>/<slug>/plan. */
  url: string;
  /** The agency's active custom planner domains (host names), if any. */
  domains?: string[] | null;
  /** The plan includes the launcher (entitlement tp.launcher). */
  launcher: boolean;
}

export type LauncherStyle = "link" | "button";

const HOST_RE = /^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$/;

/** An https URL safe to put in an href attribute, or null. */
export function safeHttpsUrl(raw: unknown): string | null {
  if (typeof raw !== "string" || raw.length > 2048) return null;
  try {
    const u = new URL(raw);
    if (u.protocol !== "https:" || u.username || u.password) return null;
    return u.toString();
  } catch {
    return null;
  }
}

/** The addresses a launcher may open: each custom planner domain's /plan first (the agency's own brand), then the
 *  hosted planner. Empty when the plan has no launcher or CIS sent no usable address. */
export function launcherTargets(site: PlannerSite | null | undefined): string[] {
  if (!site || site.launcher !== true) return [];
  const urls = [...(site.domains ?? []).map(domainPlanUrl), safeHttpsUrl(site.url)];
  return [...new Set(urls.filter((u): u is string => u !== null))];
}

/** The planner page of one custom planner domain (TripPlanner maps /plan there to the agency's planner), or null. */
function domainPlanUrl(host: unknown): string | null {
  const h = typeof host === "string" ? host.trim().toLowerCase() : "";
  return HOST_RE.test(h) ? safeHttpsUrl(`https://${h}/plan`) : null;
}

const ESC: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ESC[c]);
}

/** The label of the launcher, trimmed and capped; the fallback when it is empty. */
export function launcherLabel(raw: string, fallback: string): string {
  const label = raw.replace(/\s+/g, " ").trim().slice(0, LAUNCHER_LABEL_MAX);
  return label || fallback;
}

export const LAUNCHER_LABEL_MAX = 60;

// The button look: plain inline CSS, so it shows the same on any site builder (no stylesheet, no class to clash with).
const BUTTON_CSS =
  "display:inline-block;padding:12px 20px;border-radius:8px;background:#1d3557;color:#ffffff;" +
  "font:600 15px/1.2 system-ui,sans-serif;text-decoration:none";

/** The HTML to paste: one <a> that opens `url` in a new tab. Null when the URL is not a safe https address. */
export function launcherSnippet(url: string, label: string, style: LauncherStyle): string | null {
  const href = safeHttpsUrl(url);
  if (!href) return null;
  const attrs = `href="${escapeHtml(href)}" target="_blank" rel="noopener"`;
  const css = style === "button" ? ` style="${BUTTON_CSS}"` : "";
  return `<a ${attrs}${css}>${escapeHtml(label)}</a>`;
}
