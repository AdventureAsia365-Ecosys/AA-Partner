// web/lib/dates.ts
// AA-935 — locale-aware dates (card CRM-07 "locale-aware date input/display", R32; design v3 §2.3 "dates follow the
// locale; anything sent out spells the month").
//
// A stored date is an ISO calendar date (YYYY-MM-DD) or an ISO instant. Display always spells the month, so a date
// never reads two ways: 2026-04-03 is "3 Apr" for en-AU / en-GB / vi-VN readers ("3 thg 4") and "Apr 3" for en-US.
// Typing a date follows the locale's field order: "03/04" is 3 April for en-AU, en-GB and vi-VN and March 4 for en-US;
// the input shows the spelled reading next to the field (components/DateInput.tsx), so the person sees which one the
// CRM understood. Everything is computed in UTC for calendar dates (no time-zone drift across midnight).
//
// Edge-safe: Intl only.

export type DateLocale = "en-AU" | "en-GB" | "en-US" | "vi-VN";
export const DATE_LOCALES: readonly DateLocale[] = ["en-AU", "en-GB", "en-US", "vi-VN"];

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

export function isDateLocale(value: unknown): value is DateLocale {
  return typeof value === "string" && (DATE_LOCALES as readonly string[]).includes(value);
}

function validDate(y: number, m: number, d: number): Date | null {
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d)) return null;
  if (y < 1900 || y > 2200 || m < 1 || m > 12 || d < 1 || d > 31) return null;
  const date = new Date(Date.UTC(y, m - 1, d));
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d ? date : null;
}

/** An ISO calendar date or instant as a Date (calendar dates at UTC midnight), or null. */
export function toDate(value: string | Date | null | undefined): Date | null {
  if (value == null || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const m = ISO_DATE.exec(value);
  if (m) return validDate(Number(m[1]), Number(m[2]), Number(m[3]));
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function toIsoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function fmt(locale: DateLocale, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat(locale, { timeZone: "UTC", ...options });
}

/** "3 Apr 2026" (en-AU, en-GB), "Apr 3, 2026" (en-US), "3 thg 4, 2026" (vi-VN). "—" for nothing. */
export function formatDate(value: string | Date | null | undefined, locale: DateLocale, opts: { year?: boolean } = {}) {
  const d = toDate(value);
  if (!d) return "—";
  return fmt(locale, { day: "numeric", month: "short", ...(opts.year === false ? {} : { year: "numeric" }) }).format(d);
}

/** Day and month only: "3 Apr" (en-AU) / "Apr 3" (en-US) — the card's E test. */
export function formatDayMonth(value: string | Date | null | undefined, locale: DateLocale): string {
  return formatDate(value, locale, { year: false });
}

/** For anything sent out (e-mails, proposals): the month in full words, "3 April 2026" / "April 3, 2026". */
export function formatOutward(value: string | Date | null | undefined, locale: DateLocale): string {
  const d = toDate(value);
  if (!d) return "—";
  return fmt(locale, { day: "numeric", month: "long", year: "numeric" }).format(d);
}

const INSTANT: Intl.DateTimeFormatOptions = {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
};

/**
 * An instant (ISO date-time) in a named IANA zone, with the zone's label: "5 Nov 2026, 11:23 am GMT+7". Agency views
 * pass the agency timezone (the API's `timezone`), never the server's (UTC) or the browser's, so the server render and
 * the browser print the same time (AA-952 S261 F2; the calendar fix of #142). An unknown zone prints UTC, labelled.
 * "" for nothing; an unreadable value is returned as is.
 */
export function formatInstant(value: string | null | undefined, locale: DateLocale, timeZone: string): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  try {
    return new Intl.DateTimeFormat(locale, { ...INSTANT, timeZone }).format(d);
  } catch {
    return new Intl.DateTimeFormat(locale, { ...INSTANT, timeZone: "UTC" }).format(d);
  }
}

/** True when the locale writes the day before the month in numeric dates. */
export function dayFirst(locale: DateLocale): boolean {
  return locale !== "en-US";
}

/** The typing hint shown in a date field. */
export function inputPattern(locale: DateLocale): string {
  return dayFirst(locale) ? "DD/MM/YYYY" : "MM/DD/YYYY";
}

const NUMERIC = /^(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2}|\d{4}))?$/;

/**
 * What a person typed, read in their locale's order, as an ISO date; null when it is not a real date.
 * ISO input (2026-04-03) is accepted as is. A missing year takes `defaultYear` (the current UTC year by default);
 * a two-digit year is 20YY.
 */
export function parseDateInput(
  raw: string,
  locale: DateLocale,
  defaultYear: number = new Date().getUTCFullYear(),
): string | null {
  const text = (raw ?? "").trim();
  if (!text) return null;
  const iso = ISO_DATE.exec(text);
  if (iso) {
    const d = validDate(Number(iso[1]), Number(iso[2]), Number(iso[3]));
    return d ? toIsoDate(d) : null;
  }
  const m = NUMERIC.exec(text);
  if (!m) return null;
  const a = Number(m[1]);
  const b = Number(m[2]);
  const year = m[3] === undefined ? defaultYear : m[3].length === 2 ? 2000 + Number(m[3]) : Number(m[3]);
  const [day, month] = dayFirst(locale) ? [a, b] : [b, a];
  const d = validDate(year, month, day);
  return d ? toIsoDate(d) : null;
}
