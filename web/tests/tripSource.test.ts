// AA-1089 (Odoo study rank 48): the trip header names the AA tour and departure the trip came from. Invented names only.
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TripSourceLine } from "@/components/pipeline/TripSourceLine";
import { MESSAGES, type Lang, type MessageKey } from "@/lib/i18n";
import { tripSourceEn } from "@/lib/i18nTripSource";
import { aaSourceHref, type AaSourceView } from "@/lib/pipeline";

const LANGS: Lang[] = ["en", "vi"];
const TOUR_REF = "6b0f7a1c-2d3e-4f50-8a61-9c7d5e3b1a20";

function source(over: Partial<AaSourceView> = {}): AaSourceView {
  return {
    origin: "web_booking",
    tour_source_id: TOUR_REF,
    title: "Lotus Valley Walk",
    served_state: "served",
    departure_id: "dep-lotus-0412",
    departure_date: "2027-04-12",
    page_url: `https://go.example.test/trip/${TOUR_REF}`,
    ...over,
  };
}

const render = (s: AaSourceView, lang: Lang = "en") =>
  renderToString(createElement(TripSourceLine, { source: s, lang, locale: lang === "vi" ? "vi-VN" : "en-AU" }));

describe("trip source line", () => {
  it("shows the tour, the departure date, Booked online and the tour page link", () => {
    const html = render(source());
    expect(html).toContain('data-testid="trip-aa-source"');
    expect(html).toContain('data-origin="web_booking"');
    expect(html).toContain("Lotus Valley Walk");
    expect(html).toContain("Departs 12 Apr 2027");
    expect(html).toContain("Booked online");
    expect(html).toContain(`href="https://go.example.test/trip/${TOUR_REF}"`);
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("leaves out what is not known: no title, no date, no page, not booked online", () => {
    const html = render(source({ origin: "aa_line", title: null, departure_date: null, page_url: null }));
    expect(html).toContain("An AA tour");
    expect(html).not.toContain("trip-aa-source-departure");
    expect(html).not.toContain("trip-aa-source-link");
    expect(html).not.toContain("Booked online");
  });

  it("words the line in Vietnamese when the agent chose it", () => {
    const html = render(source(), "vi");
    expect(html).toContain("Tour AA");
    expect(html).toContain("Khởi hành");
    expect(html).toContain("Đặt trực tuyến");
  });

  it("renders nothing when the trip names no AA tour", () => {
    expect(renderToString(createElement(TripSourceLine, { source: null, lang: "en", locale: "en-AU" }))).toBe("");
    expect(renderToString(createElement(TripSourceLine, { lang: "en", locale: "en-AU" }))).toBe("");
  });

  it("links only an http(s) page", () => {
    expect(aaSourceHref(source())).toBe(`https://go.example.test/trip/${TOUR_REF}`);
    expect(aaSourceHref(source({ page_url: null }))).toBeNull();
    expect(aaSourceHref(source({ page_url: "javascript:alert(1)" }))).toBeNull();
    expect(aaSourceHref(source({ page_url: "not a url" }))).toBeNull();
  });

  it("words every key in EN and VI, never with an issue id", () => {
    const keys = Object.keys(tripSourceEn) as MessageKey[];
    for (const lang of LANGS) {
      for (const key of keys) {
        expect(MESSAGES[lang][key], `${lang} ${key}`).toBeTruthy();
        expect(MESSAGES[lang][key]).not.toMatch(/AA-\d+/);
      }
    }
    expect(MESSAGES.vi["tripSource.page"]).not.toBe(MESSAGES.en["tripSource.page"]);
  });
});
