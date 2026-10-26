"use client";
// portal/app/portal-access/SignOutButton.tsx — AA-979 P2b: one sign-out (studio + CRM + Cognito), lib/signOut.ts.
import { signOutEverywhere } from "@/lib/signOut";
import { buttonStyle } from "./AccessCard";

export function SignOutButton() {
  return (
    <button type="button" style={buttonStyle} onClick={() => void signOutEverywhere()} data-testid="portal-sign-out">
      Sign out
    </button>
  );
}
