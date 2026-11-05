// web/app/(studio)/portal-access/AccessCard.tsx
// AA-979 P2b — the frame of the two studio access pages (chooser, no access): light card like /tenant-login, outside the
// /portal shell (which needs a studio session these pages do not have yet).
// AA-1036 (R3): text in the UI language (access.messages.ts; server components: translator with the request language).
import { BRAND, FONT_DISPLAY, FONT_SANS, LOGO_SRC, SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import { PageFrame, translator } from "@/app/_kit";
import { serverContext } from "@/lib/serverContext";
import { ACCESS_MESSAGES } from "./access.messages";

export async function AccessCard({ title, testId, children }: { title: string; testId: string; children: React.ReactNode }) {
  const t = translator(ACCESS_MESSAGES, (await serverContext()).lang);
  return (
    <PageFrame
      testId={testId}
      style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: BRAND.bg, padding: 16, fontFamily: FONT_SANS, overflowY: "visible" }}
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
        <img src={LOGO_SRC} alt={t("logoAlt")} style={{ height: 32, width: "auto" }} />
        <h1 style={{ margin: 0, fontFamily: FONT_DISPLAY, fontSize: 20, color: BRAND.ink }}>{title}</h1>
        {children}
      </div>
    </PageFrame>
  );
}

export const linkStyle: React.CSSProperties = { color: BRAND.accentDeep, fontSize: 13.5 };

/**
 * AA-1025 (S264): Adventure Asia's address, labelled as a contact. A bare address under "sign in with the address it
 * was sent to" read as the account's own address (the tech lead's "the studio shows an old session's e-mail").
 */
export async function SupportContact() {
  const t = translator(ACCESS_MESSAGES, (await serverContext()).lang);
  return (
    <p data-testid="portal-support-contact" style={{ margin: 0, fontSize: 13, color: BRAND.muted }}>
      {t("support")}{" "}
      <a href={`mailto:${SUPPORT_EMAIL}`} style={linkStyle}>
        {SUPPORT_EMAIL}
      </a>
    </p>
  );
}
