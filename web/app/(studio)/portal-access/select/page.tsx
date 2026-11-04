// web/app/(studio)/portal-access/select/page.tsx
// AA-979 P2b — the agency chooser of a person who belongs to several Content studios (CIS POST /auth/tenant-session
// answered 409 choose_tenant). The list comes from CIS GET /auth/tenant-memberships with the partner access token
// (the CRM web's session, read only); each choice POSTs /api/auth/tenant-select, which exchanges for that tenant.
// AA-1025 (S264): the CRM sign-in (SignedInAs) and the labelled support contact, as on /portal-access/none.
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { config } from "@/lib/config";
import { memberships, partnerSession, safeStudioNext } from "@/lib/studio/tenantSession";
import { BRAND } from "@/app/_kit/core/brand";
import { CRM_HOME_PATH } from "@/lib/nav";
import { Button, translator } from "@/app/_kit";
import { serverContext } from "@/lib/serverContext";
import { AccessCard, linkStyle, SupportContact } from "../AccessCard";
import { ACCESS_MESSAGES } from "../access.messages";
import { SignedInAs } from "../SignedInAs";
import { SignOutButton } from "../SignOutButton";

export const dynamic = "force-dynamic";

const ROLE = { owner: "roleOwner", editor: "roleEditor", viewer: "roleViewer" } as const;

function roleKey(role: string): (typeof ROLE)[keyof typeof ROLE] | null {
  return Object.hasOwn(ROLE, role) ? ROLE[role as keyof typeof ROLE] : null;
}

export default async function SelectTenantPage({ searchParams }: { searchParams: Promise<{ next?: string; error?: string }> }) {
  const params = await searchParams;
  const t = translator(ACCESS_MESSAGES, (await serverContext()).lang);
  const next = safeStudioNext(params.next);
  const cfg = config();
  const partner = partnerSession(cfg, await cookies());
  if (!partner) redirect(`/auth/refresh?next=${encodeURIComponent(`/portal-access/select?next=${encodeURIComponent(next)}`)}`);
  const list = cfg.cisApiUrl ? await memberships(cfg.cisApiUrl, partner.at) : null;
  if (list !== null && list.length === 0) redirect("/portal-access/none");
  return (
    <AccessCard title={t("chooseTitle")} testId="portal-select-tenant">
      <SignedInAs />
      {params.error && (
        <p role="alert" style={{ margin: 0, fontSize: 13.5, color: BRAND.danger }}>
          {t("chooseError")}
        </p>
      )}
      {list === null ? (
        <p role="alert" style={{ margin: 0, fontSize: 13.5, color: BRAND.danger }}>
          {t("unreachable")}
        </p>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
          {list.map((m) => (
            <li
              key={m.tenant_id}
              data-testid="studio-option"
              style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", border: `1px solid ${BRAND.line}`, borderRadius: 8 }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, color: BRAND.ink, overflowWrap: "anywhere" }}>{m.tenant_name}</div>
                <div style={{ fontSize: 12, color: BRAND.muted }}>{(() => { const k = roleKey(m.content_studio_role); return k ? t(k) : m.content_studio_role; })()}</div>
              </div>
              <form method="post" action="/api/auth/tenant-select">
                <input type="hidden" name="tenant_id" value={m.tenant_id} />
                <input type="hidden" name="next" value={next} />
                <Button type="submit" size="sm">
                  {t("open")}
                </Button>
              </form>
            </li>
          ))}
        </ul>
      )}
      <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
        <a href={CRM_HOME_PATH} style={linkStyle}>
          {t("goToPartner")}
        </a>
        <SignOutButton />
      </div>
      <SupportContact />
    </AccessCard>
  );
}
