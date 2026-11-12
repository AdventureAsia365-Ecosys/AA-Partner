// web/lib/studio/publishCheck.ts
// AA-1088 (M3, S278 audit) — CIS refuses the first publish of a piece it could not fully confirm against the trip
// (POST /v1/publish-log/{piece_id}/publish answers 409 with body {detail, code: "content_needs_final_check",
// can_publish_anyway}). The account owner may then publish it anyway (body {"publish_anyway": true}, audited by CIS);
// an editor is asked to hand it to an owner. The tenant BFF does not forward response headers, so the code is read
// from the body. No React: unit-tested in tests/studio/publishCheck.test.ts.

/** The refusal code CIS sends for a piece that needs a final read before it goes live. */
export const NEEDS_FINAL_CHECK = "content_needs_final_check";

export interface FinalCheckRefusal {
  /** True when this person (the account owner) may publish it anyway. */
  canPublishAnyway: boolean;
}

/** The final-check refusal in a publish answer, or null for any other answer. */
export function finalCheckRefusal(status: number, body: unknown): FinalCheckRefusal | null {
  if (status !== 409 || !body || typeof body !== "object") return null;
  const b = body as { code?: unknown; can_publish_anyway?: unknown };
  if (b.code !== NEEDS_FINAL_CHECK) return null;
  return { canPublishAnyway: b.can_publish_anyway === true };
}

/** The request of a publish click: a plain publish has no body; "Publish anyway" sends the owner's choice. */
export function publishRequestInit(publishAnyway: boolean): RequestInit {
  if (!publishAnyway) return { method: "POST" };
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ publish_anyway: true }),
  };
}
