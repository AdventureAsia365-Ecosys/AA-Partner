// web/components/CenteredCard.tsx
// AA-935 — the frame of the pages outside the shell (sign-out, agency chooser, health, client host): brand mark, one
// card centred on the cream ground. Server component.
import { K } from "@/app/_kit";

export function CenteredCard({ children, testId, wide = false }: { children: React.ReactNode; testId?: string; wide?: boolean }) {
  return (
    <main
      data-testid={testId}
      style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: "32px 16px", background: K.bg }}
    >
      <div style={{ width: "100%", maxWidth: wide ? 640 : 440, display: "grid", gap: 16 }}>
        <div
          style={{
            fontFamily: "'Fahkwang', 'Poppins', Georgia, serif",
            fontSize: 20,
            color: K.accent,
            textAlign: "center",
          }}
        >
          Adventure Asia Partner
        </div>
        <div
          style={{
            background: K.card,
            border: `1px solid ${K.line}`,
            borderRadius: 12,
            padding: "22px 22px",
            boxShadow: "0 1px 2px rgba(31,41,51,0.06), 0 12px 32px rgba(31,41,51,0.06)",
          }}
        >
          {children}
        </div>
      </div>
    </main>
  );
}

export const linkButton: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  padding: "8px 18px",
  borderRadius: 999,
  background: K.accent,
  color: "var(--aa-on-accent)",
  fontWeight: 600,
  fontSize: 13,
  textDecoration: "none",
  border: `1px solid ${K.accent}`,
  cursor: "pointer",
};
