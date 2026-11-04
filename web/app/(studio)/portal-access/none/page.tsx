// web/app/(studio)/portal-access/none/page.tsx
// AA-979 P2b — signed in with the partner pool, but CIS lists no Content studio membership for this account (403
// no_membership), or CIS could not be reached (?reason=unavailable: fail closed, nothing opens). No link back to the
// CRM home: for a person without a CRM agency the CRM sends them here again; signing out is the way on.
// AA-1025 (S264): the CRM sign-in is shown (SignedInAs, live from the CRM web) and the support address is labelled as
// a contact (SupportContact): a bare info@ link here read as the signed-in account's address.
import Link from "next/link";
import { AccessCard, linkStyle, SupportContact } from "../AccessCard";
import { SignedInAs } from "../SignedInAs";
import { SignOutButton } from "../SignOutButton";
import { BRAND } from "@/app/_kit/core/brand";
import { translator } from "@/app/_kit";
import { serverContext } from "@/lib/serverContext";
import { ACCESS_MESSAGES } from "../access.messages";

export const dynamic = "force-dynamic";

export default async function NoAccessPage({ searchParams }: { searchParams: Promise<{ reason?: string; next?: string }> }) {
  const { reason } = await searchParams;
  const unavailable = reason === "unavailable";
  const t = translator(ACCESS_MESSAGES, (await serverContext()).lang);
  return (
    <AccessCard title={unavailable ? t("unavailableTitle") : t("noAccessTitle")} testId="portal-no-access">
      <SignedInAs />
      <p style={{ margin: 0, fontSize: 13.5, color: BRAND.muted }}>
        {unavailable ? t("unavailableBody") : t("noAccessBody")}
      </p>
      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        {unavailable && (
          <Link href="/portal/dashboard" style={linkStyle}>
            {t("tryAgain")}
          </Link>
        )}
        <SignOutButton />
      </div>
      <SupportContact />
    </AccessCard>
  );
}
