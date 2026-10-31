// @vitest-environment jsdom
// AA-957 (S262 live evidence): the trip page showed raw codes: tasks "stage3_due_t21", "collect_deposit",
// "aa_incident" and the history line "deposit_paid → Booked". The API keeps the list of every task kind and stage
// reason it writes in api/app/task_kinds.py (its own unit test keeps that list complete); this test reads that file
// and fails when a code has no English or no Vietnamese label, then renders the trip panel with those codes.
import { readFileSync } from "node:fs";
import path from "node:path";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MESSAGES, type Lang, type MessageKey } from "@/lib/i18n";
import type { Member, TripView } from "@/lib/pipeline";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { TripPanel, historyLabel } = await import("@/components/pipeline/TripPanel");

const API = path.resolve(__dirname, "../../api/app");

/** The string items of a Python ``NAME = frozenset({...})`` or ``NAME = (...)`` / ``NAME: ... = (...)`` literal. */
function pythonStrings(file: string, name: string): string[] {
  const source = readFileSync(path.join(API, file), "utf8");
  const start = source.search(new RegExp(`^${name}\\b[^=\\n]*=`, "m"));
  expect(start, `${name} in api/app/${file}`).toBeGreaterThanOrEqual(0);
  const body = source.slice(start, source.indexOf("\n)", start));
  const items = [...body.matchAll(/"([a-z][a-z0-9_]*)"/g)].map((m) => m[1]);
  expect(items.length, `${name} has items`).toBeGreaterThan(0);
  return items;
}

const TASK_KINDS = pythonStrings("task_kinds.py", "TASK_KINDS");
const STAGE_REASONS = pythonStrings("task_kinds.py", "STAGE_REASONS");
const PREFIXES = pythonStrings("task_kinds.py", "STAGE_REASON_PREFIXES");
const LOST_REASONS = pythonStrings("pipeline.py", "LOST_REASONS");
const CANCEL_REASONS = pythonStrings("stages.py", "CANCEL_REASONS");
const LANGS: Lang[] = ["en", "vi"];

function label(lang: Lang, key: string): string | undefined {
  return MESSAGES[lang][key as MessageKey];
}

describe("every code the API writes has a label in both languages", () => {
  it("reads the API's lists (the three codes of the live evidence among them)", () => {
    expect(TASK_KINDS).toEqual(expect.arrayContaining(["stage3_due_t21", "collect_deposit", "aa_incident"]));
    expect(STAGE_REASONS).toContain("deposit_paid");
    expect(PREFIXES).toEqual(["lost_", "cancel_"]);
  });

  it.each(LANGS)("task kinds (%s)", (lang) => {
    const missing = TASK_KINDS.filter((k) => !label(lang, `task.${k}`)?.trim());
    expect(missing).toEqual([]);
  });

  it.each(LANGS)("stage-history reasons, whole codes and both families (%s)", (lang) => {
    const missing = STAGE_REASONS.filter((r) => !label(lang, `reason.${r}`)?.trim());
    expect(missing).toEqual([]);
    expect(LOST_REASONS.length).toBeGreaterThan(0);
    for (const code of [...STAGE_REASONS, ...LOST_REASONS.map((r) => `lost_${r}`), ...CANCEL_REASONS.map((r) => `cancel_${r}`)]) {
      const text = historyLabel(lang, { reason_code: code });
      expect(text, code).not.toBe(code);
      expect(text, code).not.toContain("_");
    }
  });
});

const TRIP = "4d0c3a1e-8f7b-4c2a-9e51-2b6f0a7d3c10";
const VIEW: TripView = {
  trip: {
    trip_id: TRIP,
    title: "Lotus coast, 6 nights",
    stage: "booked",
    stage_entered_at: "2026-10-31T09:00:00Z",
    waiting_on: "agency",
    start_date: "2026-11-20",
    end_date: "2026-11-26",
    month: null,
    date_flex_days: 0,
    nights: 6,
    destinations: ["vietnam"],
    party_adults: 2,
    party_child_ages: [],
    party_type: null,
    budget_band: null,
    trip_style: null,
    owner_member_id: "m1",
    is_mine: true,
    lead_source: "form",
    lost_reason_code: null,
    last_inquiry_at: "2026-10-20T09:00:00Z",
    created_at: "2026-10-20T09:00:00Z",
    intake_completeness: { s1: 100, s2: 100, s3: 40 },
  },
  party: [],
  tasks: [
    { task_id: "t1", kind: "stage3_due_t21", priority: "normal", assignee_member_id: "m1", status: "open", due_at: "2026-10-31T23:30:00Z", done_at: null },
    { task_id: "t2", kind: "collect_deposit", priority: "normal", assignee_member_id: "m1", status: "done", due_at: null, done_at: "2026-10-31T10:00:00Z" },
    { task_id: "t3", kind: "aa_incident", priority: "urgent", assignee_member_id: "m1", status: "open", due_at: null, done_at: null },
  ],
  notes: [],
  history: [{ from_stage: "proposal", to_stage: "booked", reason_code: "deposit_paid", at: "2026-10-31T10:00:00Z", actor: "member:m1" }],
  submissions: [],
  allowed_moves: ["pre_departure"],
  can_cancel: true,
  stage2_link: null,
};
const MEMBERS: Member[] = [{ member_id: "m1", role: "owner", status: "active", is_me: true }];

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
});

async function mount(element: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(element);
  });
  return container;
}

describe("trip panel: tasks and history in words", () => {
  it.each(LANGS)("shows no raw code (%s)", async (lang) => {
    const locale = lang === "vi" ? "vi-VN" : "en-AU";
    const c = await mount(createElement(TripPanel, { view: VIEW, members: MEMBERS, role: "owner", lang, locale, timeZone: "Asia/Ho_Chi_Minh" }));
    const tasks = c.querySelector('[data-testid="trip-tasks"]')!.textContent ?? "";
    const history = c.querySelector('[data-testid="trip-history"]')!.textContent ?? "";
    for (const code of ["stage3_due_t21", "collect_deposit", "aa_incident"]) expect(tasks).not.toContain(code);
    expect(tasks).toContain(MESSAGES[lang]["task.stage3_due_t21"]);
    expect(history).not.toContain("deposit_paid");
    expect(history).toContain(MESSAGES[lang]["reason.deposit_paid"]);
  });

  it("prints a task's due time in the agency's zone, the same on the server and in any browser", async () => {
    const c = await mount(createElement(TripPanel, { view: VIEW, members: MEMBERS, role: "owner", lang: "en", locale: "en-AU", timeZone: "Asia/Ho_Chi_Minh" }));
    const due = c.querySelector('[data-kind="stage3_due_t21"]')!.textContent ?? "";
    // 23:30 UTC on 31 Oct is 06:30 on 1 Nov in Ho Chi Minh City, labelled with its zone.
    expect(due).toMatch(/1 Nov 2026, 6:30\s?am GMT\+7/);
  });
});
