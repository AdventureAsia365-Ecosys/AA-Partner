// AA-1069 / AA-1070 guard (E2E Dev red after the kit-only web): a kit button takes its test id through the `testId`
// prop. The kit's button family (Button, ChipButton, ListButton, Pressable, ... in app/_kit/core/Buttons.tsx) builds
// the DOM props with `domProps()`, which sets `data-testid` from `testId` AFTER the native props, so a plain
// `data-testid="..."` attribute on one of them is silently dropped: the page renders, every unit test that does not
// query that id passes, and Playwright waits forever for the control (the inquiry form's destination chips, the
// planning form's interest chips, the board's bulk move). This test parses every runtime source file with the
// TypeScript compiler and refuses a `data-testid` JSX attribute on any kit component whose body calls `domProps(`.
// The vendored kit core itself is not scanned (it is edited and guarded in AA-CIS-App only).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { expect, test } from "vitest";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SCAN = ["app", "components", "lib"];
const SKIP_DIRS = new Set(["node_modules", ".next"]);
const KIT_CORE = path.join("app", "_kit", "core");
const BUTTONS = path.join(ROOT, KIT_CORE, "Buttons.tsx");

function sourceFiles(rel: string): string[] {
  const abs = path.join(ROOT, rel);
  if (!fs.existsSync(abs)) return [];
  if (fs.statSync(abs).isFile()) return [rel];
  const out: string[] = [];
  for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
    const child = path.join(rel, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name) && child !== KIT_CORE) out.push(...sourceFiles(child));
    } else if (entry.name.endsWith(".tsx") && !/\.test\.tsx$/.test(entry.name)) {
      out.push(child);
    }
  }
  return out;
}

function parse(file: string, code = fs.readFileSync(file, "utf8")): ts.SourceFile {
  return ts.createSourceFile(file, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

/** The exported kit components whose DOM props come from `domProps()` (and so overwrite `data-testid`). */
export function domPropsComponents(code = fs.readFileSync(BUTTONS, "utf8")): Set<string> {
  const sf = parse(BUTTONS, code);
  const names = new Set<string>();
  for (const s of sf.statements) {
    if (!ts.isFunctionDeclaration(s) || !s.name || !s.body) continue;
    const exported = s.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
    if (exported && /\bdomProps\(/.test(s.body.getText(sf))) names.add(s.name.text);
  }
  return names;
}

/** `data-testid` attributes on kit button elements in one file, as "file:line <Tag>". */
export function droppedTestIds(rel: string, kit: Set<string>, code?: string): string[] {
  const sf = parse(rel, code ?? fs.readFileSync(path.join(ROOT, rel), "utf8"));
  const local = new Map<string, string>(); // local name -> kit name
  for (const s of sf.statements) {
    if (!ts.isImportDeclaration(s) || !ts.isStringLiteral(s.moduleSpecifier) || !/(^|\/)_kit(\/|$)/.test(s.moduleSpecifier.text)) continue;
    const bindings = s.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const el of bindings.elements) {
      const original = (el.propertyName ?? el.name).text;
      if (kit.has(original)) local.set(el.name.text, original);
    }
  }
  if (!local.size) return [];
  const found: string[] = [];
  const visit = (node: ts.Node) => {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && local.has(node.tagName.getText(sf))) {
      for (const attr of node.attributes.properties) {
        if (ts.isJsxAttribute(attr) && attr.name.getText(sf) === "data-testid") {
          const line = sf.getLineAndCharacterOfPosition(attr.getStart(sf)).line + 1;
          found.push(`${rel.split(path.sep).join("/")}:${line} <${node.tagName.getText(sf)}>`);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

test("the kit's domProps family is found (Button, ChipButton, ListButton, Pressable)", () => {
  const kit = domPropsComponents();
  for (const name of ["Button", "ChipButton", "ListButton", "Pressable"]) expect(kit.has(name)).toBe(true);
});

test("the scanner catches a data-testid on a kit button, aliased or not, and leaves testId and native tags alone", () => {
  const kit = domPropsComponents();
  const code = [
    'import { ChipButton, Button as KitButton } from "@/app/_kit";',
    "export function X() {",
    "  return (<div>",
    '    <ChipButton selected data-testid="chip-a">A</ChipButton>',
    '    <KitButton data-testid="btn-b">B</KitButton>',
    '    <ChipButton selected testId="chip-c">C</ChipButton>',
    '    <button data-testid="native-d">D</button>',
    "  </div>);",
    "}",
  ].join("\n");
  expect(droppedTestIds("components/x.tsx", kit, code)).toEqual(["components/x.tsx:4 <ChipButton>", "components/x.tsx:5 <KitButton>"]);
});

test("no kit button in the web app takes a data-testid attribute (use the testId prop)", () => {
  const kit = domPropsComponents();
  const offenders = SCAN.flatMap(sourceFiles).flatMap((rel) => droppedTestIds(rel, kit));
  expect(offenders).toEqual([]);
});
