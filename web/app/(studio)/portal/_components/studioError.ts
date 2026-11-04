// web/app/(studio)/portal/_components/studioError.ts
// AA-1036 (R3) — how a studio error reaches the tenant in the UI language. The studio's API helpers (reviewListApi.ts,
// catalogApi.ts, marketplaceApi.ts) never throw an English sentence: they throw a StudioError with a message key of
// errors.messages.ts and the HTTP status, plus the API's own sentence (`detail`) when it sent one. The kit's
// ApiError (app/_kit/local/studio/api.ts) carries the same `detail`. The component turns any of them into text with
// plainError(e, fallback, lang).
import { ApiError, translator, type UiLang } from "@/app/_kit";
import { ERROR_MESSAGES } from "./errors.messages";

export type StudioErrorCode = keyof typeof ERROR_MESSAGES.en;

export class StudioError extends Error {
  readonly code: StudioErrorCode;
  readonly status: number;
  /** The API's own sentence (its `detail`), shown unchanged; "" when the API sent none. */
  readonly detail: string;

  constructor(code: StudioErrorCode, status = 0, detail = "") {
    // The message is for logs and developers; the UI reads code/status/detail.
    super(detail || `${code}${status ? ` ${status}` : ""}`);
    this.name = "StudioError";
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

/** The `detail` of an API error body when it is a sentence (a FastAPI 422 sends a list: not shown). */
export function detailOf(body: unknown): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  return typeof d === "string" ? d.trim() : "";
}

/**
 * The tenant sentence of an error, in the UI language:
 *   - a StudioError → the API's own sentence when it sent one, else its message key in `lang`;
 *   - the kit's ApiError → the API's sentence, else "Request failed (status)" in `lang`;
 *   - a TypeError (the browser's "Failed to fetch", a bug) → `fallback`: never a sentence for the tenant;
 *   - any other Error / string → its text (the component already wrote it in `lang`), else `fallback`.
 * Drops the machine code the 409s of AA-986 start with ("tour_discontinued: …", "tour_paused: …").
 */
export function plainError(e: unknown, fallback: string, lang: UiLang): string {
  let msg: string;
  if (e instanceof StudioError) {
    msg = e.detail || (translator(ERROR_MESSAGES, lang) as (k: string, ...a: unknown[]) => string)(e.code, e.status);
  } else if (e instanceof ApiError) {
    msg = e.detail || translator(ERROR_MESSAGES, lang)("requestFailed", e.status);
  } else if (e instanceof TypeError) {
    msg = "";
  } else {
    msg = e instanceof Error ? e.message : typeof e === "string" ? e : "";
  }
  return (msg || fallback).replace(/^(tour_discontinued|tour_paused):\s*/, "");
}
