// web/components/mail/MailboxBanner.tsx — the red "mailbox disconnected" banner (AA-945, R24: client messages are held
// until the mailbox is connected again; the owner also gets the AA system mail alert).
import { K } from "@/app/_kit";
import { t, type Lang } from "@/lib/i18n";

export function MailboxBanner({ lang }: { lang: Lang }) {
  return (
    <div
      role="alert"
      data-testid="mailbox-banner"
      style={{
        display: "flex",
        gap: 10,
        alignItems: "center",
        flexWrap: "wrap",
        padding: "10px 14px",
        marginBottom: 12,
        borderRadius: 10,
        background: K.dangerTint,
        border: `1px solid ${K.danger}`,
        color: K.ink,
        fontSize: 13,
      }}
    >
      <span>{t(lang, "mail.banner")}</span>
      <a href="/settings/mailbox" style={{ fontWeight: 600 }}>
        {t(lang, "mail.bannerLink")}
      </a>
    </div>
  );
}
