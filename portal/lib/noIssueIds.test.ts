// AA-994 guard: user-visible text in the portal never carries an internal Linear issue ID (AA-123 style).
// Issue IDs belong in code comments, tests and commit text only. This test parses every runtime source file with the
// TypeScript compiler and checks the text a page can render: JSX text, string literals and template-literal parts.
// Comments are not tokens, so they are never scanned. Run: npm test (from portal/).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

const ROOT = path.resolve(import.meta.dirname, "..");
const SCAN = ["app", "lib", "proxy.ts", "next.config.ts", "instrumentation.ts", "instrumentation-client.ts"]; // next.config: headers and redirects reach the browser
const SKIP_DIRS = new Set(["node_modules", ".next"]);
const KIT_CORE = path.join("app", "_kit", "core"); // vendored kit copy: edited and guarded in AA-CIS-App only
const ISSUE_ID = /\bAA-\d+\b/;

// Strings that look like an issue ID but never reach a screen. Changing them would break something real.
const ALLOWED: { file: string; text: string; why: string }[] = [
  { file: "lib/seal.ts", text: "aa-crm-web/AA-935/v1", why: "HKDF salt of the sealed session cookie; changing it signs everyone out" },
];

function sourceFiles(rel: string): string[] {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) return [];
  if (fs.statSync(abs).isFile()) return [rel];
  const out: string[] = [];
  for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
    const child = path.join(rel, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name) && child !== KIT_CORE) out.push(...sourceFiles(child));
    } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      out.push(child);
    }
  }
  return out;
}

function visibleStrings(rel: string, code = fs.readFileSync(path.join(ROOT, rel), "utf8")): { line: number; text: string }[] {
  const kind = rel.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(rel, code, ts.ScriptTarget.Latest, true, kind);
  const found: { line: number; text: string }[] = [];
  const visit = (node: ts.Node) => {
    const isModulePath = ts.isStringLiteral(node) && (ts.isImportDeclaration(node.parent) || ts.isExportDeclaration(node.parent));
    if (
      !isModulePath &&
      (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateHead(node) ||
        ts.isTemplateMiddle(node) || ts.isTemplateTail(node) || ts.isJsxText(node))
    ) {
      const text = ts.isJsxText(node) ? node.getText(sf) : node.text;
      if (ISSUE_ID.test(text)) found.push({ line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, text });
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

test("no user-visible string in the portal contains an internal issue ID", () => {
  const files = SCAN.flatMap(sourceFiles).map(f => f.split(path.sep).join("/"));
  assert.ok(files.length > 50, `expected the portal sources, found ${files.length} files`);
  const hits: string[] = [];
  for (const file of files) {
    for (const s of visibleStrings(file)) {
      if (ALLOWED.some(a => a.file === file && a.text === s.text)) continue;
      hits.push(`${file}:${s.line}: ${JSON.stringify(s.text.trim().slice(0, 120))}`);
    }
  }
  assert.deepEqual(hits, [], "move the issue ID into a code comment:\n" + hits.join("\n"));
});

test("the scanner sees JSX text, attributes and template parts but not comments", () => {
  const sample = [
    "// AA-1 comment",
    "/* AA-2 block */",
    "import x from \"./AA-6\";",
    "const a = <p title=\"AA-3\">see AA-4</p>;",
    "const b = `x ${a} AA-5`;",
    "const c = 'fine';",
  ].join("\n");
  const texts = visibleStrings("sample.tsx", sample).map(s => s.text.trim());
  assert.deepEqual(texts, ["AA-3", "see AA-4", "AA-5"]);
});
