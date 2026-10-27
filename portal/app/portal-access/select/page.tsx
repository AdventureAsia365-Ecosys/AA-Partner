// portal/app/portal-access/select/page.tsx
// AA-979 P2b — the agency chooser of a person who belongs to several Content studios (CIS POST /auth/tenant-session
// answered 409 choose_tenant). The list comes from CIS GET /auth/tenant-memberships with the partner access token
// (the CRM web's session, read only); each choice POSTs /api/auth/tenant-select, which exchanges for that tenant.
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { API_URL } from "@/lib/auth-server";
import { config } from "@/lib/config";
import { memberships, partnerSession, safeStudioNext } from "@/lib/tenantSession";
import { BRAND } from "../../_kit/core/brand";
import { AccessCard, buttonStyle, linkStyle } from "../AccessCard";
import { SignOutButton } from "../SignOutButton";

export const dynamic = "force-dynamic";

const ROLE: Record<string, string> = { owner: "Owner", editor: "Editor", viewer: "Viewer" };

export default async function SelectTenantPage({ searchParams }: { searchParams: Promise<{ next?: string; error?: string }> }) {
  const params = await searchParams;
  const next = safeStudioNext(params.next);
  const cfg = config();
  const partner = partnerSession(cfg, await cookies());
  if (!partner) redirect(`${cfg.signInPath}?next=${encodeURIComponent(`/portal-access/select?next=${encodeURIComponent(next)}`)}`);
  const list = await memberships(API_URL, partner.at);
  if (list !== null && list.length === 0) redirect("/portal-access/none");
  return (
    <AccessCard title="Choose a Content studio" testId="portal-select-tenant">
      {params.error && (
        <p role="alert" style={{ margin: 0, fontSize: 13.5, color: BRAND.danger }}>
          That agency could not be opened. Choose again.
        </p>
      )}
      {list === null ? (
        <p role="alert" style={{ margin: 0, fontSize: 13.5, color: BRAND.danger }}>
          The Content studio is not reachable right now. Try again in a minute.
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
                <div style={{ fontSize: 12, color: BRAND.muted }}>{ROLE[m.content_studio_role] ?? m.content_studio_role}</div>
              </div>
              <form method="post" action="/api/auth/tenant-select">
                <input type="hidden" name="tenant_id" value={m.tenant_id} />
                <input type="hidden" name="next" value={next} />
                <button type="submit" style={buttonStyle}>
                  Open
                </button>
              </form>
            </li>
          ))}
        </ul>
      )}
      <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
        <a href="/" style={linkStyle}>
          Go to AA Partner
        </a>
        <SignOutButton />
      </div>
    </AccessCard>
  );
}
