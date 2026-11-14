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

type Grouped = Extract<MdBlock, { kind: "para" | "quote" | "ul" | "ol" | "table" }>;

/** Line by line: each kind of line has its own small step (complexity budget of the code-health ratchet). */
class BlockReader {
  readonly blocks: MdBlock[] = [];
  private cur: Grouped | null = null;
  private fence: string[] | null = null;

  flush(): void {
    if (this.cur) this.blocks.push(this.cur);
    this.cur = null;
  }

  /** Start a new grouped block unless the current one is of the same kind (then `add` extends it). */
  private group(kind: Grouped["kind"], add: (b: Grouped) => void, make: () => Grouped): void {
    if (this.cur?.kind === kind) add(this.cur);
    else {
      this.flush();
      this.cur = make();
    }
  }

  private fenced(raw: string): boolean {
    if (this.fence === null) return false;
    if (FENCE_RE.test(raw)) {
      this.blocks.push({ kind: "code", text: this.fence.join("\n") });
      this.fence = null;
    } else this.fence.push(raw);
    return true;
  }

  private single(raw: string): boolean {
    if (FENCE_RE.test(raw)) {
      this.flush();
      this.fence = [];
      return true;
    }
    if (!raw.trim()) {
      this.flush();
      return true;
    }
    if (RULE_RE.test(raw) && !TABLE_RE.test(raw)) {
      this.flush();
      this.blocks.push({ kind: "rule" });
      return true;
    }
    const h = HEADING_RE.exec(raw);
    if (!h) return false;
    this.flush();
    this.blocks.push({ kind: "heading", level: h[1].length, text: h[2] });
    return true;
  }

  private list(raw: string): boolean {
    const b = BULLET_RE.exec(raw);
    const m = b ?? ORDERED_RE.exec(raw);
    if (!m) return false;
    const kind = b ? "ul" : "ol";
    this.group(kind, (c) => (c as { items: string[] }).items.push(m[1]), () => ({ kind, items: [m[1]] }));
    return true;
  }

  private grouped(raw: string): boolean {
    if (TABLE_RE.test(raw)) {
      this.group("table", (c) => ((c as { text: string }).text += `\n${raw.trim()}`), () => ({ kind: "table", text: raw.trim() }));
      return true;
    }
    const q = QUOTE_RE.exec(raw);
    if (!q) return false;
    this.group("quote", (c) => (c as { lines: string[] }).lines.push(q[1]), () => ({ kind: "quote", lines: [q[1]] }));
    return true;
  }

  /** A wrapped line of a list item continues that item. */
  private continuation(raw: string): boolean {
    const c = this.cur;
    if (!c || (c.kind !== "ul" && c.kind !== "ol") || !/^\s{2,}\S/.test(raw)) return false;
    c.items[c.items.length - 1] += ` ${raw.trim()}`;
    return true;
  }

  line(raw: string): void {
    if (this.fenced(raw) || this.single(raw) || this.grouped(raw) || this.list(raw) || this.continuation(raw)) return;
    this.group("para", (c) => (c as { lines: string[] }).lines.push(raw.trim()), () => ({ kind: "para", lines: [raw.trim()] }));
  }

  end(): MdBlock[] {
    if (this.fence !== null) this.blocks.push({ kind: "code", text: this.fence.join("\n") });
    this.fence = null;
    this.flush();
    return this.blocks;
  }
}

export function parseBlocks(text: string): MdBlock[] {
  const reader = new BlockReader();
  for (const raw of (text ?? "").split(/\r?\n/)) reader.line(raw);
  return reader.end();
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
