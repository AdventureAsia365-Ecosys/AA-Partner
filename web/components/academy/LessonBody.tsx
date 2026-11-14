// web/components/academy/LessonBody.tsx — AA-1105: a lesson body or a cited section (Markdown from the learning API)
// as React elements (lib/academyMarkdown.ts: no HTML is ever injected). Server-safe: no hooks.
import { Fragment, type ReactNode } from "react";
import { K, mono, sans } from "@/app/_kit";
import { parseBlocks, parseInline, type MdInline } from "@/lib/academyMarkdown";

const HEADING_SIZE: Record<number, number> = { 1: 20, 2: 17, 3: 15, 4: 14, 5: 14, 6: 14 };

function inline(text: string, key: string): ReactNode[] {
  return parseInline(text).map((tok: MdInline, i) => {
    const k = `${key}-${i}`;
    switch (tok.kind) {
      case "code":
        return (
          <code key={k} style={{ fontFamily: mono, fontSize: "0.92em", background: K.neutralBg, padding: "1px 4px", borderRadius: 4 }}>
            {tok.text}
          </code>
        );
      case "strong":
        return <strong key={k}>{tok.text}</strong>;
      case "em":
        return <em key={k}>{tok.text}</em>;
      case "link":
        return tok.href ? (
          <a key={k} href={tok.href} target="_blank" rel="noopener noreferrer" style={{ color: K.accentDeep }}>
            {tok.text}
          </a>
        ) : (
          <span key={k} style={{ textDecoration: "underline dotted" }}>
            {tok.text}
          </span>
        );
      default:
        return <Fragment key={k}>{tok.text}</Fragment>;
    }
  });
}

const preStyle: React.CSSProperties = {
  margin: "10px 0",
  padding: "10px 12px",
  background: K.neutralBg,
  borderRadius: 8,
  fontFamily: mono,
  fontSize: 12.5,
  lineHeight: 1.5,
  overflowX: "auto",
  whiteSpace: "pre",
  color: K.ink,
};

/** `skipFirstHeading`: drop a leading H1 (the page title already shows it). */
export function LessonBody({ text, skipFirstHeading = false, testId }: { text: string; skipFirstHeading?: boolean; testId?: string }) {
  let blocks = parseBlocks(text);
  if (skipFirstHeading && blocks[0]?.kind === "heading" && blocks[0].level === 1) blocks = blocks.slice(1);
  return (
    <div style={{ fontFamily: sans, fontSize: 14.5, lineHeight: 1.65, color: K.body, overflowWrap: "anywhere", minWidth: 0 }} data-testid={testId}>
      {blocks.map((b, i) => {
        const key = `b${i}`;
        switch (b.kind) {
          case "heading":
            return (
              <div
                key={key}
                role="heading"
                aria-level={Math.min(b.level + 1, 6)}
                style={{ fontSize: HEADING_SIZE[b.level], fontWeight: 700, color: K.ink, lineHeight: 1.35, margin: "18px 0 6px" }}
              >
                {inline(b.text, key)}
              </div>
            );
          case "ul":
          case "ol": {
            const List = b.kind;
            return (
              <List key={key} style={{ margin: "8px 0", paddingLeft: 22 }}>
                {b.items.map((it, j) => (
                  <li key={`${key}-${j}`} style={{ marginBottom: 4 }}>
                    {inline(it, `${key}-${j}`)}
                  </li>
                ))}
              </List>
            );
          }
          case "quote":
            return (
              <div key={key} style={{ margin: "10px 0", padding: "2px 0 2px 12px", borderLeft: `3px solid ${K.line}`, color: K.muted }}>
                {b.lines.map((l, j) => (
                  <Fragment key={`${key}-${j}`}>
                    {j > 0 && <br />}
                    {inline(l, `${key}-${j}`)}
                  </Fragment>
                ))}
              </div>
            );
          case "code":
          case "table":
            return (
              <pre key={key} style={preStyle}>
                {b.text}
              </pre>
            );
          case "rule":
            return <hr key={key} style={{ border: "none", borderTop: `1px solid ${K.line}`, margin: "16px 0" }} />;
          default:
            return (
              <p key={key} style={{ margin: "8px 0" }}>
                {inline(b.lines.join(" "), key)}
              </p>
            );
        }
      })}
    </div>
  );
}
