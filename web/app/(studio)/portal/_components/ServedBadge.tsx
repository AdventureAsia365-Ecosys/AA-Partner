// web/app/(studio)/portal/_components/ServedBadge.tsx
// AA-986 L3 (root ADR 0015 decision 5, Q1 / Q2 / Q7) — the persistent badge on My Tours, Content and Publish.
// The API decides what to show (`served_notice` on every /v1 row: shared/tour_liveness.py ServedState.notice):
//   "discontinued" → "AA no longer serves this tour" (the tour was retired: deleted at the source or its Master
//                    trashed). The tenant keeps everything it made; new work on it is refused with 409.
//   "paused"       → "Paused" (AA paused the tour; it may come back). No notification is sent for a pause (Q1).
//   null           → nothing (served, or a supersede waiting for its new version: invisible to the tenant, Q2).
import { Badge, translator, useLang, type UiLang } from "@/app/_kit";
import { fmtDate } from "@/lib/studio/format";
import { COMMON_MESSAGES } from "./common.messages";

/** The served fields every tenant /v1 row about a tour carries (AA-986 L3). */
export interface ServedFields {
  served_status?: "served" | "paused" | "retired" | null;
  served_reason?: string | null;
  discontinued_at?: string | null;
  served_notice?: "discontinued" | "paused" | null;
}

export function servedTitle(f: ServedFields | null | undefined, lang: UiLang = "en"): string | undefined {
  if (!f?.served_notice) return undefined;
  const t = translator(COMMON_MESSAGES, lang);
  const since = f.discontinued_at ? t("servedSince", fmtDate(f.discontinued_at, lang)) : "";
  return f.served_notice === "discontinued" ? t("servedDiscontinued", since) : t("servedPaused", since);
}

/** True when new work on the row's tour is refused (the API answers 409 tour_discontinued / tour_paused). */
export function workClosed(f: ServedFields | null | undefined): boolean {
  return f?.served_status === "retired" || f?.served_status === "paused";
}

/** The line shown instead of the Publish button when publishing is closed (null = open), with the 409's meaning:
 *  retired = closed for good; paused = for now; a supersede waiting for its new version (paused /
 *  superseded_pending) = a neutral line, never a discontinuation (Q2). */
export function publishClosedLabel(f: ServedFields | null | undefined, lang: UiLang = "en"): string | null {
  const t = translator(COMMON_MESSAGES, lang);
  if (f?.served_status === "retired") return t("publishClosedRetired");
  if (f?.served_status === "paused") {
    return f.served_reason === "superseded_pending" ? t("publishClosedSuperseded") : t("publishClosedPaused");
  }
  return null;
}

/** The unpublish dialog's sentence about what happens to the piece afterwards, per served state (only `retired`
 *  says the tour is no longer served; a pause is temporary; a supersede stays neutral, Q2). */
export function afterUnpublishSentence(f: ServedFields | null | undefined, lang: UiLang = "en"): string {
  const t = translator(COMMON_MESSAGES, lang);
  if (f?.served_status === "retired") return t("afterUnpublishRetired");
  if (f?.served_status === "paused") {
    return f.served_reason === "superseded_pending" ? t("afterUnpublishSuperseded") : t("afterUnpublishPaused");
  }
  return t("afterUnpublish");
}

export function ServedBadge({ fields }: { fields: ServedFields | null | undefined }) {
  const lang = useLang();
  if (!fields?.served_notice) return null;
  const t = translator(COMMON_MESSAGES, lang);
  return (
    <span title={servedTitle(fields, lang)} style={{ display: "inline-flex", maxWidth: "100%" }}>
      {fields.served_notice === "discontinued"
        ? <Badge tone="danger">{t("badgeDiscontinued")}</Badge>
        : <Badge tone="warning">{t("badgePaused")}</Badge>}
    </span>
  );
}

/** The tenant sentence of an API error: drops the machine code the 409s of AA-986 start with
 *  ("tour_discontinued: …", "tour_paused: …"), keeps any other message as it is. */
export function plainError(e: unknown, fallback: string): string {
  const msg = e instanceof Error ? e.message : typeof e === "string" ? e : "";
  return (msg || fallback).replace(/^(tour_discontinued|tour_paused):\s*/, "");
}
