// AA-1036 (R3) guard: the Content studio speaks English and Vietnamese through the kit i18n (app/_kit/core/i18n.ts).
//   1. Every message catalogue of the studio (`*.messages.ts` under the studio folders) has a Vietnamese message for
//      every English key, of the same kind (string / function), never empty and never an internal issue ID.
//   2. No studio component keeps a hard-coded English UI string: JSX text, a text attribute (title, placeholder,
//      aria-label, label…) given as a string literal, or an English-looking string literal inside a JSX expression.
//      Proper names that read the same in both languages are listed in ALLOWED_TEXT.
//   2b. The studio's helper modules (.ts) never throw an English sentence (they throw a StudioError with a message
//      key, studioError.ts) and no studio object literal keeps an English UI label ({ label: "Landing Page" }).
//   3. The studio's language store: one setting for the whole app (the CRM's cookie and form), English by default.
//   4. The draft legal documents (legal/*/<name>.messages.ts, structured text): the Vietnamese document has the same
//      sections and blocks as the English one, and no English placeholder left in it.
// The scanner parses with the TypeScript compiler, so comments are never scanned. Run: npm test (from web/).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import ts from "typescript";
import { describe, expect, test } from "vitest";
import { missingTranslations, translator, type Messages } from "@/app/_kit/core/i18n";
import { localeForLang, studioLangStore } from "@/lib/studio/lang";
import { docShape, docText, type LegalDoc } from "@/app/(studio)/_legal/legalDoc";
import { LOCALE_COOKIE } from "@/lib/i18n";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const STUDIO_DIRS = ["app/(studio)", "app/_kit/local/studio"];
/** Catalogues outside the studio folders that studio pages render (lib helpers' words). */
const LIB_DIRS = ["lib/studio"];

function walk(rel: string): string[] {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) return [];
  const out: string[] = [];
  for (const e of fs.readdirSync(abs, { withFileTypes: true })) {
    const child = path.join(rel, e.name);
    if (e.isDirectory()) out.push(...walk(child));
    else out.push(child.split(path.sep).join("/"));
  }
  return out;
}

const FILES = STUDIO_DIRS.flatMap(walk);
const CATALOGUES = [...FILES, ...LIB_DIRS.flatMap(walk)].filter((f) => /\.messages\.ts$/.test(f));
const COMPONENTS = FILES.filter((f) => f.endsWith(".tsx"));

// ── 1. catalogues ──────────────────────────────────────────────────────────────────────────────────────────────

function isMessages(v: unknown): v is Messages<Record<string, string | ((...a: never[]) => string)>> {
  if (!v || typeof v !== "object" || !("en" in v) || !("vi" in v)) return false;
  const en = (v as { en: unknown }).en;
  return !!en && typeof en === "object" && Object.values(en).every((m) => typeof m === "string" || typeof m === "function");
}

/** Structured per-language content (a guide, a legal document): `{ en, vi }` of the same shape. */
function isStructured(v: unknown): v is Record<"en" | "vi", unknown> {
  return !!v && typeof v === "object" && "en" in v && "vi" in v && !isMessages(v);
}

/** The shape of structured content: every string becomes "s" (link targets and anchors are kept, they must match). */
function shapeOf(v: unknown): string {
  return JSON.stringify(v, (k, x) => (typeof x === "string" && k !== "href" && k !== "id" ? "s" : x));
}

function isLegalDocs(v: unknown): v is Record<"en" | "vi", LegalDoc> {
  return !!v && typeof v === "object" && "en" in v && "vi" in v && Array.isArray((v as { en: LegalDoc }).en?.sections);
}

/** Sample arguments for a message function: numbers and words, enough to render every message. */
function render(m: unknown): string {
  if (typeof m === "string") return m;
  if (typeof m === "function") return String((m as (...a: unknown[]) => unknown)(2, "Sample", "Other", 3, 4));
  return "";
}

describe("studio message catalogues", () => {
  test("the studio has message catalogues", () => {
    expect(CATALOGUES.length, "expected *.messages.ts files in the studio").toBeGreaterThan(5);
  });

  test.each(CATALOGUES)("%s: every English key has a Vietnamese message", async (file) => {
    const mod = (await import(pathToFileURL(path.join(ROOT, file)).href)) as Record<string, unknown>;
    const catalogues = Object.entries(mod).filter(([, v]) => isMessages(v));
    const docs = Object.entries(mod).filter(([, v]) => isLegalDocs(v));
    const structured = Object.entries(mod).filter(([, v]) => isStructured(v));
    expect(catalogues.length + structured.length, `${file} exports no defineMessages() catalogue`).toBeGreaterThan(0);
    for (const [name, s] of structured) {
      if (!isStructured(s)) continue;
      expect(shapeOf(s.vi), `${file} ${name}: the Vietnamese content's structure`).toBe(shapeOf(s.en));
    }
    for (const [name, d] of docs) {
      if (!isLegalDocs(d)) continue;
      expect(docShape(d.vi), `${file} ${name}: the Vietnamese document's sections and blocks`).toEqual(docShape(d.en));
      expect(d.vi.title.trim()).not.toBe("");
      const vi = docText(d.vi);
      expect(vi, `${file} ${name}: an English placeholder in the Vietnamese text`).not.toMatch(/to be confirmed/);
      expect(`${docText(d.en)} ${vi}`, `${file} ${name}: an issue ID in UI text`).not.toMatch(/\bAA-\d+\b/);
    }
    for (const [name, m] of catalogues) {
      if (!isMessages(m)) continue;
      expect(missingTranslations(m), `${file} ${name}: keys without a Vietnamese message`).toEqual([]);
      expect(Object.keys(m.vi).sort(), `${file} ${name}: Vietnamese keys not in English`).toEqual(Object.keys(m.en).sort());
      for (const key of Object.keys(m.en)) {
        const en = render(m.en[key]);
        const vi = render((m.vi as Record<string, unknown>)[key]);
        expect(vi.trim(), `${file} ${name}.${key}: empty Vietnamese message`).not.toBe("");
        expect(`${en} ${vi}`, `${file} ${name}.${key}: an issue ID in UI text`).not.toMatch(/\bAA-\d+\b/);
      }
    }
  });
});

// ── 2. hard-coded English ──────────────────────────────────────────────────────────────────────────────────────

/** Attributes whose value is shown to a person (or read out by a screen reader). */
const TEXT_ATTRS = new Set([
  "title", "placeholder", "aria-label", "aria-description", "alt", "label", "sub", "description", "message", "hint",
  "ariaLabel", "emptyText", "emptyTitle", "confirmLabel", "cancelLabel", "saveLabel", "heading", "caption", "tooltip",
  "text", "subtitle", "body", "note", "error", "loadingLabel", "buttonLabel", "example",
]);

/** Text that reads the same in English and Vietnamese (names, symbols, units), exact after trim. */
const ALLOWED_TEXT = new Set([
  "Adventure Asia", "WordPress", "Facebook", "Instagram", "LinkedIn", "TikTok", "CRM", "SEO", "URL", "CTA", "AI",
  "Esc", "⌘K", "Ctrl K", "Ctrl", "OK", "ID", "USD", "H1", "H2", "PDF", "CSV", "HTML", "JSON", "API",
  "adventure.asia", // the Adventure Asia site, as a link label
  "xxxx xxxx xxxx xxxx xxxx xxxx", // the shape of a WordPress application password (an input placeholder)
]);

/** English-looking: two letters in a row, and either a space or a capitalised word (a message key is camelCase). */
function looksEnglish(text: string): boolean {
  const s = text.replace(/\s+/g, " ").trim();
  if (!/[A-Za-z]{2,}/.test(s) || ALLOWED_TEXT.has(s)) return false;
  // Only acronyms and format names ("CSV (", "XLSX"): the same in both languages.
  if ((s.match(/[A-Za-z]{2,}/g) ?? []).every((w) => /^[A-Z0-9]+$/.test(w))) return false;
  return /\s/.test(s) || /^[A-Z][a-z]/.test(s);
}

/** A literal compared with === / !== (a value check, not text), or the CSS inside a <style> element. */
function notText(node: ts.Node): boolean {
  const p = node.parent;
  if (p && ts.isBinaryExpression(p) && [
    ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken,
    ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken,
  ].includes(p.operatorToken.kind)) return true;
  for (let q: ts.Node | undefined = node.parent; q; q = q.parent) {
    if (ts.isJsxElement(q)) return q.openingElement.tagName.getText() === "style";
  }
  return false;
}

/** Calls whose string arguments are not UI text (class names, URLs, keys, logs, errors meant for developers). */
const NON_UI_CALLS = new Set([
  "t", "tc", "tk", "require", "encodeURIComponent", "fetch", "apiGet", "apiPost", "apiPut", "apiDelete", "apiPatch",
  "console.log", "console.warn", "console.error", "Error", "URLSearchParams", "router.push", "router.replace",
  "useQuery", "invalidateQueries", "setQueryData", "getQueryData", "searchParams.get", "params.get", "matchMedia",
  "querySelector", "addEventListener", "removeEventListener", "getItem", "setItem", "removeItem", "startsWith",
  "endsWith", "includes", "split", "replace", "join", "toLocaleString", "toLocaleDateString", "Intl.NumberFormat",
]);

function callName(node: ts.CallExpression | ts.NewExpression): string {
  const e = node.expression;
  if (ts.isIdentifier(e)) return e.text;
  if (ts.isPropertyAccessExpression(e)) {
    const head = ts.isIdentifier(e.expression) ? e.expression.text + "." : "";
    return head + e.name.text;
  }
  return "";
}

function insideNonUiCall(node: ts.Node): boolean {
  for (let p: ts.Node | undefined = node.parent; p; p = p.parent) {
    if (ts.isJsxElement(p) || ts.isJsxSelfClosingElement(p) || ts.isJsxAttribute(p)) return false;
    if (ts.isCallExpression(p) || ts.isNewExpression(p)) {
      const name = callName(p);
      if (NON_UI_CALLS.has(name) || NON_UI_CALLS.has(name.split(".").pop() ?? "")) return true;
    }
  }
  return false;
}

/** The JSX attribute a node sits in (directly, or inside its expression), or null. */
function enclosingAttr(node: ts.Node): ts.JsxAttribute | null {
  for (let p: ts.Node | undefined = node.parent; p; p = p.parent) {
    if (ts.isJsxAttribute(p)) return p;
    if (ts.isJsxElement(p) || ts.isJsxSelfClosingElement(p) || ts.isJsxFragment(p)) return null;
  }
  return null;
}

function insideJsxChild(node: ts.Node): boolean {
  for (let p: ts.Node | undefined = node.parent; p; p = p.parent) {
    if (ts.isJsxAttribute(p)) return false;
    if (ts.isJsxExpression(p) && p.parent && (ts.isJsxElement(p.parent) || ts.isJsxFragment(p.parent))) return true;
    if (ts.isJsxElement(p) || ts.isJsxSelfClosingElement(p) || ts.isJsxFragment(p)) return false;
  }
  return false;
}

function attrName(a: ts.JsxAttribute): string {
  return a.name.getText();
}

export function hardCodedStrings(rel: string, code: string): { line: number; text: string }[] {
  const sf = ts.createSourceFile(rel, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const hits: { line: number; text: string }[] = [];
  const hit = (node: ts.Node, text: string) =>
    hits.push({ line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, text: text.trim().slice(0, 100) });
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node)) {
      const text = node.getText(sf);
      if (/[A-Za-z]{2,}/.test(text) && !ALLOWED_TEXT.has(text.replace(/\s+/g, " ").trim())) hit(node, text);
    } else if (
      ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) || ts.isTemplateTail(node)
    ) {
      const text = node.text;
      const attr = enclosingAttr(node);
      if (notText(node)) {
        // a value check or CSS
      } else if (attr && TEXT_ATTRS.has(attrName(attr)) && looksEnglish(text) && !insideNonUiCall(node)) hit(node, text);
      else if (!attr && insideJsxChild(node) && looksEnglish(text) && !insideNonUiCall(node)) hit(node, text);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

describe("no hard-coded English in studio components", () => {
  test("the scanner sees JSX text, text attributes and strings in JSX, not keys, classes or comments", () => {
    const sample = [
      "// Hello world comment",
      "const a = <p title=\"Open the tour\" className=\"some class\" data-testid=\"x y\">Write a post</p>;",
      "const b = <p>{t(\"navMarketplace\")} {ok ? \"Saved it\" : t(\"saving\")}</p>;",
      "const c = <Btn label={`Open ${name}`} href=\"/portal/tours\" />;",
      "const d = <p>{\"Adventure Asia\"} WordPress</p>;",
      "const e = <p>{fetch(\"/api/tenant/me x\")}</p>;",
    ].join("\n");
    expect(hardCodedStrings("sample.tsx", sample).map((h) => h.text)).toEqual([
      "Open the tour", "Write a post", "Saved it", "Open",
    ]);
  });

  test("every studio component takes its UI text from a message catalogue", () => {
    expect(COMPONENTS.length, "expected the studio components").toBeGreaterThan(40);
    const hits: string[] = [];
    for (const file of COMPONENTS) {
      for (const h of hardCodedStrings(file, fs.readFileSync(path.join(ROOT, file), "utf8"))) {
        hits.push(`${file}:${h.line}: ${JSON.stringify(h.text)}`);
      }
    }
    expect(hits, "move the text into the area's *.messages.ts (EN + VI) and render it with useT / translator").toEqual([]);
  });
});

// ── 2b. helpers and object literals ────────────────────────────────────────────────────────────────────────────

/** Studio code outside the catalogues and the components' JSX: helper modules, and object literals anywhere.
 *  A server module (`*-server.ts`) answers the BFF, not a person: not scanned. */
const SOURCES = [...FILES, ...LIB_DIRS.flatMap(walk)].filter(
  (f) => /\.tsx?$/.test(f) && !/\.messages\.ts$/.test(f) && !/-server\.ts$/.test(f),
);

/** Calls whose arguments reach the tenant as an error sentence. */
const ERROR_CALLS = new Set(["Error", "ApiError", "StudioError", "plainError"]);

function insideErrorText(node: ts.Node): boolean {
  for (let p: ts.Node | undefined = node.parent; p; p = p.parent) {
    if (ts.isThrowStatement(p)) return true;
    if ((ts.isCallExpression(p) || ts.isNewExpression(p)) && ERROR_CALLS.has(callName(p))) return true;
    if (ts.isBlock(p) || ts.isSourceFile(p) || ts.isArrowFunction(p) || ts.isFunctionExpression(p)) return false;
  }
  return false;
}

/** English sentences a helper throws (.ts files only: a component's errors already come from its catalogue), and
 *  English values of a text property (`label`, `title`…) in an object literal (any studio file). */
export function englishOutsideJsx(rel: string, code: string): { line: number; text: string }[] {
  const sf = ts.createSourceFile(rel, code, ts.ScriptTarget.Latest, true, rel.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const helper = rel.endsWith(".ts");
  const hits: { line: number; text: string }[] = [];
  const visit = (node: ts.Node) => {
    if (
      ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) || ts.isTemplateTail(node)
    ) {
      const p = node.parent;
      const textProp = ts.isPropertyAssignment(p) && p.initializer === node && TEXT_ATTRS.has(p.name.getText(sf));
      if (looksEnglish(node.text) && !notText(node) && (textProp || (helper && insideErrorText(node)))) {
        hits.push({ line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, text: node.text.trim().slice(0, 100) });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

describe("no hard-coded English in studio helpers and object literals", () => {
  test("the scanner sees thrown sentences and text properties, not keys, codes or developer logs", () => {
    const sample = [
      "if (!r.ok) throw new Error(`Save failed (${r.status})`);",
      "if (r.status === 409) throw new Error(\"A rewrite is already running.\");",
      "const x = plainError(e, \"Request failed\", lang);",
      "throw new StudioError(\"rewriteBusy\", r.status);",
      "const TABS = [{ key: \"landing_page\", label: \"Landing Page\" }, { key: \"blog\", label: \"navBlog\" }];",
      "console.warn(\"Something went wrong here\");",
      "if (mode === \"Not a sentence check\") {}",
    ].join("\n");
    expect(englishOutsideJsx("sample.ts", sample).map((h) => h.text)).toEqual([
      "Save failed (", "A rewrite is already running.", "Request failed", "Landing Page",
    ]);
    // In a component only the object literal counts (its thrown errors are checked by the JSX rule's t() use).
    expect(englishOutsideJsx("sample.tsx", sample).map((h) => h.text)).toEqual(["Landing Page"]);
  });

  test("every studio helper and object literal takes its UI text from a message catalogue", () => {
    expect(SOURCES.filter((f) => f.endsWith(".ts")).length, "expected the studio helper modules").toBeGreaterThan(10);
    const hits: string[] = [];
    for (const file of SOURCES) {
      for (const h of englishOutsideJsx(file, fs.readFileSync(path.join(ROOT, file), "utf8"))) {
        hits.push(`${file}:${h.line}: ${JSON.stringify(h.text)}`);
      }
    }
    expect(hits, "throw a StudioError with a key of errors.messages.ts, or take the label from a catalogue").toEqual([]);
  });

  test("a studio error reads in the UI language; the API's own sentence passes through", async () => {
    const { StudioError, plainError } = await import("@/app/(studio)/portal/_components/studioError");
    const { ApiError } = await import("@/app/_kit/local/studio/api");
    expect(plainError(new StudioError("rewriteBusy", 409), "x", "en")).toBe("A rewrite of this tour is already in progress.");
    expect(plainError(new StudioError("rewriteBusy", 409), "x", "vi")).toBe("Tour này đang được viết lại.");
    expect(plainError(new StudioError("saveFailed", 500), "x", "vi")).toBe("Không lưu được (500)");
    expect(plainError(new StudioError("publishFailed", 409, "tour_paused: Paused by the operator."), "x", "vi"))
      .toBe("Paused by the operator.");
    expect(plainError(new ApiError("HTTP 502", 502), "x", "vi")).toBe("Yêu cầu không thành công (502)");
    expect(plainError(new ApiError("Quota reached.", 429, "Quota reached."), "x", "vi")).toBe("Quota reached.");
    expect(plainError(new TypeError("Failed to fetch"), "Lỗi mạng", "vi")).toBe("Lỗi mạng");
    expect(plainError(new Error("Đã viết sẵn"), "x", "vi")).toBe("Đã viết sẵn");
    expect(plainError(null, "Dự phòng", "vi")).toBe("Dự phòng");
  });

  test("the Slate tabs and the channel names share one catalogue", async () => {
    const { channelName } = await import("@/app/(studio)/portal/_components/channelLabels");
    expect(channelName("landing_page", "vi")).toBe("Trang đích");
    expect(channelName("ads", "vi")).toBe("Quảng cáo");
    const slate = fs.readFileSync(path.join(ROOT, "app/(studio)/portal/_components/SlateTab.tsx"), "utf8");
    expect(slate).toContain("channelName(key, lang)");
  });
});

// ── 3. the language setting ────────────────────────────────────────────────────────────────────────────────────

describe("studio language store", () => {
  test("answers the language the server resolved, English by default", () => {
    expect(studioLangStore("vi", "vi-VN").getSnapshot()).toBe("vi");
    expect(studioLangStore("en", "en-AU").getServerSnapshot()).toBe("en");
    expect(studioLangStore("en", "en-AU").cookieName).toBe(LOCALE_COOKIE);
  });

  test("a choice saves the CRM's date locale (Vietnamese is vi-VN, English keeps its variant)", () => {
    expect(localeForLang("vi", "en-GB")).toBe("vi-VN");
    expect(localeForLang("en", "vi-VN")).toBe("en-AU");
    expect(localeForLang("en", "en-US")).toBe("en-US");
  });

  test("the shell catalogue renders Vietnamese only when asked", async () => {
    const { STUDIO_MESSAGES } = await import("@/app/_kit/local/studio/studio.messages");
    expect(translator(STUDIO_MESSAGES, "en")("navMyTours")).toBe("My Tours");
    expect(translator(STUDIO_MESSAGES, "vi")("navMyTours")).toBe("Tour của tôi");
  });
});
