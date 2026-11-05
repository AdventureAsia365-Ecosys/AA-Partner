// web/components/CenteredCard.tsx
// AA-935 — the frame of the pages outside the shell (sign-out, agency chooser, health, client host): brand mark, one
// card centred on the cream ground. Server component; the frame is the kit PageFrame (the page's one <main>, marked for
// the visual job), restyled as a centred column (AA-1008 S6f).
import { K, PageFrame, alpha } from "@/app/_kit";

/** The Cognito sign-in route (app/auth/sign-in/route.ts): a route handler that redirects to the hosted sign-in, so
 *  links to it stay plain <a> (a full page load, never a client-side navigation). */
export const SIGN_IN_PATH = "/auth/sign-in";

export function CenteredCard({ children, testId, wide = false }: { children: React.ReactNode; testId?: string; wide?: boolean }) {
  return (
    <PageFrame
      testId={testId}
      style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: "32px 16px", background: K.bg, overflowY: "visible" }}
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
            boxShadow: `0 1px 2px ${alpha(K.ink, 6)}, 0 12px 32px ${alpha(K.ink, 6)}`,
          }}
        >
          {children}
        </div>
      </div>
    </PageFrame>
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
  color: K.onAccent,
  fontWeight: 600,
  fontSize: 13,
  textDecoration: "none",
  border: `1px solid ${K.accent}`,
  cursor: "pointer",
};
