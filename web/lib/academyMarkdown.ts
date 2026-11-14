// web/lib/academyMarkdown.ts
// AA-1105 — the small Markdown reader of the academy lessons and their cited sections (body_md of learn.http.v1). No
// dependency and no HTML: the text becomes blocks and inline tokens that components/academy/LessonBody.tsx turns into
// React elements, so a lesson can never inject markup. Covered: headings, paragraphs, bullet and numbered lists,
// quotes, rules, fenced code, tables (kept as monospaced text: the kit allows no raw <table> outside DataTable),
// inline code, bold, italic and links. A link opens only when it is an absolute https URL; any other target (a
// relative repo path such as ../adr/0001.md) is shown as its text. Edge-safe, unit-tested (tests/academy.test.ts).

export type MdBlock =
  | { kind: "heading"; level: number; text: string }
  | { kind: "para"; lines: string[] }
  | { kind: "quote"; lines: string[] }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "code"; text: string }
  | { kind: "table"; text: string }
  | { kind: "rule" };

export type MdInline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; text: string }
  | { kind: "em"; text: string }
  | { kind: "link"; text: string; href: string | null };

const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const BULLET_RE = /^\s*[-*+]\s+(.*)$/;
const ORDERED_RE = /^\s*\d{1,3}[.)]\s+(.*)$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const RULE_RE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const FENCE_RE = /^\s*(```|~~~)/;
const TABLE_RE = /^\s*\|.*\|\s*$/;

export function parseBlocks(text: string): MdBlock[] {
  const blocks: MdBlock[] = [];
  let cur: MdBlock | null = null;
  let fence: string[] | null = null;
  const flush = () => {
    if (cur) blocks.push(cur);
    cur = null;
  };
  for (const raw of (text ?? "").split(/\r?\n/)) {
    if (fence !== null) {
      if (FENCE_RE.test(raw)) {
        blocks.push({ kind: "code", text: fence.join("\n") });
        fence = null;
      } else fence.push(raw);
      continue;
    }
    if (FENCE_RE.test(raw)) {
      flush();
      fence = [];
      continue;
    }
    if (!raw.trim()) {
      flush();
      continue;
    }
    const c = cur as MdBlock | null;
    if (TABLE_RE.test(raw)) {
      if (c && c.kind === "table") c.text += `\n${raw.trim()}`;
      else {
        flush();
        cur = { kind: "table", text: raw.trim() };
      }
      continue;
    }
    if (RULE_RE.test(raw)) {
      flush();
      blocks.push({ kind: "rule" });
      continue;
    }
    const h = HEADING_RE.exec(raw);
    if (h) {
      flush();
      blocks.push({ kind: "heading", level: h[1].length, text: h[2] });
      continue;
    }
    const b = BULLET_RE.exec(raw);
    const o = b ? null : ORDERED_RE.exec(raw);
    if (b || o) {
      const kind = b ? "ul" : "ol";
      const item = (b ?? o)![1];
      if (c && c.kind === kind) c.items.push(item);
      else {
        flush();
        cur = { kind, items: [item] };
      }
      continue;
    }
    const q = QUOTE_RE.exec(raw);
    if (q) {
      if (c && c.kind === "quote") c.lines.push(q[1]);
      else {
        flush();
        cur = { kind: "quote", lines: [q[1]] };
      }
      continue;
    }
    // A wrapped line of a list item continues that item.
    if (c && (c.kind === "ul" || c.kind === "ol") && /^\s{2,}\S/.test(raw)) {
      c.items[c.items.length - 1] += ` ${raw.trim()}`;
      continue;
    }
    if (c && c.kind === "para") c.lines.push(raw.trim());
    else {
      flush();
      cur = { kind: "para", lines: [raw.trim()] };
    }
  }
  if (fence !== null) blocks.push({ kind: "code", text: fence.join("\n") });
  flush();
  return blocks;
}

// One inline token per match: link, code, bold (** / __), italic (* / _ with a non-word char around it).
const INLINE_RE =
  /\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+?)\*\*|__([^_]+?)__|(^|[^\w*])\*([^\s*][^*\n]*?)\*(?![\w*])|(^|[^\w_])_([^\s_][^_\n]*?)_(?![\w_])/g;

/** An absolute https URL a lesson may open, or null (relative paths, other schemes). */
export function safeHref(raw: string): string | null {
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && !url.username && !url.password ? url.toString() : null;
  } catch {
    return null;
  }
}

export function parseInline(text: string): MdInline[] {
  const out: MdInline[] = [];
  let last = 0;
  const push = (t: string) => {
    if (t) out.push({ kind: "text", text: t });
  };
  const re = new RegExp(INLINE_RE.source, "g");
  for (let m = re.exec(text); m; m = re.exec(text)) {
    const [whole, linkText, href, code, b1, b2, p1, i1, p2, i2] = m;
    push(text.slice(last, m.index));
    if (linkText !== undefined) out.push({ kind: "link", text: linkText, href: safeHref(href) });
    else if (code !== undefined) out.push({ kind: "code", text: code });
    else if (b1 !== undefined || b2 !== undefined) out.push({ kind: "strong", text: (b1 ?? b2)! });
    else if (i1 !== undefined) {
      push(p1);
      out.push({ kind: "em", text: i1 });
    } else {
      push(p2);
      out.push({ kind: "em", text: i2 });
    }
    last = m.index + whole.length;
  }
  push(text.slice(last));
  return out;
}
