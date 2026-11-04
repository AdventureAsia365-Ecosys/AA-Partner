"use client";
// web/app/(studio)/portal-access/SignedInAs.tsx — AA-1025 (S264): the CRM sign-in of the person on the studio access pages,
// read live from the CRM web (lib/crmIdentity.ts). Nothing is rendered when it cannot be told.
import { BRAND } from "@/app/_kit/core/brand";
import { signedInText } from "@/lib/studio/crmIdentity";
import { useCrmIdentity } from "@/lib/studio/useCrmIdentity";

export function SignedInAs() {
  const text = signedInText(useCrmIdentity());
  if (!text) return null;
  return (
    <p data-testid="portal-signed-in-as" style={{ margin: 0, fontSize: 13, color: BRAND.ink, overflowWrap: "anywhere" }}>
      {text}
    </p>
  );
}
