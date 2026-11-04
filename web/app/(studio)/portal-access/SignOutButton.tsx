"use client";
// web/app/(studio)/portal-access/SignOutButton.tsx — AA-979 P2b: one sign-out (studio + CRM + Cognito), lib/signOut.ts.
// AA-1036 (R3): the kit Button (was a hand-styled <button>), label in the UI language.
import { Button, useT } from "@/app/_kit";
import { signOutEverywhere } from "@/lib/studio/signOut";
import { ACCESS_MESSAGES } from "./access.messages";

export function SignOutButton() {
  const t = useT(ACCESS_MESSAGES);
  return (
    <Button size="sm" onClick={() => void signOutEverywhere()} testId="portal-sign-out">
      {t("signOut")}
    </Button>
  );
}
