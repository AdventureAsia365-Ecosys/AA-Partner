// AA-1019 S262 live findings on the staff console's agency page.
// D1: after the owner ends AA support the page said "You have no open session in this agency." because the API listed
// only sessions still marked active. The API now lists the caller's recent sessions with an end_reason; the page names
// the reason of the latest one (no further call) and the history labels each ended session.
// O2: the settings card showed raw keys and values ("ai enabled false", "ai budget usd 2.00"). Every key of
// api/app/support/reads.py SETTINGS_KEYS (read from that file) has an EN and a VI label and a formatted value.
import { readFileSync } from "node:fs";
import path from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SessionHistory, sessionStatusLabel } from "@/components/staff/SessionHistory";
import { MESSAGES, type Lang } from "@/lib/i18n";
import { END_REASONS, ENDED_NOTICE_MS, endedNoticeKey, endReason, type SessionView } from "@/lib/staffConsole";
import { SETTING_KEYS, settingLabel, settingRows, settingValue } from "@/lib/staffSettings";

const LANGS: Lang[] = ["en", "vi"];
const TENANT = "00000000-0000-4000-8000-000000000001";
const NOW = new Date("2026-04-02T09:00:00Z");

function session(over: Partial<SessionView> = {}): SessionView {
  return {
    tenant_id: TENANT,
    session_id: "11111111-1111-4111-8111-111111111111",
    kind: "support",
    status: "active",
    ticket_ref: "SUP-12",
    reason_code: null,
    show_identity: false,
    started_at: "2026-04-02T07:00:00Z",
    expires_at: "2026-04-02T15:00:00Z",
    ended_at: null,
    end_reason: null,
    ...over,
  };
}

/** The string items of a Python ``NAME = (...)`` tuple literal in api/app (one line or many; no ")" inside). */
function pythonTuple(file: string, name: string): string[] {
  const source = readFileSync(path.resolve(__dirname, "../../api/app", file), "utf8");
  const start = source.search(new RegExp(`^${name}\\b[^=\\n]*=`, "m"));
  expect(start, `${name} in api/app/${file}`).toBeGreaterThanOrEqual(0);
  const body = source.slice(start, source.indexOf(")", start));
  const items = [...body.matchAll(/"([a-z][a-z0-9_]*)"/g)].map((m) => m[1]);
  expect(items.length, `${name} has items`).toBeGreaterThan(0);
  return items;
}

describe("D1: why a session is over", () => {
  it("the web knows every end reason of the API, with an EN and a VI notice and history label", () => {
    expect([...END_REASONS]).toEqual(pythonTuple("support/sessions.py", "END_REASONS"));
    for (const lang of LANGS) {
      for (const r of END_REASONS) {
        expect(MESSAGES[lang][`staff.ended.${r}`], `${lang} staff.ended.${r}`).toBeTruthy();
        expect(MESSAGES[lang][`staff.session.endReason.${r}`], `${lang} endReason.${r}`).toBeTruthy();
      }
    }
  });

  it("the owner ending AA support names the owner, not 'no open session'", () => {
    const revoked = session({ status: "revoked", end_reason: "owner_ended", ended_at: "2026-04-02T08:30:00Z" });
    expect(endedNoticeKey([revoked], NOW)).toBe("staff.ended.owner_ended");
    expect(MESSAGES.en["staff.ended.owner_ended"]).toBe("This session has ended. The agency owner ended AA support.");
    expect(MESSAGES.en["staff.ended.owner_ended"]).not.toBe(MESSAGES.en["staff.refused.no_support_session"]);
  });

  it("each reason, a live session, an old session and no session", () => {
    const ended = (end_reason: SessionView["end_reason"], status: SessionView["status"]) =>
      session({ status, end_reason, ended_at: "2026-04-02T08:00:00Z" });
    expect(endedNoticeKey([ended("staff_ended", "ended")], NOW)).toBe("staff.ended.staff_ended");
    expect(endedNoticeKey([ended("session_expired", "expired")], NOW)).toBe("staff.ended.session_expired");
    expect(endedNoticeKey([ended("grant_expired", "expired")], NOW)).toBe("staff.ended.grant_expired");
    // A live session: no notice (the page reads the agency).
    expect(endedNoticeKey([session(), ended("owner_ended", "revoked")], NOW)).toBeNull();
    expect(endedNoticeKey([], NOW)).toBeNull();
    // Ended more than a day ago: the plain "no open session" (the history still lists it).
    const old = session({ status: "revoked", end_reason: "owner_ended", ended_at: new Date(NOW.getTime() - ENDED_NOTICE_MS - 1000).toISOString() });
    expect(endedNoticeKey([old], NOW)).toBeNull();
    // Still marked open but its window passed: no ended_at, the end of its window counts.
    const lapsed = session({ status: "expired", end_reason: "session_expired", expires_at: "2026-04-02T08:59:00Z" });
    expect(endedNoticeKey([lapsed], NOW)).toBe("staff.ended.session_expired");
  });

  it("an API without end_reason is read from the status", () => {
    expect(endReason({ status: "revoked" })).toBe("owner_ended");
    expect(endReason({ status: "ended" })).toBe("staff_ended");
    expect(endReason({ status: "expired" })).toBe("session_expired");
    expect(endReason({ status: "active" })).toBeNull();
    expect(endReason({ status: "expired", end_reason: "grant_expired" })).toBe("grant_expired");
  });

  it("the history lists ended, revoked and expired sessions with their labels and end time", () => {
    const rows = [
      session({ session_id: "a", status: "revoked", end_reason: "owner_ended", ended_at: "2026-04-02T08:30:00Z" }),
      session({ session_id: "b", status: "ended", end_reason: "staff_ended", ended_at: "2026-04-01T10:00:00Z" }),
      session({ session_id: "c", kind: "breakglass", reason_code: "service_outage", ticket_ref: "INC-7", status: "expired", end_reason: "session_expired" }),
      session({ session_id: "d" }),
    ];
    const html = renderToStaticMarkup(createElement(SessionHistory, { sessions: rows, lang: "en", locale: "en-GB" }));
    expect(html).toContain("Ended by the owner");
    expect(html).toContain("Ended by you");
    expect(html).toContain("Expired");
    expect(html).toContain("Open");
    expect(html).toContain("Service outage");
    expect(html.match(/<li/g)).toHaveLength(4);
    expect(html).toContain("8:30"); // the revoked one shows when it ended, not when it would have expired
    expect(sessionStatusLabel("vi", rows[0])).toBe("Chủ đại lý đã kết thúc");
  });

  it("the agency page names the reason with no live session and never calls the overview then", () => {
    const page = readFileSync(path.resolve(__dirname, "../app/staff/(console)/support/[tenantId]/page.tsx"), "utf8");
    expect(page).toContain("endedNoticeKey(mine.data.sessions)");
    expect(page).toMatch(/const overview = live \? await staffCall/);
  });
});

describe("O2: the agency settings card", () => {
  const API_KEYS = pythonTuple("support/reads.py", "SETTINGS_KEYS");

  it("labels every key the API returns, in its order, in EN and VI", () => {
    expect([...SETTING_KEYS]).toEqual(API_KEYS);
    for (const lang of LANGS) {
      for (const key of API_KEYS) {
        const label = settingLabel(lang, key);
        expect(label, `${lang} ${key}`).toBeTruthy();
        expect(label).not.toContain("_");
        expect(label).not.toBe(key.replace(/_/g, " "));
      }
    }
  });

  it("formats the values staff read", () => {
    expect(settingValue("en", "ai_enabled", false, "en-GB")).toBe("Off");
    expect(settingValue("vi", "ai_enabled", true, "vi-VN")).toBe("Bật");
    expect(settingValue("en", "ai_budget_usd", "2.00", "en-GB")).toBe("2.00 USD a month");
    expect(settingValue("en", "auto_archive_months", 36, "en-GB")).toBe("36 months without trips");
    expect(settingValue("en", "auto_archive_months", 1, "en-GB")).toBe("1 month without trips");
    expect(settingValue("en", "trash_days", 30, "en-GB")).toBe("30 days");
    expect(settingValue("en", "invoice_due_days", 1, "en-GB")).toBe("1 day");
    expect(settingValue("en", "stage2_auto_send", false, "en-GB")).toBe("Off");
    expect(settingValue("en", "booking_validation_level", "basic", "en-GB")).toBe("Basic");
    expect(settingValue("vi", "fx_mode", "automatic", "vi-VN")).toBe("Tự động");
    expect(settingValue("en", "fx_mode", "new_mode", "en-GB")).toBe("New mode");
    expect(settingValue("en", "onboarding_later", [], "en-GB")).toBe("None");
    expect(settingValue("en", "onboarding_done", ["connect_mailbox", "kickoff_call"], "en-GB")).toBe("Connect your mailbox, Book a kickoff call");
    expect(settingValue("en", "timezone", "Asia/Bangkok", "en-GB")).toBe("Asia/Bangkok");
    expect(settingValue("en", "base_currency", null, "en-GB")).toBe("—");
  });

  it("no row shows a raw key or a bare true/false, and a newer key is still shown", () => {
    const settings: Record<string, unknown> = {
      ai_enabled: false,
      ai_budget_usd: "2.00",
      timezone: "Asia/Bangkok",
      base_currency: "USD",
      booking_validation_level: "off",
      fx_mode: "manual",
      trash_days: 30,
      auto_archive_months: 36,
      stage2_auto_send: false,
      invoice_due_days: 14,
      service_fee_enabled: true,
      onboarding_later: ["kickoff_call"],
      onboarding_done: [],
      brand_new_flag: true,
    };
    for (const lang of LANGS) {
      const rows = settingRows(lang, settings, lang === "vi" ? "vi-VN" : "en-GB");
      expect(rows.map((r) => r.key)).toEqual([...API_KEYS, "brand_new_flag"]);
      for (const r of rows) {
        expect(r.value).not.toMatch(/^(true|false)$/);
        expect(r.label).not.toContain("_");
      }
      expect(rows.at(-1)?.label).toBe("Brand new flag");
    }
  });
});
