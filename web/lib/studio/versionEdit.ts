// web/lib/studio/versionEdit.ts
// AA-1144 follow-up (S285 live check) — what a My Content save sends to PATCH /v1/tours/versions/{id} (action edit).
// The drawer used to send the Master's name, subtitle and SEO lines with every save, so a summary edit replaced the
// agency's own name / subtitle / SEO copy with Adventure Asia's. CIS lays the sent fields over the version it edits
// (fields not sent are kept), so the save sends only the fields the agency touched. No runtime imports (npm test).

export interface DrawerEdits {
  /** The summary as edited, or null when the agency did not touch it. */
  summary: string | null;
  /** The highlights as edited, or null when the agency did not touch them. */
  highlights: string[] | null;
}

/** The edited fields only: an untouched field is left out, never filled from the Master or the version. */
export function editedFields(edits: DrawerEdits): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (edits.summary !== null) out.summary = edits.summary;
  if (edits.highlights !== null) out.highlights = edits.highlights;
  return out;
}

/** True when the save has something to send. */
export function hasEdits(edits: DrawerEdits): boolean {
  return Object.keys(editedFields(edits)).length > 0;
}
