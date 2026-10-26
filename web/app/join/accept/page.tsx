// web/app/join/accept/page.tsx — after sign-in, accept the invitation (AA-939). Needs a session (the proxy sends a
// visitor without one to sign-in). The form POSTs /join/confirm, which binds the membership to this account. EN/VI.
import { cookies } from "next/headers";
import { CenteredCard, linkButton } from "@/components/CenteredCard";
import { K } from "@/app/_kit";
import { t, type MessageKey } from "@/lib/i18n";
import { inviteCookieNames, joinError, openInvite } from "@/lib/invite";
import { contentStudioHref } from "@/lib/nav";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function JoinAcceptPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; studio?: string }>;
}) {
  const { error, studio } = await searchParams;
  const { lang, cfg } = await serverContext();
  // AA-979 P2b: after a Content studio invitation was accepted (/join/confirm).
  if (studio === "activated" || studio === "accepted") {
    const href = contentStudioHref(cfg.contentStudioUrl, cfg.publicOrigin) ?? "/";
    return (
      <CenteredCard testId="crm-join-studio">
        <div style={{ display: "grid", gap: 14, justifyItems: "start" }}>
          <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{t(lang, "join.acceptTitle")}</h1>
          <p role="status" style={{ margin: 0, fontSize: 13.5, color: K.muted }} data-testid="join-studio-status">
            {t(lang, studio === "activated" ? "join.studioReady" : "join.studioPending")}
          </p>
          <a href={href} style={linkButton} data-testid="join-to-studio">
            {t(lang, "join.toStudio")}
          </a>
        </div>
      </CenteredCard>
    );
  }
  const jar = await cookies();
  const token = cfg.authConfigured ? openInvite(cfg, jar.get(inviteCookieNames(cfg).token)?.value) : null;
  const problem = error ? joinError(error) : !token ? "no_invitation" : null;
  return (
    <CenteredCard testId="crm-join-accept">
      <div style={{ display: "grid", gap: 14, justifyItems: "start" }}>
        <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{t(lang, "join.acceptTitle")}</h1>
        {problem ? (
          <>
            <p role="alert" style={{ margin: 0, fontSize: 13.5, color: K.danger }} data-testid="join-error">
              {t(lang, `join.error.${problem}` as MessageKey)}
            </p>
            <a href="/" style={{ fontSize: 13 }}>
              {t(lang, "join.toApp")}
            </a>
          </>
        ) : (
          <form method="post" action="/join/confirm" style={{ display: "grid", gap: 10 }}>
            <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{t(lang, "join.acceptHelp")}</p>
            <button type="submit" style={linkButton} data-testid="join-accept">
              {t(lang, "join.accept")}
            </button>
          </form>
        )}
      </div>
    </CenteredCard>
  );
}
