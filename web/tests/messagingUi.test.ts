// @vitest-environment jsdom
// AA-958 (CRM-28) web, components: the trip's WhatsApp / SMS panel (card R: with the window closed only an approved
// template is offered and sent with its values; free text inside the window; a 409 is said in words; a channel that
// cannot send says why; Undo) and Settings > SMS and WhatsApp (the owner connects with what they typed; an agent sees
// the status only; the WhatsApp test sends an approved template and the webhook check waits for a reply), marketing
// templates hidden without the client's marketing yes, and the Inbox list of known clients with no open trip.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChannelsSettings } from "@/components/messaging/ChannelsSettings";
import { NoTripInbox } from "@/components/messaging/NoTripInbox";
import { TextMessagesPanel } from "@/components/messaging/TextMessagesPanel";
import { MESSAGES } from "@/lib/i18n";
import type { Conversation } from "@/lib/mail";
import { sendableTemplates, testTemplates, type ComposeState, type MessagingView } from "@/lib/messaging";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {}, replace: () => {} }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const EN = MESSAGES.en;
const TRIP = "0f0e0d0c-0b0a-4908-8706-050403020100";
const CONV = "1f0e0d0c-0b0a-4908-8706-050403020100";
const SID = "HX" + "a1".repeat(16);
const LATER = new Date(Date.now() + 5 * 3_600_000).toISOString();

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type Call = { method: string; url: string; body: unknown };

function stubFetch(answers: (call: Call) => Response): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call = { method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(call);
      return answers(call);
    }),
  );
  return calls;
}

function compose(over: Partial<ComposeState["channels"]["whatsapp"]> = {}, sms = { available: true, reason: null }): ComposeState {
  return {
    traveler_id: "t1",
    channels: { whatsapp: { available: true, reason: null, window_until: null, ...over }, sms },
    templates: [{ content_sid: SID, name: "deposit_reminder", language: "en", body: "Hi {{1}}, pay for {{2}}.", variables: ["1", "2"] }],
  };
}

const CONVERSATION: Conversation = {
  conversation_id: CONV,
  trip_id: TRIP,
  traveler_id: "t1",
  channel: "whatsapp",
  last_message_at: "2026-11-01T09:00:00Z",
  message_count: 1,
  held_count: 0,
  last_direction: "in",
  unmatched: false,
};

function thread(windowUntil: string | null) {
  return {
    conversation_id: CONV,
    trip_id: TRIP,
    traveler_id: "t1",
    channel: "whatsapp",
    window_until: windowUntil,
    messages: [
      {
        message_id: "m1",
        direction: "in",
        status: "received",
        sender_kind: null,
        author_member_id: null,
        subject: null,
        text: "Is the kayak OK for kids?",
        html: null,
        from: "+61491570110",
        to: ["+61491570156"],
        attachments: [],
        attachment_notes: [],
        failure_code: null,
        imported: false,
        match_method: "sender",
        occurred_at: "2026-11-01T09:00:00Z",
        sent_at: null,
        updated_at: "2026-11-01T09:00:00Z",
      },
      {
        message_id: "m2",
        direction: "out",
        status: "sent",
        sender_kind: "agency_mailbox",
        author_member_id: "me",
        subject: null,
        text: "Yes, from 6 years.",
        html: null,
        from: "+61491570156",
        to: ["+61491570110"],
        attachments: [],
        attachment_notes: [],
        failure_code: null,
        imported: false,
        match_method: "compose",
        occurred_at: "2026-11-01T09:05:00Z",
        sent_at: "2026-11-01T09:05:00Z",
        updated_at: "2026-11-01T09:05:00Z",
        delivery: { template: null, status: "read", error_code: null, status_at: "2026-11-01T09:06:00Z" },
      },
    ],
  };
}

async function mountPanel(conversations: Conversation[]): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(TextMessagesPanel, { tripId: TRIP, conversations, lang: "en", locale: "en-AU" }));
  });
  await act(async () => {});
  return container;
}

const q = (c: HTMLElement, id: string) => c.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;

async function typeInto(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("trip WhatsApp / SMS panel", () => {
  it("card R: with the window closed only an approved template goes, with its values", async () => {
    const calls = stubFetch((call) => {
      if (call.url.includes("messaging/compose")) return json(200, compose());
      if (call.url.includes(`conversations/${CONV}/messages`)) return json(200, thread(null));
      return json(202, { message_id: "x", conversation_id: CONV });
    });
    const c = await mountPanel([CONVERSATION]);
    expect(q(c, "texts-thread")?.textContent).toContain("Is the kayak OK for kids?");
    expect(q(c, "texts-thread")?.textContent).toContain(EN["msg.delivery.read"]);
    expect(q(c, "texts-window")?.textContent).toBe(EN["msg.windowClosed"]);
    expect(q(c, "texts-body")).toBeNull();
    const send = () => q(c, "texts-send")!.closest("button") as HTMLButtonElement;
    expect(send().disabled).toBe(true);
    const select = c.querySelector("select") as HTMLSelectElement;
    await act(async () => {
      select.value = SID;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const inputs = Array.from(c.querySelectorAll('[data-testid="texts-composer"] input')) as HTMLInputElement[];
    expect(inputs).toHaveLength(2);
    await typeInto(inputs[0], "Lan");
    expect(send().disabled).toBe(true);
    await typeInto(inputs[1], "Hoi An");
    expect(q(c, "texts-preview")?.textContent).toBe("Hi Lan, pay for Hoi An.");
    await act(async () => send().click());
    const post = calls.find((x) => x.method === "POST")!;
    expect(post.url).toBe("/bff/messaging/messages");
    expect(post.body).toMatchObject({ conversation_id: CONV, content_sid: SID, variables: { "1": "Lan", "2": "Hoi An" } });
    expect(q(c, "texts-undo")).not.toBeNull();
  });

  it("sends free text inside the window and says a refusal in words", async () => {
    let refuse = false;
    const calls = stubFetch((call) => {
      if (call.url.includes("messaging/compose")) return json(200, compose({ window_until: LATER }));
      if (call.url.includes(`conversations/${CONV}/messages`)) return json(200, thread(LATER));
      if (call.url.endsWith("/undo")) return json(200, { status: "draft_autosaved" });
      return refuse ? json(409, { detail: "template_required" }) : json(202, { message_id: "x", conversation_id: CONV });
    });
    const c = await mountPanel([CONVERSATION]);
    expect(q(c, "texts-window")?.textContent).toBe(EN["msg.window"].replace("{hours}", "5"));
    await typeInto(q(c, "texts-body") as HTMLTextAreaElement, "See you at 7");
    await act(async () => (q(c, "texts-send")!.closest("button") as HTMLButtonElement).click());
    expect(calls.find((x) => x.method === "POST")!.body).toMatchObject({ conversation_id: CONV, body: "See you at 7" });
    await act(async () => (q(c, "texts-undo")!.closest("button") as HTMLButtonElement).click());
    expect(calls.some((x) => x.url.endsWith("/undo"))).toBe(true);
    expect(q(c, "texts-notice")?.textContent).toBe(EN["msg.undone"]);
    refuse = true;
    await act(async () => (q(c, "texts-send")!.closest("button") as HTMLButtonElement).click());
    expect(q(c, "texts-notice")?.textContent).toBe(EN["msg.error.template_required"]);
  });

  it("says why a channel cannot send, and a trip without a thread writes to the trip", async () => {
    const calls = stubFetch((call) => {
      if (call.url.includes("messaging/compose")) {
        return json(200, compose({ available: false, reason: "client_opted_out" }, { available: true, reason: null }));
      }
      return json(202, { message_id: "x", conversation_id: CONV });
    });
    const c = await mountPanel([]);
    expect(q(c, "texts-empty")).not.toBeNull();
    expect(q(c, "texts-reason")?.textContent).toBe(EN["msg.reason.client_opted_out"]);
    expect(q(c, "texts-composer")).toBeNull();
    const smsTab = Array.from(c.querySelectorAll("button")).find((b) => b.textContent === EN["msg.channel.sms"])!;
    await act(async () => smsTab.click());
    expect(q(c, "texts-window")).toBeNull();
    await typeInto(q(c, "texts-body") as HTMLTextAreaElement, "Hello");
    await act(async () => (q(c, "texts-send")!.closest("button") as HTMLButtonElement).click());
    expect(calls.find((x) => x.method === "POST")!.body).toMatchObject({ trip_id: TRIP, channel: "sms", body: "Hello" });
  });
});

function view(over: Partial<MessagingView> = {}): MessagingView {
  return {
    status: "connected",
    channels: { sms: true, whatsapp: true },
    providers: ["twilio", "sandbox"],
    can_manage: true,
    connection: {
      messaging_connection_id: "c1",
      provider: "sandbox",
      account: "AC01…cdef",
      sms_sender: "Sunshine",
      whatsapp_sender: "+61491570156",
      failure_class: null,
      last_checked_at: null,
      last_ok_at: "2026-11-01T01:00:00Z",
      last_test_at: null,
      last_test_result: null,
      last_inbound_at: null,
      last_status_at: null,
      connected_at: "2026-11-01T00:00:00Z",
      hook_url: "https://go.crm.example.test/crm/public/v1/hooks/twilio/" + "ab".repeat(16),
      status_callback_url: "https://go.crm.example.test/crm/public/v1/hooks/twilio/" + "ab".repeat(16) + "/status",
    },
    templates: [
      { content_sid: SID, name: "deposit_reminder", language: "en", approval: "approved", body: "Hi {{1}}", variables: ["1"] },
    ],
    ...over,
  };
}

async function mountSettings(v: MessagingView): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => {
    const root = createRoot(container);
    roots.push(root);
    root.render(createElement(ChannelsSettings, { view: v, lang: "en", locale: "en-AU" }));
  });
  return container;
}

describe("Settings > SMS and WhatsApp", () => {
  it("the owner sees the webhook address and connects with what they typed", async () => {
    const calls = stubFetch(() => json(200, { status: "connected" }));
    const c = await mountSettings(view());
    expect((q(c, "msg-hook-url") as HTMLInputElement).value).toContain("/crm/public/v1/hooks/twilio/");
    expect(q(c, "msg-templates")?.textContent).toContain("deposit_reminder");
    const form = q(c, "msg-connect")!.querySelector("form")!;
    const inputs = Array.from(form.querySelectorAll("input")) as HTMLInputElement[];
    await typeInto(inputs[0], " AC" + "0".repeat(32) + " ");
    await typeInto(inputs[1], "f".repeat(32));
    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    const put = calls.find((x) => x.method === "PUT")!;
    expect(put.url).toBe("/bff/messaging/connection");
    expect(put.body).toMatchObject({
      provider: "sandbox",
      account_sid: "AC" + "0".repeat(32),
      api_key_sid: null,
      sms_sender: "Sunshine",
      whatsapp_sender: "+61491570156",
    });
    expect(q(c, "msg-notice")?.textContent).toBe(EN["msg.connected"]);
    await act(async () => (q(c, "msg-refresh")!.closest("button") as HTMLButtonElement).click());
    expect(calls.some((x) => x.url === "/bff/messaging/templates/refresh")).toBe(true);
  });

  it("the test button reports a failure in words", async () => {
    stubFetch(() => json(200, { result: "failed", check: "window_closed", test_message_sent: false }));
    const c = await mountSettings(view());
    await act(async () => (q(c, "msg-test-button")!.closest("button") as HTMLButtonElement).click());
    expect(q(c, "msg-notice")?.textContent).toBe(
      EN["msg.testFailed"].replace("{reason}", EN["msg.failure.window_closed"]),
    );
  });

  it("an agent sees the status only", async () => {
    const c = await mountSettings({ status: "needs_attention", channels: { sms: false, whatsapp: true }, providers: ["twilio"], can_manage: false });
    expect(q(c, "msg-status")?.textContent).toContain(EN["msg.ownerOnly"]);
    expect(q(c, "msg-connect")).toBeNull();
    expect(q(c, "msg-webhooks")).toBeNull();
    expect(q(c, "msg-channel-sms")?.textContent).toBe(EN["msg.channelOff"].replace("{channel}", "SMS"));
  });
});

describe("marketing templates", () => {
  it("are hidden from a client without a marketing yes, with a line that says why", async () => {
    const promo = "HX" + "b2".repeat(16);
    stubFetch((call) => {
      if (call.url.includes("messaging/compose")) {
        const state = compose();
        state.templates.push({
          content_sid: promo,
          name: "autumn_offer",
          language: "en",
          body: "Hello {{1}}",
          variables: ["1"],
          marketing: true,
          available: false,
          reason: "no_marketing_consent",
        });
        return json(200, state);
      }
      if (call.url.includes(`conversations/${CONV}/messages`)) return json(200, thread(null));
      return json(202, {});
    });
    const c = await mountPanel([CONVERSATION]);
    const options = Array.from(c.querySelectorAll("select option")).map((o) => (o as HTMLOptionElement).value);
    expect(options).toContain(SID);
    expect(options).not.toContain(promo);
    expect(q(c, "texts-marketing-hidden")?.textContent).toBe(EN["msg.marketingHidden"]);
  });

  it("helpers: sendable and test templates", () => {
    const base = { name: "n", language: "en", body: "x", variables: [] };
    expect(sendableTemplates([{ ...base, content_sid: "a", available: false }, { ...base, content_sid: "b" }]).map((x) => x.content_sid)).toEqual(["b"]);
    const list = testTemplates([
      { ...base, content_sid: "m", approval: "approved", category: "MARKETING" },
      { ...base, content_sid: "p", approval: "pending", category: "UTILITY" },
      { ...base, content_sid: "u", approval: "approved", category: "utility" },
    ]);
    expect(list.map((x) => x.content_sid)).toEqual(["u", "m"]);
    expect(testTemplates(undefined)).toEqual([]);
  });
});

describe("Settings > WhatsApp test", () => {
  it("sends the chosen approved template and shows the webhook check", async () => {
    const calls = stubFetch(() => json(200, { result: "ok", check: "ok", test_message_sent: true, template: SID, inbound_check: "pending" }));
    const v = view();
    v.connection!.inbound_check = "pending";
    const c = await mountSettings(v);
    expect(q(c, "msg-inbound-check")?.textContent).toBe(EN["msg.inboundCheck.pending"]);
    const selects = Array.from(q(c, "msg-test")!.querySelectorAll("select")) as HTMLSelectElement[];
    expect(selects.map((x) => x.value)).toEqual(["whatsapp", SID]); // the channel, then the approved template
    const input = q(c, "msg-test")!.querySelector("input") as HTMLInputElement;
    await typeInto(input, "+61491570110");
    await act(async () => (q(c, "msg-test-button")!.closest("button") as HTMLButtonElement).click());
    const post = calls.find((x) => x.url === "/bff/messaging/test")!;
    expect(post.body).toEqual({ channel: "whatsapp", to: "+61491570110", content_sid: SID });
    expect(q(c, "msg-notice")?.textContent).toBe(EN["msg.testSent"]);
  });

  it("without an approved template says what to do, also on a 409", async () => {
    stubFetch(() => json(409, { detail: "template_required" }));
    const c = await mountSettings(view({ templates: [] }));
    expect(q(c, "msg-test-no-template")?.textContent).toBe(EN["msg.testNoTemplate"]);
    await typeInto(q(c, "msg-test")!.querySelector("input") as HTMLInputElement, "+61491570110");
    await act(async () => (q(c, "msg-test-button")!.closest("button") as HTMLButtonElement).click());
    expect(q(c, "msg-notice")?.textContent).toBe(EN["msg.testNoTemplate"]);
  });
});

describe("Inbox: clients with no open trip", () => {
  const NO_TRIP: Conversation = { ...CONVERSATION, trip_id: null, traveler_id: "2f0e0d0c-0b0a-4908-8706-050403020100" };

  async function mountInbox(list: Conversation[]): Promise<HTMLElement> {
    const container = document.createElement("div");
    document.body.appendChild(container);
    await act(async () => {
      const root = createRoot(container);
      roots.push(root);
      root.render(createElement(NoTripInbox, { conversations: list, lang: "en", locale: "en-AU" }));
    });
    return container;
  }

  it("shows nothing when there is none", async () => {
    const c = await mountInbox([]);
    expect(q(c, "inbox-no-trip")).toBeNull();
  });

  it("opens the message and links it to one of the client's trips", async () => {
    const calls = stubFetch((call) => {
      if (call.url.includes("/messages")) return json(200, { ...thread(null), trip_id: null });
      if (call.url.includes("travelers/")) {
        return json(200, {
          traveler: { traveler_id: NO_TRIP.traveler_id, display_name: "Mai Tran" },
          trips: [{ trip_id: TRIP, title: "Lake walks", stage: "lost", role: "lead" }],
        });
      }
      return json(200, { conversation_id: CONV, traveler_id: NO_TRIP.traveler_id, trip_id: TRIP });
    });
    const c = await mountInbox([NO_TRIP]);
    expect(q(c, "inbox-no-trip")?.textContent).toContain(EN["msg.noTrip.title"]);
    await act(async () => (q(c, `no-trip-${CONV}-open`)!.closest("button") as HTMLButtonElement).click());
    expect(q(c, `no-trip-${CONV}-text`)?.textContent).toBe("Is the kayak OK for kids?");
    expect(c.textContent).toContain("Mai Tran");
    await act(async () => (q(c, `no-trip-link-${TRIP}`)!.closest("button") as HTMLButtonElement).click());
    const post = calls.find((x) => x.method === "POST")!;
    expect(post.url).toBe(`/bff/conversations/${CONV}/link`);
    expect(post.body).toEqual({ traveler_id: NO_TRIP.traveler_id, trip_id: TRIP });
    expect(c.textContent).toContain(EN["msg.noTrip.linked"]);
  });

  it("a client without any trip is told to start one", async () => {
    stubFetch((call) =>
      call.url.includes("/messages")
        ? json(200, thread(null))
        : json(200, { traveler: { traveler_id: NO_TRIP.traveler_id, display_name: "Mai Tran" }, trips: [] }),
    );
    const c = await mountInbox([NO_TRIP]);
    await act(async () => (q(c, `no-trip-${CONV}-open`)!.closest("button") as HTMLButtonElement).click());
    expect(q(c, `no-trip-${CONV}-none`)?.textContent).toBe(EN["msg.noTrip.noTrips"]);
  });
});
