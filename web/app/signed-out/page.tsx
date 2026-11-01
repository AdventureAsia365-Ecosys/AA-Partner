// web/app/signed-out/page.tsx — public: after sign-out (Cognito logout_uri), or when sign-in failed / is not
// configured (?reason=failed | unavailable). AA-935.
import { CenteredCard, linkButton } from "@/components/CenteredCard";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { K } from "@/app/_kit";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function SignedOutPage({ searchParams }: { searchParams: Promise<{ reason?: string }> }) {
  const { reason } = await searchParams;
  const { lang, locale, cfg } = await serverContext();
  const message =
    reason === "unavailable" || !cfg.authConfigured
      ? t(lang, "auth.unavailable")
      : reason === "failed"
        ? t(lang, "auth.failed")
        : t(lang, "auth.signedOutHelp");
  return (
    <CenteredCard testId="crm-signed-out">
      <div style={{ display: "grid", gap: 14, justifyItems: "start" }}>
        <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{t(lang, "auth.signedOut")}</h1>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }} data-testid="signed-out-message">
          {message}
        </p>
        {cfg.authConfigured && (
          <a href="/auth/sign-in" style={linkButton} data-testid="sign-in-link">
            {t(lang, "auth.signIn")}
          </a>
        )}
        <LocaleSwitch locale={locale} label={t(lang, "shell.language")} example={t(lang, "shell.dateExample")} next="/signed-out" />
      </div>
    </CenteredCard>
  );
}
