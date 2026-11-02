// portal/app/portal-access/none/page.tsx
// AA-979 P2b — signed in with the partner pool, but CIS lists no Content studio membership for this account (403
// no_membership), or CIS could not be reached (?reason=unavailable: fail closed, nothing opens). No link back to the
// CRM home: for a person without a CRM agency the CRM sends them here again; signing out is the way on.
// AA-1025 (S264): the CRM sign-in is shown (SignedInAs, live from the CRM web) and the support address is labelled as
// a contact (SupportContact): a bare info@ link here read as the signed-in account's address.
import { AccessCard, linkStyle, SupportContact } from "../AccessCard";
import { SignedInAs } from "../SignedInAs";
import { SignOutButton } from "../SignOutButton";
import { BRAND } from "../../_kit/core/brand";

export const dynamic = "force-dynamic";

export default async function NoAccessPage({ searchParams }: { searchParams: Promise<{ reason?: string; next?: string }> }) {
  const { reason } = await searchParams;
  const unavailable = reason === "unavailable";
  return (
    <AccessCard title={unavailable ? "Content studio unavailable" : "No Content studio access"} testId="portal-no-access">
      <SignedInAs />
      <p style={{ margin: 0, fontSize: 13.5, color: BRAND.muted }}>
        {unavailable
          ? "The Content studio could not check your access right now. Try again in a minute."
          : "This account has no access to a Content studio yet. If you expected one, ask Adventure Asia for an invitation, then sign in to AA Partner with the e-mail address the invitation was sent to."}
      </p>
      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        {unavailable && (
          <a href="/portal/dashboard" style={linkStyle}>
            Try again
          </a>
        )}
        <SignOutButton />
      </div>
      <SupportContact />
    </AccessCard>
  );
}
