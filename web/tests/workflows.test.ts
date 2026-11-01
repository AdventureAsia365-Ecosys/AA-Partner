// @vitest-environment jsdom
// AA-959 (card CRM-29a) web: the BFF allow-list of Tasks and Automations; how a task, a trigger, a step, an outcome
// and a stop reason read in both languages; the buckets of a due time; the editor's payload; and the pages: the Tasks
// board (buckets, a custom task's own title, the source badge, the T-48 h flight links, the digest switch for the
// owner only), the automations board (owner edits, others read; a library template needing a message template), the
// trip's automations panel (the run log in words, Check then Apply a task list).
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  BUCKETS,
  bucketOf,
  actionsFor,
  canApply,
  canEdit,
  isGreeting,
  outcomeText,
  stepText,
  stopText,
  taskTitle,
  templateDescription,
  templateName,
  triggerText,
  workflowErrorKey,
  type Rule,
  type TaskItem,
  type TaskList,
  type WorkflowOverview,
} from "@/lib/workflows";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const push = vi.fn();
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push }) }));
const { TasksBoard } = await import("@/components/workflows/TasksBoard");
const { AutomationsBoard } = await import("@/components/workflows/AutomationsBoard");
const { TripWorkflows } = await import("@/components/workflows/TripWorkflows");
const { payloadOf, draftOf } = await import("@/components/workflows/RuleEditor");

const UUID = "00000000-0000-4000-8000-0000000000a5";
const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  refresh.mockReset();
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

function task(over: Partial<TaskItem> = {}): TaskItem {
  return {
    task_id: UUID,
    kind: "custom_0123456789abcdef01234567",
    title: "Book the airport pick-up",
    priority: "normal",
    status: "open",
    assignee_member_id: null,
    due_at: "2026-11-02T02:00:00+00:00",
    due_anchor: "created",
    due_offset_days: 0,
    due_overridden: false,
    done_at: null,
    created_at: "2026-11-01T02:00:00+00:00",
    trip_id: "00000000-0000-4000-8000-0000000007e1",
    trip_title: "Highlands loop",
    trip_stage: "booked",
    traveler_id: null,
    workflow_run_id: null,
    source: "manual",
    ...over,
  };
}

function list(over: Partial<TaskList> = {}): TaskList {
  const buckets = Object.fromEntries(BUCKETS.map((b) => [b, []])) as unknown as NonNullable<TaskList["buckets"]>;
  buckets.today = [task()];
  buckets.waiting_for_dates = [task({ task_id: "t2", kind: "send_pre_travel_pack", title: null, due_at: null, source: "workflow", workflow_run_id: "r1" })];
  buckets.overdue = [task({ task_id: "t3", kind: "confirm_arrival_flight", title: null, priority: "urgent", source: "workflow" })];
  return {
    scope: "mine",
    status: "open",
    timezone: "Asia/Ho_Chi_Minh",
    digest: false,
    can_set_digest: true,
    buckets,
    counts: Object.fromEntries(BUCKETS.map((b) => [b, buckets[b].length])) as NonNullable<TaskList["counts"]>,
    ...over,
  };
}

function rule(over: Partial<Rule> = {}): Rule {
  return {
    workflow_rule_id: "00000000-0000-4000-8000-0000000000b9",
    name: "Pre-departure checklist",
    template_key: null,
    template_version: null,
    trigger: { kind: "stage_enter", value: "booked", offset_days: 0 },
    conditions: {},
    steps: [{ action: "create_task", kind: "custom", title: "Book the airport pick-up" }, { action: "send_email", message_template_id: null, purpose: "marketing" }],
    status: "paused",
    version: 1,
    templates_missing: [1],
    active_runs: 2,
    finished_runs: 5,
    ...over,
  };
}

function overview(over: Partial<WorkflowOverview> = {}): WorkflowOverview {
  return {
    rules: [rule()],
    library: [
      { key: "arrival_flight_t48h", version: 1, name: "Confirm the arrival flight (T-48 h)", trigger: { kind: "date_anchor", value: "departure", offset_days: -2 }, conditions: {}, steps: [], needs_templates: false, installed: true },
      { key: "birthday_greeting", version: 1, name: "Birthday greeting", trigger: { kind: "special_date", value: "birthday", offset_days: 0 }, conditions: {}, steps: [], needs_templates: true, installed: false },
    ],
    can_edit: true,
    can_apply: true,
    options: { trigger_kinds: ["stage_enter", "event", "date_anchor", "special_date", "manual"], events: ["message.received"], date_anchors: ["departure"], special_dates: ["birthday"], due_anchors: ["created", "departure"], task_kinds: ["request_review"], merge_fields: ["traveller_first_name"], waiting_on: ["client"], condition_keys: ["stages"] },
    ...over,
  };
}

describe("the BFF allow-list", () => {
  it("lets the task and automation routes through and nothing else", () => {
    for (const [m, p] of [
      ["GET", "tasks"], ["POST", "tasks"], ["PUT", "tasks/digest"], ["GET", `tasks/${UUID}`], ["PATCH", `tasks/${UUID}`],
      ["GET", "workflows"], ["GET", "workflows/runs"], ["POST", "workflows"], ["POST", "workflows/install"],
      ["POST", `workflows/runs/${UUID}/stop`], ["GET", `workflows/${UUID}`], ["GET", `workflows/${UUID}/preview`],
      ["PATCH", `workflows/${UUID}`], ["POST", `workflows/${UUID}/status`], ["POST", `workflows/${UUID}/apply`],
    ] as const) expect(matchBff(m, p), `${m} ${p}`).not.toBeNull();
    expect(matchBff("DELETE", `workflows/${UUID}`)).toBeNull();
    expect(matchBff("POST", `workflows/${UUID}/delete`)).toBeNull();
  });
});

describe("wording", () => {
  it("words tasks, triggers, steps, outcomes and stop reasons in both languages", () => {
    expect(taskTitle("en", task())).toBe("Book the airport pick-up");
    expect(taskTitle("en", { kind: "confirm_arrival_flight", title: null })).toContain("arrival flight");
    expect(taskTitle("vi", { kind: "confirm_arrival_flight", title: null })).toContain("chuyến bay");
    expect(taskTitle("en", { kind: "made_up_kind", title: null })).toBe("made up kind");
    expect(triggerText("en", { kind: "stage_enter", value: "booked", offset_days: 0 })).toBe("When a trip enters Booked");
    expect(triggerText("en", { kind: "date_anchor", value: "departure", offset_days: -2 })).toBe("2 days before departure");
    expect(triggerText("en", { kind: "date_anchor", value: "return", offset_days: 3 })).toBe("3 days after return");
    expect(triggerText("en", { kind: "special_date", value: "birthday", offset_days: 0 })).toBe("On the client's birthday");
    expect(triggerText("en", { kind: "event", value: "message.received", offset_days: 0 })).toContain("SMS or WhatsApp");
    expect(triggerText("vi", { kind: "manual", value: null, offset_days: 0 })).toBe("Áp dụng bằng tay");
    expect(stepText("en", { action: "create_task", kind: "request_review" })).toContain("review");
    expect(stepText("en", { action: "send_email", purpose: "marketing" })).toContain("marketing");
    expect(stepText("en", { action: "send_text", channel: "whatsapp" })).toBe("Send a WhatsApp template");
    expect(stepText("en", { action: "set_field", field: "waiting_on", value: "client" })).toBe("Set waiting on: Client");
    expect(stepText("en", { action: "set_field", field: "owner", value: "member:x" })).toContain("Assign");
    expect(stepText("en", { action: "wait", days: 2, hours: 0 })).toBe("Wait 2 days 0 h");
    expect(stepText("en", { action: "wait", until: { anchor: "departure", offset_days: -7 } })).toBe("Wait until 7 days before departure");
    expect(outcomeText("en", "skipped_no_consent")).toBe("not sent: no marketing consent");
    expect(outcomeText("en", "brand_new")).toBe("brand new");
    expect(stopText("vi", "trip_cancelled")).toBe("chuyến đi đã huỷ");
    expect(stopText("en", null)).toBe("");
    expect(templateName("vi", { key: "birthday_greeting", name: "x" })).toBe("Chúc mừng sinh nhật");
    expect(templateName("en", { key: "unknown", name: "Fallback" })).toBe("Fallback");
    expect(templateDescription("en", "passport_expiry_200d")).toContain("200 days");
    expect(workflowErrorKey("version_conflict")).toBe("wf.error.version_conflict");
    expect(workflowErrorKey("something_else")).toBe("wf.error.generic");
    expect(canEdit("owner") && !canEdit("agent") && canApply("agent") && !canApply("assistant")).toBe(true);
  });

  it("has every library template, task kind and outcome in both languages", () => {
    for (const key of Object.keys(MESSAGES.en).filter((k) => k.startsWith("wf.") || k.startsWith("tasks."))) {
      expect(MESSAGES.vi[key as keyof typeof MESSAGES.vi], key).toBeTruthy();
    }
  });

  it("puts a due time in the bucket the API does", () => {
    const now = new Date("2026-11-01T03:00:00Z"); // 10:00 in Ho Chi Minh City
    const zone = "Asia/Ho_Chi_Minh";
    expect(bucketOf(null, now, zone)).toBe("waiting_for_dates");
    expect(bucketOf("2026-11-01T02:59:00Z", now, zone)).toBe("overdue");
    expect(bucketOf("2026-11-01T16:59:00Z", now, zone)).toBe("today");
    expect(bucketOf("2026-11-01T17:01:00Z", now, zone)).toBe("upcoming");
    expect(bucketOf("2026-11-20T03:00:00Z", now, zone)).toBe("later");
  });

  it("sends the editor's draft in the API's shape", () => {
    const d = draftOf(rule({ trigger: { kind: "special_date", value: "birthday", offset_days: 0 }, conditions: { stages: ["booked"] } }));
    expect(payloadOf({ ...d, name: "  Birthday  " })).toEqual({
      name: "Birthday",
      trigger: { kind: "special_date", value: "birthday", offset_days: 0 },
      conditions: {},
      steps: d.steps,
    });
    const manual = payloadOf({ ...draftOf(), trigger: { kind: "manual", value: "x", offset_days: 3 }, conditions: { stages: [], stage3_incomplete: undefined, has_children: true } });
    expect(manual.trigger).toEqual({ kind: "manual", value: null });
    expect(manual.conditions).toEqual({ has_children: true });
  });

  it("sends a birthday or anniversary message as marketing and offers no text on a sent text", () => {
    const mail = { action: "send_email", message_template_id: "t-1", purpose: "transactional" } as const;
    const text = { action: "send_text", channel: "sms", content_sid: null, variables: {} } as const;
    const task = { action: "create_task", kind: "client_birthday" } as const;
    for (const value of ["birthday", "trip_anniversary"]) {
      const body = payloadOf({ name: "Greeting", trigger: { kind: "special_date", value, offset_days: 0 }, conditions: {}, steps: [mail, text, task] });
      expect((body.steps as Record<string, unknown>[]).map((s) => s.purpose)).toEqual(["marketing", "marketing", undefined]);
      expect(isGreeting({ kind: "special_date", value })).toBe(true);
    }
    const passport = payloadOf({ name: "Passport", trigger: { kind: "special_date", value: "passport_expiry", offset_days: -200 }, conditions: {}, steps: [mail] });
    expect((passport.steps as Record<string, unknown>[])[0].purpose).toBe("transactional");
    expect(isGreeting({ kind: "event", value: "birthday" })).toBe(false);
    expect(actionsFor({ kind: "event", value: "message.sent" })).not.toContain("send_text");
    expect(actionsFor({ kind: "event", value: "message.received" })).toContain("send_text");
    expect(actionsFor({ kind: "manual", value: null })).toEqual(["create_task", "set_field", "wait"]);
    expect(actionsFor({ kind: "special_date", value: "birthday" })).not.toContain("set_field");
    expect(workflowErrorKey("marketing_only")).toBe("wf.error.marketing_only");
    expect(workflowErrorKey("message_loop")).toBe("wf.error.message_loop");
  });
});

describe("pages", () => {
  it("lists the buckets with titles, sources and the digest switch for the owner", async () => {
    const c = await mount(createElement(TasksBoard, { list: list(), scope: "mine", members: [], lang: "en", locale: "en-AU" }));
    expect(c.querySelector('[data-testid="tasks-bucket-overdue"]')?.textContent).toContain("Confirm the arrival flight");
    expect(c.querySelector('[data-testid="tasks-bucket-today"]')?.textContent).toContain("Book the airport pick-up");
    expect(c.querySelector('[data-testid="tasks-bucket-waiting_for_dates"]')?.textContent).toContain("the date fills in");
    expect(c.querySelector('[data-testid="tasks-bucket-upcoming"]')).toBeNull();
    expect([...c.querySelectorAll('[data-testid="task-line"]')].map((e) => e.getAttribute("data-source"))).toEqual(["workflow", "manual", "workflow"]);
    expect(c.querySelector('[data-testid="tasks-digest"]')).not.toBeNull();
    const none = await mount(createElement(TasksBoard, { list: list({ can_set_digest: false }), scope: "team", members: [], lang: "vi", locale: "vi-VN" }));
    expect(none.querySelector('[data-testid="tasks-digest"]')).toBeNull();
    expect(none.textContent).toContain("Quá hạn");
  });

  it("marks a task done and opens the arrival flights with a status link", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push(`${init.method} ${url}`);
      if (String(url).endsWith(`tasks/t3`)) {
        return new Response(JSON.stringify({ ...task(), flights: [{ flight_no: "VN 611", airport_iata: "SGN", sched_local: "2026-12-20T09:30:00", tz: "Asia/Ho_Chi_Minh", status_url: "https://www.flightaware.com/live/flight/VN611" }] }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    });
    const c = await mount(createElement(TasksBoard, { list: list(), scope: "mine", members: [], lang: "en", locale: "en-AU" }));
    const flightButton = [...c.querySelectorAll("button")].find((b) => b.textContent === "Arrival flights");
    await act(async () => flightButton?.click());
    expect(c.querySelector('[data-testid="task-flights"] a')?.getAttribute("href")).toBe("https://www.flightaware.com/live/flight/VN611");
    const done = c.querySelector('[data-testid="task-toggle"] button') as HTMLButtonElement;
    await act(async () => done.click());
    expect(calls).toContain("POST /bff/tasks/t3/status");
    expect(refresh).toHaveBeenCalled();
  });

  it("lets the owner edit automations and shows others the rules only", async () => {
    const c = await mount(createElement(AutomationsBoard, { view: overview(), templates: [], textTemplates: [], members: [], lang: "en" }));
    expect(c.querySelector('[data-testid="wf-rule"]')?.textContent).toContain("When a trip enters Booked");
    expect(c.querySelector('[data-testid="wf-rule"]')?.textContent).toContain("Pick your message template");
    expect(c.querySelector('[data-testid="wf-activate"]')).not.toBeNull();
    expect(c.querySelectorAll('[data-testid="wf-install"]').length).toBe(1);
    const agent = await mount(createElement(AutomationsBoard, { view: overview({ can_edit: false }), templates: [], textTemplates: [], members: [], lang: "en" }));
    expect(agent.querySelector('[data-testid="wf-new"]')).toBeNull();
    expect(agent.querySelector('[data-testid="wf-install"]')).toBeNull();
    expect(agent.textContent).toContain("Only the agency owner edits automations");
  });

  it("opens the editor for a new automation", async () => {
    const c = await mount(createElement(AutomationsBoard, { view: overview(), templates: [], textTemplates: [], members: [], lang: "en" }));
    const open = c.querySelector('[data-testid="wf-new"] button') as HTMLButtonElement;
    await act(async () => open.click());
    expect(document.querySelector('[data-testid="wf-editor"]')).not.toBeNull();
    expect(document.querySelectorAll('[data-testid="wf-step"]').length).toBe(1);
  });

  it("shows the trip's runs in words and checks a task list before applying it", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push(`${init.method} ${url}`);
      if (String(url).includes("workflows/runs")) {
        return new Response(JSON.stringify({ runs: [{ workflow_run_id: "r1", workflow_rule_id: "w1", rule_name: "Birthday greeting", template_key: null, trip_id: null, traveler_id: null, trigger: "special", status: "completed", step_index: 2, step_count: 2, next_at: null, stop_reason: null, log: [{ step: 0, action: "send_email", outcome: "skipped_no_consent", at: "x" }, { step: 1, action: "create_task", outcome: "created", at: "x" }], created_at: "x", finished_at: "x" }] }), { status: 200 });
      }
      if (String(url).endsWith("/apply")) return new Response(JSON.stringify({ eligible: ["t"], blocked: [], dry_run: true }), { status: 200 });
      return new Response(JSON.stringify(overview({ rules: [rule({ trigger: { kind: "manual", value: null, offset_days: 0 }, status: "active", name: "Visa list" })] })), { status: 200 });
    });
    const c = await mount(createElement(TripWorkflows, { tripId: "00000000-0000-4000-8000-0000000007e1", role: "agent", lang: "en", locale: "en-AU", timeZone: "UTC" }));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(c.querySelector('[data-testid="trip-workflow-run"]')?.textContent).toContain("Send an e-mail: not sent: no marketing consent");
    const select = c.querySelector('[data-testid="trip-apply"] select') as HTMLSelectElement;
    await act(async () => {
      select.value = rule().workflow_rule_id;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const check = [...c.querySelectorAll("button")].find((b) => b.textContent === "Check") as HTMLButtonElement;
    await act(async () => check.click());
    expect(c.textContent).toContain("Ready to apply to this trip.");
    expect(calls.some((x) => x.startsWith("POST") && x.endsWith("/apply"))).toBe(true);
    const assistant = await mount(createElement(TripWorkflows, { tripId: "x", role: "assistant", lang: "en", locale: "en-AU", timeZone: "UTC" }));
    expect(assistant.querySelector('[data-testid="trip-apply"]')).toBeNull();
  });
});
