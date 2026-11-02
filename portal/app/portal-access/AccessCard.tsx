// portal/app/portal-access/AccessCard.tsx
// AA-979 P2b — the frame of the two studio access pages (chooser, no access): light card like /tenant-login, outside the
// /portal shell (which needs a studio session these pages do not have yet).
import { BRAND, FONT_DISPLAY, FONT_SANS, LOGO_SRC, SUPPORT_EMAIL } from "../_kit/core/brand";

export function AccessCard({ title, testId, children }: { title: string; testId: string; children: React.ReactNode }) {
  return (
    <main
      data-testid={testId}
      style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: BRAND.bg, padding: 16, fontFamily: FONT_SANS }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 440,
          background: BRAND.card,
          border: `1px solid ${BRAND.line}`,
          borderRadius: 12,
          padding: 24,
          display: "grid",
          gap: 14,
        }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- same static logo as /tenant-login */}
        <img src={LOGO_SRC} alt="Adventure Asia" style={{ height: 32, width: "auto" }} />
        <h1 style={{ margin: 0, fontFamily: FONT_DISPLAY, fontSize: 20, color: BRAND.ink }}>{title}</h1>
        {children}
      </div>
    </main>
  );
}

export const linkStyle: React.CSSProperties = { color: BRAND.accentDeep, fontSize: 13.5 };
export const buttonStyle: React.CSSProperties = {
  border: `1px solid ${BRAND.accentBorder}`,
  background: BRAND.card,
  color: BRAND.accentDeep,
  borderRadius: 999,
  padding: "6px 14px",
  fontSize: 13,
  cursor: "pointer",
};

/**
 * AA-1025 (S264): Adventure Asia's address, labelled as a contact. A bare address under "sign in with the address it
 * was sent to" read as the account's own address (the tech lead's "the studio shows an old session's e-mail").
 */
export function SupportContact() {
  return (
    <p data-testid="portal-support-contact" style={{ margin: 0, fontSize: 13, color: BRAND.muted }}>
      Questions? Contact Adventure Asia:{" "}
      <a href={`mailto:${SUPPORT_EMAIL}`} style={linkStyle}>
        {SUPPORT_EMAIL}
      </a>
    </p>
  );
}
