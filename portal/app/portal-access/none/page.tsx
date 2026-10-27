// portal/app/portal-access/none/page.tsx
// AA-979 P2b — signed in with the partner pool, but CIS lists no Content studio membership for this account (403
// no_membership), or CIS could not be reached (?reason=unavailable: fail closed, nothing opens). No link back to the
// CRM home: for a person without a CRM agency the CRM sends them here again; signing out is the way on.
import { AccessCard, linkStyle } from "../AccessCard";
import { SignOutButton } from "../SignOutButton";
import { BRAND, SUPPORT_EMAIL } from "../../_kit/core/brand";

export const dynamic = "force-dynamic";

export default async function NoAccessPage({ searchParams }: { searchParams: Promise<{ reason?: string; next?: string }> }) {
  const { reason } = await searchParams;
  const unavailable = reason === "unavailable";
  return (
    <AccessCard title={unavailable ? "Content studio unavailable" : "No Content studio access"} testId="portal-no-access">
      <p style={{ margin: 0, fontSize: 13.5, color: BRAND.muted }}>
        {unavailable
          ? "The Content studio could not check your access right now. Try again in a minute."
          : "This account has no access to a Content studio yet. If you expected one, ask Adventure Asia to send you an invitation, and sign in with the address it was sent to."}
      </p>
      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        {unavailable && (
          <a href="/portal/dashboard" style={linkStyle}>
            Try again
          </a>
        )}
        <a href={`mailto:${SUPPORT_EMAIL}`} style={linkStyle}>
          {SUPPORT_EMAIL}
        </a>
        <SignOutButton />
      </div>
    </AccessCard>
  );
}
