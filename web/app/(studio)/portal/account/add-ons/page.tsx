"use client";
// web/app/(studio)/portal/account/add-ons/page.tsx — AA-1036 (R3, root ADR 0019 d7): Account › Add-ons (the agency's
// add-ons and the owner's request; lib/apps.ts ADD_ONS_PATH).
import { AccountAddOns } from "../../_components/AccountAddOns";

export default function AddOnsPage() {
  return <AccountAddOns />;
}
