// web/app/select-agency/page.tsx — the agency chooser (AA-935). Lists the caller's own memberships (GET
// /crm/v1/memberships, crm-004 lookup); each is a same-origin POST to /auth/agency, which checks the id against the
// same list before it goes into the session. One active agency and no ?manual -> picked automatically.
import { redirect } from "next/navigation";
import { CenteredCard, linkButton } from "@/components/CenteredCard";
import { EmptyState, ErrorState, K } from "@/app/_kit";
import { fetchMemberships, lockedNotice } from "@/lib/crmApi";
import { safeNext, withRefreshMarker } from "@/lib/hosts";
import { t, type MessageKey } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";
import { accessValid } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function SelectAgencyPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; manual?: string; _rf?: string; locked?: string }>;
}) {
  const params = await searchParams;
  const next = safeNext(params.next ?? "/");
  const { cfg, session, lang } = await serverContext();
  if (!cfg.authConfigured) redirect("/signed-out?reason=unavailable");
  const self = `/select-agency?next=${encodeURIComponent(next)}`;
  if (!accessValid(session)) redirect(`/auth/refresh?next=${encodeURIComponent(self)}`);
  const found = await fetchMemberships(cfg, session);
  if (!found.ok) {
    // A 401 right after a refresh (one-shot marker) shows the error instead of looping through /auth/refresh.
    const refused = found.status === 401 && params._rf === "1";
    if (found.status === 401 && !refused) redirect(`/auth/refresh?next=${encodeURIComponent(withRefreshMarker(self))}`);
    return (
      <CenteredCard testId="crm-select-agency">
        <ErrorState message={t(lang, refused ? "error.session" : "error.api")} />
      </CenteredCard>
    );
  }
  const active = found.data.filter((m) => m.tenant_status === "active");
  if (active.length === 1 && !params.manual) redirect(`/auth/agency?next=${encodeURIComponent(next)}`);
  const signOut = (
    <form action="/auth/sign-out" method="post">
      <button type="submit" style={{ ...linkButton, background: "transparent", color: K.accentDeep }}>
        {t(lang, "shell.signOut")}
      </button>
    </form>
  );
  if (found.data.length === 0) {
    return (
      <CenteredCard testId="crm-select-agency">
        <EmptyState title={t(lang, "select.none")} description={t(lang, "select.noneHelp")} action={signOut} />
      </CenteredCard>
    );
  }
  const locked = lockedNotice(found.data, session.tid, params.locked);
  return (
    <CenteredCard testId="crm-select-agency" wide>
      {locked && (
        <div
          role="status"
          data-testid="crm-agency-locked"
          style={{ margin: "0 0 16px", padding: "10px 12px", borderRadius: 8, border: `1px solid ${K.line}`, background: K.accentTint }}
        >
          <div style={{ fontWeight: 600, color: K.danger }}>{t(lang, "locked.title")}</div>
          <div style={{ fontSize: 13, color: K.muted }}>{t(lang, "locked.help")}</div>
        </div>
      )}
      <h1 style={{ margin: "0 0 4px", fontSize: 18, color: K.ink }}>{t(lang, "select.title")}</h1>
      <p style={{ margin: "0 0 16px", fontSize: 13, color: K.muted }}>{t(lang, "select.subtitle")}</p>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
        {found.data.map((m) => (
          <li
            key={m.tenant_id}
            data-testid="agency-option"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              padding: "10px 12px",
              border: `1px solid ${K.line}`,
              borderRadius: 8,
              background: session.tid === m.tenant_id ? K.accentTint : K.card,
            }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>{m.display_name}</div>
              <div style={{ fontSize: 12, color: K.muted }}>{t(lang, `role.${m.member_role}` as MessageKey)}</div>
            </div>
            {m.tenant_status === "active" ? (
              <form action="/auth/agency" method="post">
                <input type="hidden" name="tenant_id" value={m.tenant_id} />
                <input type="hidden" name="next" value={next} />
                <button type="submit" style={linkButton}>
                  {t(lang, "select.open")}
                </button>
              </form>
            ) : (
              <span style={{ fontSize: 12, color: K.danger, fontWeight: 600 }}>{t(lang, "select.locked")}</span>
            )}
          </li>
        ))}
      </ul>
      <div style={{ marginTop: 16 }}>{signOut}</div>
    </CenteredCard>
  );
}
