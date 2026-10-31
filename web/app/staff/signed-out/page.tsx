// web/app/staff/signed-out/page.tsx — public: after the staff console's sign-out (WORKFORCE logout_uri), or when the
// staff sign-in failed, is not configured or the account has no staff group (?reason=failed | unavailable | no_group).
// AA-1019.
import { CenteredCard } from "@/components/CenteredCard";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { Button, K } from "@/app/_kit";
import { t } from "@/lib/i18n";
import { staffContext } from "@/lib/staffContext";

export const dynamic = "force-dynamic";

export default async function StaffSignedOutPage({ searchParams }: { searchParams: Promise<{ reason?: string }> }) {
  const { reason } = await searchParams;
  const { lang, locale, cfg } = await staffContext();
  const message =
    reason === "unavailable" || !cfg.staffAuthConfigured
      ? t(lang, "staff.unavailable")
      : reason === "failed"
        ? t(lang, "staff.failed")
        : reason === "no_group"
          ? t(lang, "staff.noGroup")
          : t(lang, "staff.signedOutHelp");
  return (
    <CenteredCard testId="staff-signed-out">
      <div style={{ display: "grid", gap: 14, justifyItems: "start" }}>
        <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{t(lang, "staff.signedOut")}</h1>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }} data-testid="staff-signed-out-message">
          {message}
        </p>
        {cfg.staffAuthConfigured && (
          <form action="/staff/auth/sign-in" method="get">
            <Button type="submit" variant="primary" testId="staff-sign-in-link">
              {t(lang, "staff.signIn")}
            </Button>
          </form>
        )}
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted2 }}>{t(lang, "staff.subtitle")}</p>
        <LocaleSwitch locale={locale} label={t(lang, "shell.language")} next="/staff/signed-out" />
      </div>
    </CenteredCard>
  );
}
