// AA-1188 (S295 DB-01) — the studio BFF proxy forwards only the CIS tenant calls the studio makes (lib/studio/tenantRoutes.ts).
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { proxyPath } from "@/lib/bffPath";
import { TENANT_ROUTES, matchTenantRoute } from "@/lib/studio/tenantRoutes";

const WEB = path.resolve(__dirname, "../..");

/** The encoded CIS sub-path the proxy matches, as app/api/tenant/[...path]/route.ts builds it. */
const sub = (p: string): string => {
  const out = proxyPath(p.split("/"));
  if (out === null) throw new Error(`invalid path ${p}`);
  return out;
};

// Every call the studio makes through the proxy (grep of `/api/tenant/` in web/app, web/lib, S295), with the method.
const CALLS: [string, string][] = [
  ["GET", "v1/billing"],
  ["GET", "v1/quota"],
  ["GET", "v1/notifications"],
  ["POST", "v1/notifications/read-all"],
  ["GET", "v1/account/members"],
  ["GET", "v1/account/add-ons"],
  ["POST", "v1/account/add-on-requests"],
  ["GET", "v1/account/activity"],
  ["GET", "v1/account/activity/export"],
  ["GET", "v1/account/tour-retirement"],
  ["PATCH", "v1/account/tour-retirement"],
  ["GET", "admin/brand-identity"],
  ["POST", "admin/brand-identity"],
  ["POST", "admin/brand-identity/upload"],
  ["POST", "admin/brand-identity/parse"],
  ["GET", "v1/facts"],
  ["POST", "v1/facts"],
  ["PATCH", "v1/facts/7c1e5c2a-3f0e-4c55-9b1a-2f7b8c9d0e1f"],
  ["DELETE", "v1/facts/7c1e5c2a-3f0e-4c55-9b1a-2f7b8c9d0e1f"],
  ["GET", "v1/tours/pool"],
  ["POST", "v1/tours/pool/T-949/rewrite"],
  ["GET", "v1/marketplace"],
  ["GET", "v1/tours/my-versions"],
  ["GET", "v1/tours/versions/42"],
  ["PATCH", "v1/tours/versions/42"],
  ["POST", "v1/tours/versions/42/retry"],
  ["GET", "v1/tours/versions/42/export-docx"],
  ["GET", "v1/progress/tour/42"],
  ["GET", "v1/progress/piece/abc-1"],
  ["GET", "v1/angle-gate/goals"],
  ["GET", "v1/angle-gate/requests/r1"],
  ["POST", "v1/angle-gate/requests/r1/goal"],
  ["POST", "v1/angle-gate/requests/r1/choose"],
  ["POST", "v1/angle-gate/requests/r1/reopen"],
  ["POST", "v1/content-writing/requests/r1/write"],
  ["GET", "v1/content-writing/requests/r1/latest-piece"],
  ["GET", "v1/content-writing/reviews"],
  ["GET", "v1/content-writing/pieces/p1"],
  ["PATCH", "v1/content-writing/pieces/p1"],
  ["GET", "v1/content-writing/pieces/p1/export"],
  ["GET", "v1/content-writing/ai-disclosure"],
  ["POST", "v1/content-writing/cta-check"],
  ["GET", "v1/publish-log"],
  ["GET", "v1/publish-log/pending"],
  ["GET", "v1/publish-log/put-back"],
  ["GET", "v1/publish-log/retired-pages"],
  ["POST", "v1/publish-log/p1/publish"],
  ["POST", "v1/publish-log/p1/put-back"],
  ["POST", "v1/publish-log/p1/move-to-draft"],
  ["DELETE", "v1/publish-log/p1"],
  ["GET", "v1/integrations/wordpress"],
  ["POST", "v1/integrations/wordpress"],
  ["DELETE", "v1/integrations/wordpress"],
  ["POST", "v1/integrations/wordpress/test"],
  ["GET", "v1/integrations/facebook"],
  ["POST", "v1/integrations/facebook"],
  ["DELETE", "v1/integrations/facebook"],
  ["POST", "v1/integrations/facebook/test"],
  ["GET", "v1/slate"],
  ["POST", "v1/slate/refresh"],
  ["POST", "v1/subjects/s1/pick"],
  ["POST", "v1/subjects/s1/cut"],
  ["GET", "v1/planning/trip-reallocation/suggest"],
  ["POST", "v1/planning/trip-reallocation/confirm"],
];

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (/\.(ts|tsx)$/.test(name)) out.push(full);
  }
  return out;
}

/** A typed-union parameter takes one of a few words, not any id: the scan uses a real value for it. */
const UNION_SAMPLES: Record<string, string> = { integration: "wordpress", kind: "tour" };

/**
 * Every `/api/tenant/...` string or template literal in web/app and web/lib, as a CIS sub-path: a `${...}` after a slash
 * is one path segment ("x1", or the UNION_SAMPLES value), a `${...}` glued to a word is a query suffix and dropped, so
 * is a `?query`.
 */
function literalPaths(): { file: string; path: string }[] {
  const found: { file: string; path: string }[] = [];
  const re = /["'`]\/api\/tenant\/([^"'`\s]*)["'`]/g;
  for (const file of [...sourceFiles(path.join(WEB, "app")), ...sourceFiles(path.join(WEB, "lib"))]) {
    if (file.includes(`${path.sep}api${path.sep}tenant${path.sep}`)) continue; // the proxy and /me themselves
    const text = readFileSync(file, "utf8");
    for (const m of text.matchAll(re)) {
      let p = m[1].replace(/\/\$\{([^}]*)\}/g, (_, expr: string) => `/${UNION_SAMPLES[expr.trim()] ?? "x1"}`)
        .replace(/\$\{[^}]*\}/g, "");
      p = p.split("?")[0].replace(/\/$/, "");
      // /api/tenant/me is its own route; "[...path]", "*", "{id}" and "..." are prose in comments.
      if (p === "me" || p === "" || /[[*{]|\.\.\./.test(p)) continue;
      found.push({ file: path.relative(WEB, file), path: p });
    }
  }
  return found;
}

/**
 * Calls the literal scan cannot read: a `/api/tenant/...` literal the scan regex does not parse whole (a `${...}` with a
 * quote or a space inside, such as `${q ? "?x" : ""}`), or a quoted bare base (`"/api/tenant"`, `"/api/tenant/"`) that a
 * concatenation or a template would extend out of sight. Either would let a new call escape the checks above.
 */
function unreadableCalls(): string[] {
  const parsed = /["'`]\/api\/tenant\/([^"'`\s]*)["'`]/g;
  const opened = /["'`]\/api\/tenant\//g;
  const bareBase = /["'`]\/api\/tenant\/?["'`]/g;
  const out: string[] = [];
  for (const file of [...sourceFiles(path.join(WEB, "app")), ...sourceFiles(path.join(WEB, "lib"))]) {
    if (file.includes(`${path.sep}api${path.sep}tenant${path.sep}`)) continue;
    const text = readFileSync(file, "utf8");
    const ok = new Set([...text.matchAll(parsed)].map((m) => m.index));
    for (const m of text.matchAll(opened)) {
      if (!ok.has(m.index)) out.push(`${path.relative(WEB, file)}: ${text.slice(m.index, m.index + 80).split("\n")[0]}`);
    }
    for (const m of text.matchAll(bareBase)) out.push(`${path.relative(WEB, file)}: bare base ${m[0]}`);
  }
  return out;
}

describe("studio BFF allow-list (AA-1188)", () => {
  it("every /api/tenant/ call in the web sources is a literal the scan reads whole (no hidden path)", () => {
    expect(unreadableCalls()).toEqual([]);
  });

  it.each(CALLS)("%s %s is forwarded", (method, p) => {
    expect(matchTenantRoute(method, sub(p))).not.toBeNull();
  });

  it("every /api/tenant/ literal in the web sources is listed (method free: the table above pins the method)", () => {
    const paths = literalPaths();
    expect(paths.length).toBeGreaterThan(40);
    const missing = paths.filter(({ path: p }) =>
      !["GET", "POST", "PATCH", "DELETE"].some((m) => matchTenantRoute(m, sub(p))));
    expect(missing).toEqual([]);
  });

  it("every literal path is in the method table, so a new call needs a table line too", () => {
    const pinned = (p: string) => CALLS.some(([m, c]) => {
      const route = matchTenantRoute(m, sub(c));
      return route !== null && route.pattern.test(sub(p));
    });
    expect(literalPaths().filter(({ path: p }) => !pinned(p))).toEqual([]);
  });

  it("every listed route is used by at least one call (no dead line)", () => {
    const dead = TENANT_ROUTES.filter((route) => !CALLS.some(([m, p]) => m === route.method && route.pattern.test(sub(p))));
    expect(dead.map((r) => `${r.method} ${r.pattern}`)).toEqual([]);
  });

  it.each([
    ["GET", "v1/tours/x/full"],
    ["GET", "v1/tours/7c1e5c2a-3f0e-4c55-9b1a-2f7b8c9d0e1f/full"],
    ["GET", "v1/tours/x"],
    ["GET", "v1/tours"],
    ["GET", "v1/tours/pool/x"],
    ["GET", "v1/tours/versions/x/full"],
    ["GET", "admin/tours"],
    ["GET", "admin/brand-identity/x"],
    ["GET", "admin/llm/route-control"],
    ["POST", "admin/jobs"],
    ["GET", "v1/admin/tenants"],
    ["POST", "v1/webhooks"],
    ["PATCH", "v1/tenant/settings"],
    ["GET", "openapi.json"],
    ["GET", "health"],
    ["GET", "v1/integrations/x"],
    ["DELETE", "v1/facts"],
    ["PUT", "v1/facts/x"],
    ["HEAD", "v1/billing"],
    ["POST", "v1/billing"],
    ["GET", "v1/billing/x"],
  ])("%s %s is refused", (method, p) => {
    expect(matchTenantRoute(method, sub(p))).toBeNull();
  });

  it("an encoded slash cannot widen a parameter segment", () => {
    // Next.js decodes %2F into one segment "x/full"; proxyPath refuses it, and its encoded form never matches either.
    expect(proxyPath(["v1", "tours", "versions", "x/full"])).toBeNull();
    expect(matchTenantRoute("GET", sub("v1/tours/x%2Ffull"))).toBeNull();
  });

  it("the method is compared case-insensitively", () => {
    expect(matchTenantRoute("get", sub("v1/billing"))).not.toBeNull();
  });
});
