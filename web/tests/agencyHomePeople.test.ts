// AA-1129 (S283 UI audit, agency app P1 CRM home and routing, P2 people and labels). Invented names and values only.
import { readFileSync } from "node:fs";
import path from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { needsYou, type AgendaView } from "@/lib/finance";
import { MESSAGES } from "@/lib/i18n";
import { memberLabel, type Member } from "@/lib/pipeline";
import { memberLabel as teamLabel, roleSeq, type TeamMember } from "@/lib/team";
import type { Conversation } from "@/lib/mail";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }), usePathname: () => "/inbox" }));
const { NeedsYou } = await import("@/components/home/NeedsYou");
const { AgendaTiles } = await import("@/components/finance/AgendaTiles");
const { UnmatchedInbox } = await import("@/components/mail/UnmatchedInbox");

const WEB = path.resolve(__dirname, "..");
const read = (rel: string) => readFileSync(path.join(WEB, rel), "utf8");

const AGENDA: AgendaView = {
  open_proposals: 2,
  deposits_due_week: 1,
  overdue_instalments: 29,
  passport_missing: 0,
  passport_days: 60,
  aa_overdue_invoices: null,
  next_departures: [
    { departure_ref: "7a1c0000-0000-4000-8000-000000000001", title: "Invented Lake Loop — 6 Days", start_date: "2031-11-27", trips: 1, pax: 3, guaranteed: null },
    { departure_ref: "SV-GROUP-1", title: null, start_date: "2031-11-30", trips: 2, pax: 4, guaranteed: null },
  ],
  departure_days: 30,
  needs_you: { tasks_overdue: 1, tasks_today: 0, waiting_on_you: 41 },
};

const member = (over: Partial<Member>): Member => ({ member_id: "d4760000-aaaa-4bbb-8ccc-000000000001", role: "agent", status: "active", is_me: false, ...over });

describe("P2: members are named, never by a bare id", () => {
  it("names a member by the API's display name and role, or by role and place", () => {
    expect(memberLabel("en", member({ display_name: "lan.nguyen", seq: 1 }))).toBe("lan.nguyen · Agent");
    expect(memberLabel("en", member({ display_name: null, seq: 2 }))).toBe("Agent 2");
    expect(memberLabel("vi", member({ role: "owner", display_name: null, seq: 1 }))).toBe("Chủ đại lý 1");
    expect(memberLabel("en", member({ is_me: true, display_name: "lan.nguyen", seq: 1 }))).toBe("Me");
  });

  it("the Team page and the forms' owner picker never print '#<id>'", () => {
    const team: TeamMember[] = [
      { member_id: "a78e8cf1-0000-4000-8000-000000000001", role: "owner", status: "active", email: null, joined_at: null, open_trips: 0, is_me: true },
      { member_id: "b85c6b16-0000-4000-8000-000000000002", role: "agent", status: "active", email: "minh@hoasen.example", joined_at: null, open_trips: 1, is_me: false },
      { member_id: "d4767297-0000-4000-8000-000000000003", role: "agent", status: "active", email: null, joined_at: null, open_trips: 0, is_me: false },
    ];
    const seq = roleSeq(team);
    const labels = team.map((m) => teamLabel(m, "en", seq.get(m.member_id)));
    expect(labels).toEqual(["Owner 1", "minh@hoasen.example", "Agent 2"]);
    expect(labels.join(" ")).not.toMatch(/#[0-9a-f]{8}/);
    expect(read("components/pipeline/FormsAdmin.tsx")).toContain("memberLabel(m, lang, seq.get(m.member_id))");
    expect(read("components/team/TeamAdmin.tsx")).not.toContain("`#${from.slice(0, 8)}`");
  });
});

describe("P2: calendar task labels", () => {
  it("words a task event with the /tasks label, not the raw kind", () => {
    const page = read("app/(crm)/calendar/page.tsx");
    expect(page).toContain("taskTitle(lang, { kind: e.task_kind, title: null })");
    expect(page).not.toContain('e.task_kind.replace(/_/g, " ")');
  });
});

describe("P1: Needs you", () => {
  it("lists what has something in it, most urgent first, with links", () => {
    expect(needsYou(AGENDA).map((i) => [i.key, i.n, i.href])).toEqual([
      ["home.needs.tasksOverdue", 1, "/tasks"],
      ["home.needs.latePayments", 29, "/payments"],
      ["home.needs.waiting", 41, "/pipeline?waiting_on=agency"],
    ]);
    expect(needsYou({ ...AGENDA, overdue_instalments: 0, needs_you: { tasks_overdue: 0, tasks_today: 0, waiting_on_you: 0 } })).toEqual([]);
    expect(needsYou({ ...AGENDA, needs_you: undefined }).map((i) => i.key)).toEqual(["home.needs.latePayments"]);
  });

  it("renders singular and plural lines, and a plain line when nothing needs the person", () => {
    const html = renderToStaticMarkup(createElement(NeedsYou, { agenda: AGENDA, lang: "en" }));
    expect(html).toContain("1 overdue task<");
    expect(html).toContain("29 late payments");
    expect(html).toContain("41 trips waiting on you");
    const empty = renderToStaticMarkup(createElement(NeedsYou, { agenda: null, lang: "vi" }));
    expect(empty).toContain("crm-home-needs-none");
    expect(empty).toContain(MESSAGES.vi["home.needs.none"]);
  });

  it("Home reads the agenda once and no longer shows a fixed empty state", () => {
    const page = read("app/(crm)/page.tsx");
    expect(page).toContain("readAgenda(ctx.cfg, session)");
    expect(page).toContain("<NeedsYou agenda={agenda} lang={lang} />");
    expect(page).not.toContain("home.needsEmpty");
  });
});

describe("P1: next group departures", () => {
  it("names a departure by its AA tour title and dates it by the date the API sends (the AA service date)", () => {
    const html = renderToStaticMarkup(createElement(AgendaTiles, { agenda: AGENDA, lang: "en", locale: "en-AU" }));
    expect(html).toContain("Invented Lake Loop — 6 Days");
    expect(html).not.toContain(">7a1c0000-0000-4000-8000-000000000001<");
    expect(html).toContain(">SV-GROUP-1<"); // a group code with no AA line keeps its code
    expect(html).toContain("27 Nov 2031");
  });
});

describe("P1: a CRM-only owner is not stuck on 'No Content studio access'", () => {
  it("the page links the CRM home for a person with an active CRM agency", () => {
    const page = read("app/(studio)/portal-access/none/page.tsx");
    expect(page).toContain("href={CRM_HOME_PATH}");
    expect(page).toContain("fetchMemberships(base.cfg, base.session)");
    expect(page).toContain('m.tenant_status === "active"');
  });
});

describe("P2: the Unmatched inbox says who wrote and about what", () => {
  const c: Conversation = {
    conversation_id: "c0000000-0000-4000-8000-000000000001",
    trip_id: null,
    traveler_id: null,
    channel: "email",
    last_message_at: "2031-10-26T08:00:00Z",
    message_count: 2,
    held_count: 0,
    last_direction: "in",
    unmatched: true,
    sender: "Minh Tran <minh@guest.example>",
    subject: "Family trip in May",
    preview: "Hello, we would like a 5-day trip…",
  };
  it("renders sender, subject, preview and a labelled count", () => {
    const html = renderToStaticMarkup(createElement(UnmatchedInbox, { conversations: [c], lang: "en", locale: "en-AU" }));
    expect(html).toContain("Minh Tran &lt;minh@guest.example&gt;");
    expect(html).toContain("Family trip in May");
    expect(html).toContain("Hello, we would like a 5-day trip…");
    expect(html).toContain("2 messages");
    const one = renderToStaticMarkup(createElement(UnmatchedInbox, { conversations: [{ ...c, message_count: 1, subject: null }], lang: "en", locale: "en-AU" }));
    expect(one).toContain("1 message<");
    expect(one).toContain("(no subject)");
  });
});
