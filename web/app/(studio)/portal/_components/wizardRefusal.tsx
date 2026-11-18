"use client";
// web/app/(studio)/portal/_components/wizardRefusal.tsx
// AA-1126 L1 (S283 review fix) — how the Create wizard (AngleGateWizard.tsx) shows a refused request. CIS answers 402
// ai_quota_exhausted with an OBJECT detail ({code, message, resets_at}) on POST .../angle-gate/requests/{id}/goal and
// POST .../content-writing/requests/{id}/write when the plan's AI writing allowance is used up. Rendering that detail
// crashed the page (an object is not a React child) or showed "[object Object]". Now:
//   refusalFromResponse  a failed response -> the allowance StudioError (studioError.allowanceError) or an Error with
//                        the API's sentence (string details only, detailOf);
//   refusalOf            any caught error -> the allowance StudioError (kept, so the banner shows) or the plain text;
//   WizardRefusal        the allowance banner (the reason + Upgrade, AllowanceHeld.tsx) or the red error box.
// Kept out of AngleGateWizard.tsx so that file and its function stay inside the code-health ratchet.

import { K, type UiLang } from "@/app/_kit";
import { AllowanceBanner } from "./AllowanceHeld";
import { allowanceError, detailOf, isAllowanceError, plainError, type StudioError } from "./studioError";

/** What a wizard step shows after a refusal: a sentence, the allowance refusal, or nothing. */
export type Refusal = string | StudioError | null;

export async function refusalFromResponse(r: Response): Promise<Error> {
  const body = await r.json().catch(() => null);
  return allowanceError(r.status, body) ?? new Error(detailOf(body));
}

export function refusalOf(e: unknown, fallback: string, lang: UiLang): Refusal {
  return isAllowanceError(e) ? e : plainError(e, fallback, lang);
}

export function WizardRefusal({ refusal, hidden = false, gap = 0 }: { refusal: Refusal; hidden?: boolean; gap?: number }) {
  if (hidden || !refusal) return null;
  if (typeof refusal !== "string") {
    return <div style={{ marginBottom: gap }}><AllowanceBanner resetsAt={refusal.resetsAt} testId="wizard-allowance" /></div>;
  }
  return (
    <div data-testid="wizard-error" style={{ padding: "9px 12px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`, borderRadius: 8, fontSize: 12, color: K.danger, marginBottom: gap }}>
      {refusal}
    </div>
  );
}
