// web/app/academy/signed-out/page.tsx — public: after the academy's sign-out (WORKFORCE logout_uri), or when the academy
// sign-in failed or is not configured (?reason=failed | unavailable). AA-1105.
import type { Metadata } from "next";
import { Button, K } from "@/app/_kit";
import { CenteredCard } from "@/components/CenteredCard";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { academyContext } from "@/lib/academyContext";
import { t } from "@/lib/i18n";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "AA Academy", robots: { index: false, follow: false } };

export default async function AcademySignedOutPage({ searchParams }: { searchParams: Promise<{ reason?: string }> }) {
  const { reason } = await searchParams;
  const { lang, locale, cfg } = await academyContext();
  const message =
    reason === "unavailable" || !cfg.academyAuthConfigured
      ? t(lang, "academy.unavailable")
      : reason === "failed"
        ? t(lang, "academy.failed")
        : t(lang, "academy.signedOutHelp");
  return (
    <CenteredCard testId="academy-signed-out" brand={t(lang, "academy.title")}>
      <div style={{ display: "grid", gap: 14, justifyItems: "start" }}>
        <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{t(lang, "academy.signedOut")}</h1>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }} data-testid="academy-signed-out-message">
          {message}
        </p>
        {cfg.academyAuthConfigured && (
          <form action="/academy/auth/sign-in" method="get">
            <Button type="submit" variant="primary" testId="academy-sign-in-link">
              {t(lang, "academy.signIn")}
            </Button>
          </form>
        )}
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted2 }}>{t(lang, "academy.subtitle")}</p>
        <LocaleSwitch locale={locale} label={t(lang, "shell.language")} example={t(lang, "shell.dateExample")} next="/academy/signed-out" />
      </div>
    </CenteredCard>
  );
}
