// web/components/PageTitle.tsx
// AA-935 — the page heading of every agency page (display font, muted subtitle), server component.
import { K } from "@/app/_kit";

export function PageTitle({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <header style={{ marginBottom: 18 }}>
      <h1
        style={{
          margin: 0,
          fontFamily: "'Fahkwang', 'Poppins', Georgia, serif",
          fontSize: 24,
          fontWeight: 500,
          color: K.ink,
          lineHeight: 1.25,
          overflowWrap: "anywhere",
        }}
      >
        {title}
      </h1>
      {subtitle && <p style={{ margin: "6px 0 0", color: K.muted, fontSize: 13.5, maxWidth: 680 }}>{subtitle}</p>}
    </header>
  );
}

export function Panel({ title, children, testId }: { title?: string; children: React.ReactNode; testId?: string }) {
  return (
    <section
      data-testid={testId}
      style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: "14px 16px", minWidth: 0 }}
    >
      {title && (
        <h2
          style={{
            margin: "0 0 10px",
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            color: K.muted,
          }}
        >
          {title}
        </h2>
      )}
      {children}
    </section>
  );
}
