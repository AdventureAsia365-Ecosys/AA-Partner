// AA-1129 follow-up (S285 live check of the S283 UI audit, agency rows 6, 10, 13, 14, 15 and proposal P4): the one
// member label rule, the access log (agency timezone, member names, list sentences), Travelling now before a trip
// starts, the client host 404, the trash under a dry run, folded settings lists and the agreements page order.
// Invented names and values only.
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { type AuditEventView, type AuditPage, actorText, eventText, whenText } from "@/lib/accessLog";
import { tripWhen } from "@/lib/duty";
import { FOLD_FIRST, fold, foldToggle } from "@/lib/fold";
import { MESSAGES, plural } from "@/lib/i18n";
import { type LifecycleView, type TrashItem, trashPurgeLine } from "@/lib/lifecycle";
import { nameFromEmail, personLabel, type PersonRef } from "@/lib/people";
import { memberLabel, type Member } from "@/lib/pipeline";
import { type Agreement, splitVersions } from "@/lib/proposals";
import { memberLabel as teamLabel } from "@/lib/team";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { DataLifecycleView } = await import("@/components/settings/DataLifecycleView");
const { AgreementsView } = await import("@/components/settings/AgreementsView");
const { AccessLogView } = await import("@/components/settings/AccessLogView");
const { FoldToggle } = await import("@/components/FoldToggle");

const WEB = path.resolve(__dirname, "..");
const read = (rel: string) => readFileSync(path.join(WEB, rel), "utf8");

const OWNER = "a1000000-0000-4000-8000-000000000001";
const AGENT = "a2000000-0000-4000-8000-000000000002";
const GONE = "a3000000-0000-4000-8000-000000000003";

describe("one member label rule (rows 10 and 15)", () => {
  it("names a member by name, then by place in the role, then by role; 'Former member' when unknown", () => {
    expect(personLabel("en", { role: "agent", display_name: "mira.solano", seq: 2 })).toBe("mira.solano · Agent");
    expect(personLabel("en", { role: "agent", display_name: null, seq: 2 })).toBe("Agent 2");
    expect(personLabel("en", { role: "assistant" })).toBe("Assistant");
    expect(personLabel("en", null)).toBe("Former member");
    expect(personLabel("vi", null)).toBe("Thành viên cũ");
    // A name that still holds an address (an older API) never shows the address.
    expect(personLabel("en", { role: "owner", display_name: "mira@lotus.example" })).toBe("mira · Owner");
  });

  it("cuts an address to the part before the '@', like the members API", () => {
    expect(nameFromEmail("tam.vo@lotus.example")).toBe("tam.vo");
    expect(nameFromEmail("  ")).toBeNull();
    expect(nameFromEmail(null)).toBeNull();
    expect(nameFromEmail(`${"x".repeat(80)}@lotus.example`)).toHaveLength(64);
  });

  it("the Team page, the forms' owner picker and the menus never show an address or an id", () => {
    const labels = [
      teamLabel({ role: "agent", email: "tam.vo@lotus.example" }, "en", 1),
      teamLabel({ role: "agent", email: null }, "en", 3),
      memberLabel("en", { member_id: AGENT, role: "agent", status: "active", is_me: false }),
    ];
    expect(labels).toEqual(["tam.vo · Agent", "Agent 3", "Agent"]);
    for (const l of labels) {
      expect(l).not.toContain("@");
      expect(l).not.toMatch(/[0-9a-f]{4}/);
    }
    // Every caller goes through the rule: no bare id fallback left in the calendar's team links.
    expect(read("components/calendar/FeedCard.tsx")).not.toContain("member_id.slice(");
  });
});

function event(over: Partial<AuditEventView> = {}): AuditEventView {
  return {
    event_id: "c1000000-0000-4000-8000-000000000001",
    at: "2026-11-19T01:36:00+00:00",
    actor_kind: "user",
    actor_group: "user",
    actor_ref: `member:${OWNER}`,
    member_id: OWNER,
    member_role: "owner",
    action: "list_view",
    outcome: "allowed",
    object_kind: "member",
    object_id: null,
    fields: ["email_enc"],
    reason: "members",
    object_ids: [OWNER, AGENT],
    ...over,
  };
}

const DIRECTORY = new Map<string, PersonRef>([
  [OWNER, { role: "owner", display_name: "mira.solano", seq: 1 }],
  [AGENT, { role: "agent", display_name: null, seq: 2 }],
]);

describe("access log (row 10)", () => {
  it("names the actor by the member rule, never 'Owner b85c6b16'", () => {
    expect(actorText(event(), "en", DIRECTORY)).toBe("mira.solano · Owner");
    expect(actorText(event({ member_id: AGENT, member_role: "agent" }), "en", DIRECTORY)).toBe("Agent 2");
    // A member the agency no longer has, with the directory loaded.
    expect(actorText(event({ member_id: GONE, member_role: "agent" }), "en", DIRECTORY)).toBe("Former member");
    // The members API did not answer: the role alone.
    expect(actorText(event(), "en", null)).toBe("Owner");
    expect(actorText(event(), "en", DIRECTORY)).not.toMatch(/[0-9a-f]{8}/);
  });

  it("words a list view in full (was 'viewed the list the agency')", () => {
    const line = eventText(event(), "en", DIRECTORY);
    expect(line).toBe("viewed the team members · E-mail · reason: team members · 2 items shown");
    expect(line).not.toContain("the list the agency");
    expect(eventText(event(), "vi", DIRECTORY)).toContain("đã xem danh sách thành viên");
    // A kind without its own sentence keeps a readable fallback (S285: was "viewed a list: supplier bill").
    expect(eventText(event({ object_kind: "supplier_bill", fields: [], reason: null, object_ids: [] }), "en")).toBe(
      "viewed the supplier bill list",
    );
    // A list sentence never ends with a dangling "of" / "for" (lesson S262).
    for (const lang of ["en", "vi"] as const) {
      for (const [k, v] of Object.entries(MESSAGES[lang])) if (k.startsWith("log.list.")) expect(v, k).not.toMatch(/\b(of|for)$/);
    }
  });

  it("names a member object by the rule: the actor's own sign-in is 'the agency', another member by its label", () => {
    expect(eventText(event({ action: "sign_in", object_id: OWNER, fields: [], reason: null, object_ids: [] }), "en", DIRECTORY)).toBe(
      "signed in to the agency",
    );
    expect(
      eventText(event({ action: "member_remove", object_id: AGENT, fields: ["status"], reason: null, object_ids: [] }), "en", DIRECTORY),
    ).toBe("removed team member Agent 2 · status");
  });

  it("prints the time in the agency's timezone, so the server render and the browser agree (hydration #418)", () => {
    expect(whenText("2026-11-19T01:36:00+00:00", "en-AU", "Asia/Ho_Chi_Minh")).toBe("19 Nov 2026 08:36");
    expect(whenText("2026-11-19T20:30:00+00:00", "en-AU", "Asia/Ho_Chi_Minh")).toBe("20 Nov 2026 03:30");
    expect(whenText("2026-11-19T01:36:00+00:00", "en-AU", "Not/AZone")).toBe("19 Nov 2026 01:36");
    expect(whenText("2026-11-19T01:36:00+00:00", "en-AU")).toBe("19 Nov 2026 01:36");
  });

  it("the page renders the same text whatever the process timezone", () => {
    const page: AuditPage = { events: [event()], next_cursor: null, retention_months: 12, oldest: "2025-11-19T00:00:00+00:00", timezone: "Asia/Ho_Chi_Minh" };
    const members: Member[] = [
      { member_id: OWNER, role: "owner", status: "active", is_me: true, display_name: "mira.solano", seq: 1 },
      { member_id: AGENT, role: "agent", status: "active", is_me: false, display_name: null, seq: 2 },
    ];
    const render = () => renderToString(createElement(AccessLogView, { initial: page, members, filter: {}, lang: "en", locale: "en-AU" }));
    const before = process.env.TZ;
    try {
      process.env.TZ = "America/New_York";
      const a = render();
      process.env.TZ = "UTC";
      const b = render();
      expect(a).toBe(b);
      expect(a).toContain("19 Nov 2026 08:36");
      expect(a).toContain("mira.solano · Owner");
      expect(a).not.toContain("Me ");
    } finally {
      process.env.TZ = before;
    }
    expect(read("app/(crm)/settings/access-log/page.tsx")).toContain('"members"');
  });
});

describe("Travelling now before a trip starts (row 6)", () => {
  it("says 'Starts in N days' before the start date and 'Day N' during the trip", () => {
    expect(tripWhen({ start_date: "2026-11-20", day_no: 1 }, "2026-11-19")).toEqual({ kind: "starts", n: 1 });
    expect(tripWhen({ start_date: "2026-11-25", day_no: 1 }, "2026-11-19")).toEqual({ kind: "starts", n: 6 });
    expect(tripWhen({ start_date: "2026-11-19", day_no: 1 }, "2026-11-19")).toEqual({ kind: "day", n: 1 });
    expect(tripWhen({ start_date: "2026-11-15", day_no: 5 }, "2026-11-19")).toEqual({ kind: "day", n: 5 });
    expect(tripWhen({ start_date: "2026-11-10", day_no: null }, "2026-11-19")).toEqual({ kind: "leaves" });
    expect(tripWhen({ start_date: null, day_no: null }, "2026-11-19")).toEqual({ kind: "leaves" });
    expect(plural("en", "duty.trip.startsIn", 1)).toBe("Starts tomorrow");
    expect(plural("en", "duty.trip.startsIn", 6)).toBe("Starts in 6 days");
    expect(plural("vi", "duty.trip.startsIn", 6)).toBe("Khởi hành sau 6 ngày");
    // The page reads the view's `today` (the agency's timezone), not the server clock.
    expect(read("app/(crm)/travelling/page.tsx")).toContain("tripWhen(trip, view.data.today)");
  });
});

describe("client host 404 (white-label)", () => {
  it("has no catch-all placeholder page under /c any more (it answered 200 with the partner brand)", () => {
    expect(existsSync(path.join(WEB, "app/c/[...slug]/page.tsx"))).toBe(false);
    expect(existsSync(path.join(WEB, "app/c/_not-found"))).toBe(false);
    for (const lang of ["en", "vi"] as const) expect(MESSAGES[lang]).not.toHaveProperty("client.title");
  });
});

const LIFECYCLE: LifecycleView = {
  rules: [],
  settings: { trash_days: 7, auto_archive_months: 36 },
  lost_reasons: [],
  archive_reasons: [],
  hold_reasons: ["other"],
  deletion_dry_run: true,
  backup_purge_days: 30,
  role: "owner",
  holds: [],
  runs: [],
  archived_inactive: [],
  offboarding: null,
};

function trashItem(i: number, purge: string): TrashItem {
  return { kind: "task", id: `d${String(i).padStart(7, "0")}-0000-4000-8000-000000000000`, label: `Call the lantern shop ${i}`, deleted_at: "2026-11-01T00:00:00Z", deleted_by: null, purge_after: purge };
}

describe("trash and folded settings lists (rows 13, 14)", () => {
  it("words the removal date by the dry-run switch and the date", () => {
    const now = "2026-11-19T00:00:00Z";
    expect(trashPurgeLine("2026-11-08T00:00:00Z", true, now, "en", "en-AU")).toBe("Would have been removed on 8 Nov 2026");
    expect(trashPurgeLine("2026-11-25T00:00:00Z", true, now, "en", "en-AU")).toBe("Would be removed on 25 Nov 2026");
    expect(trashPurgeLine("2026-11-25T00:00:00Z", false, now, "en", "en-AU")).toBe("Removed for good on 25 Nov 2026");
    expect(trashPurgeLine("2026-11-08T00:00:00Z", false, now, "en", "en-AU")).toBe("Removed for good at the next daily clean-up");
    expect(trashPurgeLine("2026-11-08T00:00:00Z", true, now, "vi", "vi-VN")).toContain("Lẽ ra đã bị xoá");
  });

  it("pages a long trash and never says 'Removed for good' on a past date under a dry run", () => {
    const trash = Array.from({ length: 23 }, (_, i) => trashItem(i + 1, "2026-11-08T00:00:00Z"));
    const html = renderToString(
      createElement(DataLifecycleView, { view: LIFECYCLE, trash, tenantId: "t-1", now: "2026-11-19T00:00:00Z", lang: "en", locale: "en-AU" }),
    );
    expect(html).toContain("data-trash-dry-run");
    expect(html).toContain("Would have been removed on 8 Nov 2026");
    expect(html).not.toContain("Removed for good on");
    expect(html.match(/data-testid="data-trash-item"/g)?.length).toBe(10); // the first page of the kit DataTable
  });

  it("folds a list after its first rows with 'Show all n' / 'Show fewer'", () => {
    const rows = Array.from({ length: 8 }, (_, i) => i);
    expect(fold(rows, false)).toEqual({ shown: rows.slice(0, FOLD_FIRST), hidden: 8 - FOLD_FIRST });
    expect(fold(rows, true)).toEqual({ shown: rows, hidden: 0 });
    expect(fold(rows.slice(0, 3), false)).toEqual({ shown: [0, 1, 2], hidden: 0 });
    expect(foldToggle(8, false)).toBe("all");
    expect(foldToggle(8, true)).toBe("fewer");
    expect(foldToggle(FOLD_FIRST, false)).toBeNull();
    const all = renderToString(createElement(FoldToggle, { total: 8, open: false, onToggle: () => {}, lang: "en", testId: "f" }));
    expect(all).toContain("Show all 8");
    expect(all).toContain('aria-expanded="false"');
    expect(renderToString(createElement(FoldToggle, { total: 8, open: true, onToggle: () => {}, lang: "vi" }))).toContain("Thu gọn");
    expect(renderToString(createElement(FoldToggle, { total: 3, open: false, onToggle: () => {}, lang: "en" }))).toBe("");
    const portability = read("components/settings/DataPortabilityView.tsx");
    expect(portability).toContain("fold(exportsList, allExports)");
    expect(portability).toContain("fold(view.requests, all)");
  });
});

function agreement(i: number, kind: Agreement["kind"], status: Agreement["status"]): Agreement {
  return { doc_id: `e${i}000000-0000-4000-8000-000000000000`, kind, version_label: `v${i}`, status, body_text: `Terms text ${i}`, body_sha256: "x", published_at: null };
}

describe("agreements page (P4)", () => {
  it("shows the New version form first, then current versions, and folds retired ones", () => {
    const docs = [agreement(1, "waiver", "retired"), agreement(2, "waiver", "published"), agreement(3, "booking_terms", "draft"), agreement(4, "booking_terms", "retired")];
    const { current, older } = splitVersions(docs);
    expect(current.map((d) => d.version_label)).toEqual(["v3", "v2"]);
    expect(older.map((d) => d.version_label)).toEqual(["v4", "v1"]);
    const html = renderToString(createElement(AgreementsView, { docs, lang: "en", locale: "en-AU" }));
    const form = html.indexOf('data-testid="agreement-form"');
    const firstRow = html.indexOf('data-testid="agreement-row"');
    expect(form).toBeGreaterThan(-1);
    expect(form).toBeLessThan(firstRow);
    const older$ = html.indexOf('data-testid="agreement-older"');
    expect(older$).toBeGreaterThan(firstRow);
    expect(html.slice(older$)).toContain("Older versions");
    expect(html.slice(older$)).toContain("v4");
    expect(html.slice(0, older$)).not.toContain(">v4<");
  });
});
