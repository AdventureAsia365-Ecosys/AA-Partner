// web/app/join/invite/page.tsx — public: the invitation landing (AA-939, card CRM-11 "invite -> active < 5 min").
// Before the claim: "Open invitation" (POST /join/claim). After it: the address to sign in with and, for a new
// account, the one-time temporary password (from the sealed claim cookie, lib/invite.ts), then "Continue to sign in"
// -> managed login (own password + authenticator app) -> /join/accept. EN/VI.
import { cookies } from "next/headers";
import { CenteredCard, linkButton } from "@/components/CenteredCard";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { SubmitOnceForm } from "@/components/SubmitOnceForm";
import { K } from "@/app/_kit";
import { t, type MessageKey } from "@/lib/i18n";
import { inviteCookieNames, joinError, openInvite, shownAnswer } from "@/lib/invite";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const code: React.CSSProperties = {
  fontFamily: "'JetBrains Mono', monospace",
  fontSize: 15,
  padding: "8px 10px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.bg,
  color: K.ink,
  overflowWrap: "anywhere",
  userSelect: "all",
};

export default async function JoinInvitePage({ searchParams }: { searchParams: Promise<{ error?: string }> }) {
  const { error } = await searchParams;
  const { lang, locale, cfg } = await serverContext();
  const jar = await cookies();
  const names = inviteCookieNames(cfg);
  const token = cfg.authConfigured ? openInvite(cfg, jar.get(names.token)?.value) : null;
  // Only an answer of THIS invitation (S263: a stale cookie of an earlier link showed another address).
  const claim = cfg.authConfigured ? shownAnswer(cfg, jar, token) : null;
  const problem = error ? joinError(error) : !token ? "no_invitation" : null;
  return (
    <CenteredCard testId="crm-join-invite">
      <div style={{ display: "grid", gap: 14, justifyItems: "start" }}>
        <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{t(lang, "join.title")}</h1>
        {problem ? (
          <p role="alert" style={{ margin: 0, fontSize: 13.5, color: K.danger }} data-testid="join-error">
            {t(lang, `join.error.${problem}` as MessageKey)}
          </p>
        ) : claim ? (
          <div style={{ display: "grid", gap: 10, width: "100%" }} data-testid="join-claimed">
            <p style={{ margin: 0, fontSize: 13.5, color: K.ink }}>{t(lang, "join.signInAs")}</p>
            <div style={code} data-testid="join-email">
              {claim.email}
            </div>
            {claim.temporaryPassword ? (
              <>
                <p style={{ margin: 0, fontSize: 13.5, color: K.ink }}>{t(lang, "join.temporaryPassword")}</p>
                <div style={code} data-testid="join-temporary-password">
                  {claim.temporaryPassword}
                </div>
                <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{t(lang, "join.temporaryHelp")}</p>
              </>
            ) : (
              <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>
                {t(lang, claim.state === "existing" ? "join.existingHelp" : "join.alreadyClaimedHelp")}
              </p>
            )}
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{t(lang, "join.mfaHelp")}</p>
            <a href={`/auth/sign-in?next=${encodeURIComponent("/join/accept")}`} style={linkButton} data-testid="join-sign-in">
              {t(lang, "join.continue")}
            </a>
          </div>
        ) : (
          <SubmitOnceForm action="/join/claim" label={t(lang, "join.open")} pendingLabel={t(lang, "join.opening")} testId="join-open">
            <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{t(lang, "join.openHelp")}</p>
          </SubmitOnceForm>
        )}
        <LocaleSwitch locale={locale} label={t(lang, "shell.language")} example={t(lang, "shell.dateExample")} next="/join/invite" />
      </div>
    </CenteredCard>
  );
}
