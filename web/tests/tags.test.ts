// @vitest-environment jsdom
// AA-960 (card CRM-29b) web: the BFF allow-list of the tag routes; how a system tag and its reasons read in both
// languages; the bulk preview sentence (card E); who manages / bulk-tags / sees Marketing; the client tag rule of the
// automations editor; and the components: the tag bar of a record (agency tags removable, system tags with reasons,
// "archived: no automation started"), the bulk dialog (Preview, then Apply with the previewed count), Settings > Tags
// (owner edits, others read) and the coverage panel (Preview, then Run). Invented names only.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  bulkSummary,
  canBulkTag,
  canManageTags,
  consentKey,
  reasonText,
  seesMarketing,
  tagErrorKey,
  tagLabel,
  type BulkResult,
  type Coverage,
  type TagVocabulary,
} from "@/lib/tags";
import { actionsFor, isClientRule, isTagRule } from "@/lib/workflows";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }) }));
const { TagBar } = await import("@/components/tags/TagBar");
const { BulkTagButton } = await import("@/components/tags/BulkTagButton");
const { TagsSettings } = await import("@/components/tags/TagsSettings");
const { CoveragePanel } = await import("@/components/tags/CoveragePanel");
const { payloadOf } = await import("@/components/workflows/RuleEditor");

const ID = "00000000-0000-4000-8000-0000000000a5";
const TAG = "00000000-0000-4000-8000-00000000a960";
const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function mount(element: ReturnType<typeof createElement>): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(element);
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  return container;
}

function vocab(over: Partial<TagVocabulary> = {}): TagVocabulary {
  return {
    tags: [
      { tag_id: TAG, kind: "agency", applies_to: "client", code: "repeat_client", name: "Repeat client", tone: "purple", archived: false, uses: 3 },
      { tag_id: "t2", kind: "system", applies_to: "client", code: "sys_ready_to_travel", name: "Ready to travel again", tone: "info", archived: false, uses: 1 },
    ],
    tones: ["neutral", "accent", "success", "warning", "danger", "info", "purple"],
    can_manage: true,
    can_bulk: true,
    ...over,
  };
}

describe("tag helpers", () => {
  it("lists every tag route in the BFF allow-list, nothing more", () => {
    for (const [m, p] of [
      ["GET", "tags"],
      ["POST", "tags"],
      ["PATCH", `tags/${ID}`],
      ["DELETE", `tags/${ID}`],
      ["POST", `tags/${ID}/bulk`],
      ["GET", `trips/${ID}/tags`],
      ["GET", `travelers/${ID}/tags`],
      ["PUT", `trips/${ID}/tags/${ID}`],
      ["DELETE", `travelers/${ID}/tags/${ID}`],
      ["GET", "workflows/coverage"],
      ["GET", "marketing/ready-to-travel"],
    ] as const) expect(matchBff(m, p), `${m} ${p}`).not.toBeNull();
    expect(matchBff("PUT", `tags/${ID}`)).toBeNull();
    expect(matchBff("POST", `trips/${ID}/tags`)).toBeNull();
    expect(matchBff("GET", `tags/${ID}`)).toBeNull();
  });

  it("words system tags and their reasons in both languages, agency tags by their own name", () => {
    const sys = { code: "sys_aa_trekking", name: "Trekking", kind: "system" };
    expect(tagLabel("en", sys)).toBe("Trekking");
    expect(tagLabel("vi", sys)).toBe("Đi bộ đường dài");
    expect(tagLabel("en", { code: "sys_unknown_new", name: "New one", kind: "system" })).toBe("New one");
    expect(tagLabel("vi", { code: "vip", name: "VIP", kind: "agency" })).toBe("VIP");
    expect(reasonText("en", { code: "returned_months_ago", months: 14 })).toBe("Came home 14 months ago");
    expect(reasonText("vi", { code: "new_aa_tours", count: 3, country: "QX" })).toBe("3 tour AA ở QX khách chưa đi");
    expect(reasonText("en", { code: "something_new" })).toBe("something_new");
    for (const code of ["sys_aa_day_tour", "sys_aa_multi_day", "sys_aa_easy", "sys_aa_challenging", "sys_ready_to_travel"]) {
      expect(MESSAGES.en[`tag.sys.${code}` as keyof typeof MESSAGES.en]).toBeTruthy();
      expect(MESSAGES.vi[`tag.sys.${code}` as keyof typeof MESSAGES.vi]).toBeTruthy();
    }
  });

  it("says the bulk preview count first, then what is skipped or suppressed (card E)", () => {
    const r: BulkResult = { tag_id: TAG, action: "add", applies_to: "client", total: 6, count: 4, already: 1, held: 0, archived_suppressed: 2, sample: [], dry_run: true };
    expect(bulkSummary("en", r, "Repeat client")).toEqual([
      "4 clients will get the tag Repeat client.",
      "1 already have it.",
      "2 archived clients get the tag, but no automation starts for them.",
    ]);
    expect(bulkSummary("en", { ...r, action: "remove", applies_to: "trip", already: 0, archived_suppressed: 0, held: 1 }, "VIP")).toEqual([
      "4 trips will lose the tag VIP.",
      "1 are on legal hold and keep their tags.",
    ]);
  });

  it("knows who may do what, and the refusal words", () => {
    expect([canManageTags("owner"), canManageTags("agent")]).toEqual([true, false]);
    expect([canBulkTag("agent"), canBulkTag("assistant")]).toEqual([true, false]);
    expect([seesMarketing("owner"), seesMarketing("assistant")]).toEqual([true, false]);
    expect(tagErrorKey("count_changed")).toBe("tag.err.count_changed");
    expect(tagErrorKey("anything_else")).toBe("tag.err.generic");
    expect(consentKey("unknown")).toBe("tag.ready.consent.unknown");
  });

  it("treats a client tag rule as a rule about the client", () => {
    const clientTag = { kind: "event" as const, value: "client_tag.added" };
    expect(isClientRule(clientTag)).toBe(true);
    expect(isTagRule(clientTag) && isTagRule({ kind: "event", value: "trip_tag.added" })).toBe(true);
    expect(isClientRule({ kind: "event", value: "trip_tag.added" })).toBe(false);
    expect(actionsFor(clientTag)).toEqual(["create_task", "send_email", "send_text", "wait"]);
    const body = payloadOf({ name: " Welcome back ", trigger: { ...clientTag, offset_days: 0 },
      conditions: { tags_any: ["sys_ready_to_travel"], stages: ["booked"] }, steps: [{ action: "create_task", kind: "custom", title: "Call" }] });
    expect(body.conditions).toEqual({ tags_any: ["sys_ready_to_travel"] });
    const trip = payloadOf({ name: "VIP", trigger: { kind: "event", value: "trip_tag.added", offset_days: 0 },
      conditions: { tags_any: ["vip"], stages: [] }, steps: [{ action: "create_task", kind: "custom", title: "Call" }] });
    expect(trip.conditions).toEqual({ tags_any: ["vip"] });
  });
});

describe("tag components", () => {
  it("shows a client's tags: agency tags can be removed, system tags explain themselves", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push(`${init.method} ${url}`);
      if (String(url).endsWith("/tags") && init.method === "GET") {
        return new Response(JSON.stringify({ tags: [
          { tag_link_id: "l1", tag_id: TAG, kind: "agency", code: "repeat_client", name: "Repeat client", tone: "purple", archived: false, source: "member", reasons: [], since: null },
          { tag_link_id: "l2", tag_id: "t2", kind: "system", code: "sys_ready_to_travel", name: "Ready to travel again", tone: "info", archived: false, source: "system", reasons: [{ code: "returned_months_ago", months: 14 }], since: null },
        ] }), { status: 200 });
      }
      if (String(url).includes("tags?applies_to=client")) return new Response(JSON.stringify(vocab()), { status: 200 });
      if (init.method === "PUT") return new Response(JSON.stringify({ added: true, workflows_started: 0, workflows_suppressed: true }), { status: 200 });
      return new Response("{}", { status: 200 });
    });
    const c = await mount(createElement(TagBar, { subject: "client", id: ID, lang: "en" }));
    expect(c.querySelector('[data-testid="tag-chip"]')?.textContent).toContain("Repeat client");
    expect(c.querySelector('[data-testid="system-tag"]')?.textContent).toContain("Came home 14 months ago");
    // The system tag has no remove button; the agency tag does.
    expect(c.querySelectorAll('[data-testid="system-tag"] button').length).toBe(0);
    const remove = c.querySelector('[data-testid="tag-chip"] button') as HTMLButtonElement;
    await act(async () => remove.click());
    expect(calls).toContain(`DELETE /bff/travelers/${ID}/tags/${TAG}`);
  });

  it("previews a bulk apply and applies it with the previewed count", async () => {
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      if (String(url).includes("tags?applies_to")) return new Response(JSON.stringify(vocab()), { status: 200 });
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      bodies.push(body);
      return new Response(JSON.stringify({ tag_id: TAG, action: "add", applies_to: "client", total: 5, count: 3, already: 2, held: 0, archived_suppressed: 0, sample: [], dry_run: body.dry_run }), { status: 200 });
    });
    const c = await mount(createElement(BulkTagButton, { subject: "client", lang: "en", scope: "Active", selection: { filter: { archived: false } } }));
    await act(async () => (c.querySelector("button") as HTMLButtonElement).click());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    const dialog = document.querySelector('[data-testid="bulk-tag-dialog"]') as HTMLElement;
    const select = dialog.querySelector("select") as HTMLSelectElement;
    await act(async () => {
      select.value = TAG;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const button = (label: string) => [...dialog.querySelectorAll("button")].find((b) => b.textContent?.startsWith(label)) as HTMLButtonElement;
    await act(async () => button("Preview").click());
    expect(dialog.querySelector('[data-testid="bulk-tag-preview"]')?.textContent).toContain("3 clients will get the tag Repeat client.");
    await act(async () => button("Apply to 3").click());
    expect(bodies.map((b) => [b.dry_run, b.expected])).toEqual([[true, null], [false, 3]]);
    expect(bodies[0].filter).toEqual({ archived: false });
    expect(dialog.textContent).toContain("Done: 3 changed.");
  });

  it("lets the owner rename, archive and delete a tag; others read", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push(`${init.method} ${url}`);
      return new Response(JSON.stringify(init.method === "GET" ? vocab() : {}), { status: 200 });
    });
    const c = await mount(createElement(TagsSettings, { initial: vocab(), lang: "en" }));
    const row = c.querySelector('[data-testid="tag-row"][data-code="repeat_client"]') as HTMLElement;
    expect(row.textContent).toContain("On 3 records");
    const archive = [...row.querySelectorAll("button")].find((b) => b.textContent === "Archive") as HTMLButtonElement;
    await act(async () => archive.click());
    expect(calls).toContain(`PATCH /bff/tags/${TAG}`);
    // A system tag is read only.
    expect(c.querySelector('[data-testid="tag-row"][data-code="sys_ready_to_travel"] button')).toBeNull();
    const reader = await mount(createElement(TagsSettings, { initial: vocab({ can_manage: false }), lang: "en" }));
    expect(reader.querySelector('[data-testid="tag-row"] button')).toBeNull();
  });

  it("lists trips missing an automation, previews and runs it", async () => {
    const coverage: Coverage = { rules: [{ workflow_rule_id: "w1", name: "Booked checklist", template_key: null, trigger: { kind: "stage_enter", value: "booked" }, stage: "booked", missing_count: 2, trip_ids: ["a", "b"] }], by_trip: { a: ["w1"], b: ["w1"] } };
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      if (init.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        bodies.push(body);
        return new Response(JSON.stringify(body.dry_run ? { eligible: ["a"], blocked: [{ trip_id: "b", reason: "stage" }] } : { eligible: ["a"], started: 1 }), { status: 200 });
      }
      return new Response(JSON.stringify(coverage), { status: 200 });
    });
    const c = await mount(createElement(CoveragePanel, { role: "agent", lang: "en", initial: coverage }));
    expect(c.querySelector('[data-testid="coverage-rule"]')?.textContent).toContain("2 trips missing Booked checklist");
    const button = (label: string) => [...c.querySelectorAll("button")].find((b) => b.textContent?.startsWith(label)) as HTMLButtonElement;
    await act(async () => button("Preview").click());
    expect(c.textContent).toContain("1 trips can start it now.");
    await act(async () => button("Run for 1").click());
    expect(bodies.map((b) => [b.dry_run, b.trip_ids])).toEqual([[true, ["a", "b"]], [false, ["a"]]]);
    const assistant = await mount(createElement(CoveragePanel, { role: "assistant", lang: "en", initial: coverage, stage: "proposal" }));
    expect(assistant.textContent).toContain("Every trip has its automations.");
  });
});
