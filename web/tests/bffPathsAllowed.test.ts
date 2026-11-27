// AA-1188 guard (S296 live check: the AA prices page answered 500 for every signed-in agency user). The page read
// `crmCall(..., "GET", AA_TERMS_PATH)` ("aa/terms"), a path on neither BFF list of lib/bff.ts, so crmCall threw and the
// whole server page failed; the page tests mock crmCall, so CI never saw it. This test reads the runtime sources
// (app/, components/, lib/) and checks every CRM API path the web calls against the list that call goes through:
//   - crmCall (server components, route handlers): BFF_ROUTES + SERVER_ONLY_ROUTES, path as written (lib/crmApi.ts);
//   - bffSend / bffFetch / bffDownload and "/bff/..." literals (the browser): BFF_ROUTES only, the query cut off (the
//     BFF matches the URL path; app/bff/[...path]/route.ts), or a route of its own under app/bff/ (identity).
// A path is a string literal, an UPPER_CASE constant of the scanned sources, or a template literal whose placeholders
// are tried with a few sample ids (a UUID, a word, a number, nothing; the last one also with the words of the lists'
// alternations, such as approve|reject). A path held in a variable (a wrapper's `path` argument)
// or a template that starts with a placeholder cannot be read statically and is counted as not checked.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";
import { BFF_ROUTES, matchBff, SERVER_ONLY_ROUTES, type BffRoute } from "@/lib/bff";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SCAN = ["app", "components", "lib"];
const SKIP_DIRS = new Set(["node_modules", ".next", "core"]); // _kit/core is the CIS kit: no CRM call
const SERVER_ROUTES = [...BFF_ROUTES, ...SERVER_ONLY_ROUTES];
// The BFF's own route folders next to [...path] (app/bff/identity): the browser may call them directly.
const OWN_BFF_ROUTES = new Set(
  fs
    .readdirSync(path.join(ROOT, "app", "bff"), { withFileTypes: true })
    .filter((d) => d.isDirectory() && !d.name.startsWith("["))
    .map((d) => d.name),
);
// "" stands for an optional suffix (`tags${archived ? "?archived=true" : ""}`).
const SAMPLES = ["0b3c6a52-7f4e-4d7b-9a51-2c8e6f1d4a90", "sample", "7", "sample-7", ""];
const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;

type Part = string | null; // null = a ${...} placeholder
export interface Call {
  file: string;
  kind: "server" | "browser";
  method: string | null; // null = not a literal (any method)
  expr: string;
}
export interface Finding {
  file: string;
  kind: "server" | "browser";
  method: string | null;
  path: string;
}

function sourceFiles(rel: string): string[] {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) return [];
  if (fs.statSync(abs).isFile()) return [rel];
  const out: string[] = [];
  for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
    const child = path.join(rel, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) out.push(...sourceFiles(child));
    } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      out.push(child);
    }
  }
  return out;
}

/** Index just past the string or template literal that opens at `i`. */
function skipLiteral(code: string, i: number): number {
  const q = code[i];
  i += 1;
  while (i < code.length) {
    const c = code[i];
    if (c === "\\") {
      i += 2;
      continue;
    }
    if (c === q) return i + 1;
    if (q === "`" && c === "$" && code[i + 1] === "{") {
      i = skipBalanced(code, i + 1);
      continue;
    }
    if (q !== "`" && c === "\n") return i; // an unterminated quote (a regex or JSX text): stop at the line end
    i += 1;
  }
  return i;
}

/** Index just past the bracket group that opens at `i` ( "(", "[" or "{" ), strings and templates skipped. */
function skipBalanced(code: string, i: number): number {
  let depth = 0;
  while (i < code.length) {
    const c = code[i];
    if (c === '"' || c === "'" || c === "`") {
      i = skipLiteral(code, i);
      continue;
    }
    if (c === "(" || c === "[" || c === "{") depth += 1;
    else if (c === ")" || c === "]" || c === "}") {
      depth -= 1;
      if (depth === 0) return i + 1;
    }
    i += 1;
  }
  return i;
}

/** The top-level argument texts of the call whose "(" is at `open`. */
function callArgs(code: string, open: number): string[] {
  const end = skipBalanced(code, open) - 1;
  const args: string[] = [];
  let depth = 0;
  let from = open + 1;
  let i = open + 1;
  while (i < end) {
    const c = code[i];
    if (c === '"' || c === "'" || c === "`") {
      i = skipLiteral(code, i);
      continue;
    }
    if (c === "(" || c === "[" || c === "{") depth += 1;
    else if (c === ")" || c === "]" || c === "}") depth -= 1;
    else if (c === "," && depth === 0) {
      args.push(code.slice(from, i).trim());
      from = i + 1;
    }
    i += 1;
  }
  const last = code.slice(from, end).trim();
  if (last) args.push(last);
  return args;
}

/** Every call of `name` in `code` (not its declaration), with its argument texts. Skips a generic `<...>`. */
function callsOf(code: string, name: string): string[][] {
  const out: string[][] = [];
  const re = new RegExp(`(?<![\\w$.])${name}\\b`, "g");
  for (const m of code.matchAll(re)) {
    if (/function\s+$/.test(code.slice(Math.max(0, m.index - 20), m.index))) continue;
    let i = m.index + name.length;
    while (/\s/.test(code[i] ?? "")) i += 1;
    if (code[i] === "<") {
      let depth = 0;
      for (; i < code.length; i += 1) {
        if (code[i] === "<") depth += 1;
        else if (code[i] === ">" && code[i - 1] !== "=") {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      i += 1;
      while (/\s/.test(code[i] ?? "")) i += 1;
    }
    if (code[i] === "(") out.push(callArgs(code, i));
  }
  return out;
}

const literalMethod = (arg: string | undefined): string | null => {
  const m = /^["'](GET|POST|PUT|PATCH|DELETE)["']$/.exec((arg ?? "").trim());
  return m ? m[1] : null;
};

/** Every CRM API call in one source text. */
export function callsIn(file: string, code: string): Call[] {
  const calls: Call[] = [];
  for (const a of callsOf(code, "crmCall")) {
    if (a.length >= 4) calls.push({ file, kind: "server", method: literalMethod(a[2]), expr: a[3] });
  }
  for (const a of callsOf(code, "bffSend")) {
    if (a.length >= 2) calls.push({ file, kind: "browser", method: literalMethod(a[0]), expr: a[1] });
  }
  for (const a of callsOf(code, "bffFetch")) {
    if (a.length < 1) continue;
    const init = a[1] ?? "";
    const m = /\bmethod\s*:\s*["'](GET|POST|PUT|PATCH|DELETE)["']/.exec(init);
    const method = m ? m[1] : /\bmethod\b/.test(init) ? null : "GET";
    calls.push({ file, kind: "browser", method, expr: a[0] });
  }
  for (const a of callsOf(code, "bffDownload")) {
    if (a.length >= 1) calls.push({ file, kind: "browser", method: "GET", expr: a[0] });
  }
  // A "/bff/..." literal handed to fetch / XMLHttpRequest by hand (lib/bffClient.ts's own `/bff/${path}` is dynamic).
  // A bare "/bff/" is a prefix test (lib/hosts.ts), not a call.
  for (const m of code.matchAll(/(["'`])\/bff\/((?:(?!\1)[^\\\n]|\\.)+)\1/g)) {
    calls.push({ file, kind: "browser", method: null, expr: `${m[1]}${m[2]}${m[1]}` });
  }
  return calls;
}

/** UPPER_CASE string constants of the sources (`const AA_TERMS_PATH = "aa/terms"`), every value seen per name. */
export function constantsIn(codes: string[]): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const code of codes) {
    for (const m of code.matchAll(/\bconst\s+([A-Z][A-Z0-9_]*)\s*(?::\s*string\s*)?=\s*(["'])([^"'\\\n]*)\2/g)) {
      const seen = out.get(m[1]) ?? [];
      if (!seen.includes(m[3])) seen.push(m[3]);
      out.set(m[1], seen);
    }
  }
  return out;
}

/** The path(s) an argument text stands for, as parts (null = placeholder); null when it cannot be read statically. */
export function resolvePath(expr: string, consts: Map<string, string[]>): Part[][] | null {
  const e = expr.replace(/\s+as\s+const$/, "").trim();
  const quoted = /^(["'])([^"'\\]*)\1$/.exec(e);
  if (quoted) return [[quoted[2]]];
  if (/^[A-Z][A-Z0-9_]*$/.test(e)) return consts.has(e) ? consts.get(e)!.map((v) => [v]) : null;
  if (!(e.startsWith("`") && e.endsWith("`") && skipLiteral(e, 0) === e.length)) return null;
  const parts: Part[] = [];
  let text = "";
  let i = 1;
  while (i < e.length - 1) {
    if (e[i] === "\\") {
      text += e[i + 1];
      i += 2;
    } else if (e[i] === "$" && e[i + 1] === "{") {
      parts.push(text, null);
      text = "";
      i = skipBalanced(e, i + 1);
    } else {
      text += e[i];
      i += 1;
    }
  }
  parts.push(text);
  const clean = parts.filter((p) => p !== "");
  if (clean[0] === null || clean[0] === undefined) return null; // `${base}/...`: the start of the path is not known
  return [clean];
}

/** Cut the query off (the browser's URL path is what the BFF matches). */
function withoutQuery(parts: Part[]): Part[] {
  const out: Part[] = [];
  for (const p of parts) {
    if (p !== null && p.includes("?")) {
      out.push(p.slice(0, p.indexOf("?")));
      return out;
    }
    out.push(p);
  }
  return out;
}

/** The words of the lists' alternations (`(approve|reject|execute)`): a last placeholder may be one of them. */
function alternationWords(routes: readonly BffRoute[]): string[] {
  const words = new Set<string>();
  for (const r of routes) {
    for (const m of r.pattern.source.matchAll(/\((?:\?:)?([A-Za-z0-9_|-]+)\)/g)) for (const w of m[1].split("|")) words.add(w);
  }
  return [...words];
}

/** Concrete paths for the parts: placeholders take the sample ids, the last one also the alternation words. */
function fillings(parts: Part[], words: readonly string[]): string[] {
  let acc = [""];
  parts.forEach((p, i) => {
    const pool = i === parts.length - 1 ? [...SAMPLES, ...words] : SAMPLES;
    acc = p === null ? acc.flatMap((a) => pool.map((s) => a + s)) : acc.map((a) => a + p);
  });
  return acc;
}

function allowedOn(routes: readonly BffRoute[], method: string | null, sub: string): boolean {
  return (method ? [method] : METHODS).some((m) => matchBff(m, sub, routes) !== null);
}

/** Check calls against the lists; returns the refused calls and how many were checked / not readable. */
export function check(
  calls: Call[],
  consts: Map<string, string[]>,
  lists: {
    server: readonly BffRoute[];
    browser: readonly BffRoute[];
    own: Set<string>;
    serverWords: readonly string[];
    browserWords: readonly string[];
  },
): { refused: Finding[]; checked: number; unread: number } {
  const refused: Finding[] = [];
  let checked = 0;
  let unread = 0;
  for (const c of calls) {
    const paths = resolvePath(c.expr, consts);
    if (!paths) {
      unread += 1;
      continue;
    }
    checked += 1;
    for (const raw of paths) {
      const parts = c.kind === "browser" ? withoutQuery(raw) : raw;
      const candidates = fillings(parts, c.kind === "server" ? lists.serverWords : lists.browserWords);
      const ok = candidates.some((sub) =>
        c.kind === "server"
          ? allowedOn(lists.server, c.method, sub)
          : allowedOn(lists.browser, c.method, sub) || lists.own.has(sub.split("/")[0]),
      );
      if (!ok) refused.push({ file: c.file, kind: c.kind, method: c.method, path: parts.map((p) => p ?? "${…}").join("") });
    }
  }
  return { refused, checked, unread };
}

const LISTS = {
  server: SERVER_ROUTES,
  browser: BFF_ROUTES,
  own: OWN_BFF_ROUTES,
  serverWords: alternationWords(SERVER_ROUTES),
  browserWords: alternationWords(BFF_ROUTES),
};

function scanSources(): { calls: Call[]; consts: Map<string, string[]> } {
  const files = SCAN.flatMap(sourceFiles);
  const codes = files.map((rel) => fs.readFileSync(path.join(ROOT, rel), "utf8"));
  return { calls: files.flatMap((rel, i) => callsIn(rel, codes[i])), consts: constantsIn(codes) };
}

test("the reader finds literal, constant and template paths and skips declarations", () => {
  const code = [
    'export const AA_TERMS_PATH = "aa/terms";',
    "export async function crmCall<T>(cfg: C, session: S, method: Method, subPath: string) {}",
    'await crmCall<{ items: Array<X> }>(ctx.cfg, session, "GET", AA_TERMS_PATH);',
    "await crmCall<X>(cfg, session, \"POST\", `trips/${tripId}/stage`, { body: { to: \"won\" } });",
    'await bffSend<X>("GET", `aa/catalog?${new URLSearchParams({ q: "x, y" })}`);',
    "await bffSend(method, path, body);",
    'await bffDownload("aa/tariff?currency=USD", { filename: "x.csv" });',
    'fetch("/bff/identity", { credentials: "same-origin" });',
  ].join("\n");
  const calls = callsIn("x.tsx", code);
  expect(calls.map((c) => [c.kind, c.method, c.expr])).toEqual([
    ["server", "GET", "AA_TERMS_PATH"],
    ["server", "POST", "`trips/${tripId}/stage`"],
    ["browser", "GET", '`aa/catalog?${new URLSearchParams({ q: "x, y" })}`'],
    ["browser", null, "path"],
    ["browser", "GET", '"aa/tariff?currency=USD"'],
    ["browser", null, '"identity"'],
  ]);
  const consts = constantsIn([code]);
  expect(resolvePath("AA_TERMS_PATH", consts)).toEqual([["aa/terms"]]);
  expect(resolvePath("`trips/${tripId}/stage`", consts)).toEqual([["trips/", null, "/stage"]]);
  expect(resolvePath("`${base}/${tagId}`", consts)).toBeNull();
  expect(resolvePath("path", consts)).toBeNull();
});

test("a server page path missing from both lists is refused (the S296 aa/terms case)", () => {
  const code = 'await crmCall<AaTerms>(ctx.cfg, session, "GET", AA_TERMS_PATH);\nexport const AA_TERMS_PATH = "aa/terms";';
  const calls = callsIn("app/(crm)/aa-tariff/page.tsx", code);
  const consts = constantsIn([code]);
  const without = SERVER_ROUTES.filter((r) => !r.pattern.test("aa/terms"));
  expect(check(calls, consts, { ...LISTS, server: without }).refused).toEqual([
    { file: "app/(crm)/aa-tariff/page.tsx", kind: "server", method: "GET", path: "aa/terms" },
  ]);
  expect(check(calls, consts, LISTS).refused).toEqual([]);
});

test("a server-only path called from the browser is refused; a wrong method is refused", () => {
  const code = [
    'await bffSend("GET", "aa/terms");',
    'await bffSend("DELETE", "aa/tariff");',
    'await bffSend("GET", "aa/tariff?currency=EUR");',
    "await bffSend(\"POST\", `refund-requests/${r.refund_id}/${action}`, body);",
    "await bffSend(\"POST\", `refund-requests/${r.refund_id}/erase`, body);",
    "await bffSend(\"GET\", `tags${archived ? \"?archived=true\" : \"\"}`);",
  ].join("\n");
  const { refused, checked } = check(callsIn("components/x.tsx", code), new Map(), LISTS);
  expect(checked).toBe(6);
  expect(refused.map((f) => `${f.method} ${f.path}`)).toEqual([
    "GET aa/terms",
    "DELETE aa/tariff",
    "POST refund-requests/${…}/erase",
  ]);
});

test("every CRM API path the web calls is on the list its call goes through", () => {
  const { calls, consts } = scanSources();
  const { refused, checked } = check(calls, consts, LISTS);
  expect(refused.map((f) => `${f.file}: ${f.kind} ${f.method ?? "*"} ${f.path}`)).toEqual([]);
  // The reader must keep seeing the calls (a parser that goes blind would pass with nothing checked).
  expect(checked).toBeGreaterThan(220); // 270 of 322 calls were readable when this guard was written (S296)
});

test("the AA prices page reads aa/terms through a listed server route", () => {
  const rel = path.join("app", "(crm)", "aa-tariff", "page.tsx");
  const code = fs.readFileSync(path.join(ROOT, rel), "utf8");
  const { consts } = scanSources();
  const calls = callsIn(rel, code);
  const terms = calls.filter((c) => resolvePath(c.expr, consts)?.some((p) => p.join("") === "aa/terms"));
  expect(terms).toHaveLength(1);
  expect(check(terms, consts, LISTS).refused).toEqual([]);
  expect(matchBff("GET", "aa/terms", BFF_ROUTES)).toBeNull(); // server only: the browser never asks for it
});
