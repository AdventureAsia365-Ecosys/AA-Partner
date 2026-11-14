"use client";
// web/components/academy/LocalTime.tsx — AA-1105: an instant from the learning API (a retake time) in the reader's own
// time zone. The server render (and the first client render, so hydration matches) prints it in UTC, labelled; the
// browser then prints it in its own zone.
import { useHydrated } from "@/app/_kit";
import { formatInstant, type DateLocale } from "@/lib/dates";

export function LocalTime({ iso, locale }: { iso: string; locale: DateLocale }) {
  const hydrated = useHydrated();
  const zone = hydrated ? Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" : "UTC";
  return (
    <time dateTime={iso} suppressHydrationWarning>
      {formatInstant(iso, locale, zone)}
    </time>
  );
}
