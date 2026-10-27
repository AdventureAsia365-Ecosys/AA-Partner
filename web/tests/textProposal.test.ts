// @vitest-environment jsdom
// AA-951 (card CRM-22): the text proposal helpers (lib/textProposal.ts), the BFF lines of card AA-951, the builder's
// Before you go panel (card E: no AA item -> no pack, the reminders still shown; picks saved with the page's seq) and
// the A-05 page (card R: a 409 price_hash_mismatch says so and loads the fresh text; WhatsApp hands over the text).
import { act, createElement } from "react";
import { hydrateRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  canRenderText,
  linkLostOnUndo,
  linkNoteKey,
  sendBody,
  textErrorKey,
  togglePack,
  whatsappShareUrl,
  withSuggestions,
  type BeforeYouGo,
  type TextPreview,
  type TextSent,
} from "@/lib/textProposal";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {}, push: () => {} }) }));
const { BeforeYouGoPanel } = await import("@/components/proposals/BeforeYouGoPanel");
const { TextProposalPage } = await import("@/components/proposals/TextProposalPage");

const P = "00000000-0000-4000-8000-0000000000d1";
const X = "00000000-0000-4000-8000-0000000000d2";
const HASH = "ab12cd34ef56".padEnd(64, "0");

const roots: Root[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) act(() => r.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function mount(el: ReturnType<typeof createElement>): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  container.innerHTML = renderToString(el);
  document.body.appendChild(container);
  await act(async () => {
    roots.push(hydrateRoot(container, el));
  });
  return container;
}

function byg(over: Partial<BeforeYouGo> = {}): BeforeYouGo {
  return {
    aa_items: 1,
    countries: ["NP"],
    fitness_warning: true,
    insurance_reminder: true,
    selected: [],
    suggested: [{ group_key: "prep-pack-nepal", title: "Before you go: Nepal", countries: ["NP"], locales: ["en", "vi"], selected: false }],
    packs_not_chosen: true,
    ...over,
  };
}

function preview(over: Partial<TextPreview> = {}): TextPreview {
  return {
    proposal_id: P,
    trip_id: X,
    version: 2,
    kind: "builder",
    status: "published",
    format: "email",
    locale: "en",
    subject: "Your trip proposal: Nepal (version 2)",
    body: "Your trip proposal: Nepal\n\nProposal reference: ab12cd34ef56\n",
    price_hash: HASH,
    reference: "ab12cd34ef56",
    unfilled: [],
    link: "added_on_send",
    ...over,
  };
}

describe("helpers", () => {
  it("which versions render as text", () => {
    expect(canRenderText("builder", "published")).toBe(true);
    expect(canRenderText("builder", "draft")).toBe(false);
    expect(canRenderText("text", "draft")).toBe(true);
    expect(canRenderText("text", "superseded")).toBe(false);
  });

  it("pack picks keep their order; the suggestions are added after the picks", () => {
    expect(togglePack(["a", "b"], "c", true)).toEqual(["a", "b", "c"]);
    expect(togglePack(["a", "b"], "a", false)).toEqual(["b"]);
    expect(togglePack(["a"], "a", true)).toEqual(["a"]);
    const data = byg({ selected: [{ group_key: "x", title: "X", content_version: 1, locales: ["en"], suggested: false }] });
    expect(withSuggestions(data)).toEqual(["x", "prep-pack-nepal"]);
  });

  it("WhatsApp share link and error keys", () => {
    expect(whatsappShareUrl("Hi & bye")).toBe("https://wa.me/?text=Hi%20%26%20bye");
    expect(textErrorKey("price_hash_mismatch")).toBe("text.err.price_hash_mismatch");
    expect(textErrorKey("whatever")).toBe("text.err.generic");
    expect(textErrorKey(undefined)).toBe("text.err.generic");
  });

  it("an undone e-mail that carried the only link asks the next send for a new link", () => {
    const sent = (over: Partial<TextSent>): TextSent => ({
      proposal_id: P, status: "sent", format: "email", locale: "en", price_hash: HASH, reference: "ab12cd34ef56",
      link_path: "/c/p/t", link_in_text: true, version_sent: true, link_reissued: false, superseded: null,
      subject: "S", body: "B", ...over,
    });
    expect(linkLostOnUndo(sent({}))).toBe(true);
    expect(linkLostOnUndo(sent({ link_in_text: false, link_path: null, version_sent: false }))).toBe(false);
    expect(linkLostOnUndo(sent({ format: "whatsapp" }))).toBe(false);
    expect(linkNoteKey(preview(), false)).toBe("text.linkOnSend");
    expect(linkNoteKey(preview({ link: "client_has_it" }), false)).toBe("text.linkClientHas");
    expect(linkNoteKey(preview({ link: "client_has_it" }), true)).toBe("text.linkRelink");
    const base = { format: "email" as const, locale: "en" as const, priceHash: HASH, templateId: "", messageId: X };
    expect(sendBody({ ...base, relink: false })).toEqual({ format: "email", locale: "en", price_hash: HASH, message_id: X });
    expect(sendBody({ ...base, format: "whatsapp", templateId: P, relink: true })).toEqual({
      format: "whatsapp", locale: "en", price_hash: HASH, template_id: P, new_link: true,
    });
  });

  it("every new string exists in both languages", () => {
    for (const key of Object.keys(MESSAGES.en).filter((k) => /^(text|byg|templates)\./.test(k))) {
      expect(MESSAGES.vi[key as keyof typeof MESSAGES.vi], key).toBeTruthy();
    }
  });
});

describe("BFF lines of card AA-951", () => {
  it("allows the text, Before you go and template routes", () => {
    const allowed: [string, string][] = [
      ["GET", `proposals/${P}/text`],
      ["POST", `proposals/${P}/text/send`],
      ["GET", `proposals/${P}/before-you-go`],
      ["PUT", `proposals/${P}/before-you-go`],
      ["GET", "message-templates"],
      ["POST", "message-templates"],
      ["POST", "message-templates/import-aa"],
      ["PATCH", `message-templates/${P}`],
      ["DELETE", `message-templates/${P}`],
    ];
    for (const [method, path] of allowed) expect(matchBff(method, path)?.card, `${method} ${path}`).toBe("AA-951");
    for (const [method, path] of [
      ["DELETE", `proposals/${P}/text`],
      ["POST", `proposals/${P}/before-you-go`],
      ["PUT", "message-templates"],
      ["POST", "message-templates/import"],
      ["GET", `proposals/x/text`],
    ] as [string, string][]) {
      expect(matchBff(method, path), `${method} ${path}`).toBeNull();
    }
  });
});

describe("Before you go panel", () => {
  const panel = (data: BeforeYouGo, onSave = vi.fn(async () => true), draft = true) =>
    createElement(BeforeYouGoPanel, { data, draft, busy: false, lang: "en", onSave });

  it("card E: no AA item -> no pack offered, the reminders stay", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(panel(byg({ aa_items: 0, suggested: [], packs_not_chosen: false, fitness_warning: false })));
    expect(container.querySelector("[data-testid=byg-no-aa]")).not.toBeNull();
    expect(container.querySelectorAll("[data-testid=byg-pack]")).toHaveLength(0);
    expect(container.querySelector("[data-testid=byg-reminders]")!.textContent).toMatch(/official government link/);
    expect(container.querySelector("[data-testid=byg-fitness]")).toBeNull();
  });

  it("a published version shows its packs read only", () => {
    const data = byg({ selected: [{ group_key: "prep-pack-nepal", title: "Before you go: Nepal", content_version: 2, locales: ["en"], suggested: true }], packs_not_chosen: false });
    const container = document.createElement("div");
    container.innerHTML = renderToString(panel(data, vi.fn(async () => true), false));
    expect(container.querySelector<HTMLFieldSetElement>("fieldset")!.disabled).toBe(true);
    expect(container.querySelector("[data-testid=byg-save]")).toBeNull();
  });

  it("hydrated: tick a suggested pack, then save sends the picks", async () => {
    const onSave = vi.fn(async () => true);
    const container = await mount(panel(byg(), onSave));
    const box = container.querySelector<HTMLInputElement>("[data-testid=byg-pack] input")!;
    const save = () => container.querySelector<HTMLButtonElement>("[data-testid=byg-save] button")!;
    expect(save().disabled).toBe(true); // nothing changed yet
    await act(async () => {
      box.click();
    });
    expect(save().disabled).toBe(false);
    await act(async () => {
      save().click();
    });
    expect(onSave).toHaveBeenCalledWith(["prep-pack-nepal"]);
    expect(container.querySelector("[data-testid=byg-fitness]")).not.toBeNull();
  });
});

describe("Text proposal page (A-05)", () => {
  const page = (role = "agent") => createElement(TextProposalPage, { initial: preview(), templates: [], role, lang: "en" });

  it("the assistant previews but has no send button", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(page("assistant"));
    expect(container.querySelector("[data-testid=text-body]")!.textContent).toMatch(/ab12cd34ef56/);
    expect(container.querySelector("[data-testid=text-send]")).toBeNull();
    expect(container.querySelector("[data-testid=text-reference]")!.textContent).toBe("Price reference ab12cd34ef56");
  });

  it("card R: a stale hash says so and loads the fresh text; the next send uses the new hash", async () => {
    const fresh = preview({ price_hash: "f".repeat(64), reference: "ffffffffffff", body: "New text\n" });
    const calls: [string, RequestInit][] = [];
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      if (init.method === "POST" && calls.filter(([, i]) => i.method === "POST").length === 1) {
        return new Response(JSON.stringify({ detail: "price_hash_mismatch", current_price_hash: "f".repeat(64) }), { status: 409 });
      }
      if (init.method === "GET") {
        // The first read is the format switch (same version, same hash); the one after the 409 is the fresh text.
        const gets = calls.filter(([, i]) => i.method === "GET").length;
        return new Response(JSON.stringify(gets === 1 ? preview({ format: "whatsapp" }) : fresh), { status: 200 });
      }
      return new Response(
        JSON.stringify({ proposal_id: P, status: "sent", format: "whatsapp", locale: "en", price_hash: fresh.price_hash, reference: fresh.reference, link_path: "/c/p/t", link_in_text: true, version_sent: true, superseded: null, subject: null, body: "Final text https://go/c/p/t\n" }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const container = await mount(page());
    await act(async () => {
      container.querySelector<HTMLInputElement>("input[value=whatsapp]")!.click();
    });
    const send = () => container.querySelector<HTMLButtonElement>("[data-testid=text-send] button")!;
    await act(async () => {
      send().click();
    });
    const firstPost = calls.find(([, i]) => i.method === "POST")!;
    expect(firstPost[0]).toBe(`/bff/proposals/${P}/text/send`);
    expect(JSON.parse(String(firstPost[1].body))).toEqual({ format: "whatsapp", locale: "en", price_hash: HASH });
    expect(container.querySelector("[data-testid=text-message]")!.textContent).toMatch(/changed since this text was shown/);
    expect(container.querySelector("[data-testid=text-body]")!.textContent).toBe("New text\n");
    await act(async () => {
      send().click();
    });
    const posts = calls.filter(([, i]) => i.method === "POST");
    expect(JSON.parse(String(posts[1][1].body)).price_hash).toBe("f".repeat(64));
    expect(container.querySelector("[data-testid=text-final]")!.textContent).toMatch(/Final text/);
  });

  it("undoing the e-mail that carried the only link: the page says so and the next send re-issues the link", async () => {
    const sentVersion = preview({ status: "sent", link: "client_has_it" });
    const calls: [string, RequestInit][] = [];
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      if (init.method === "GET") return new Response(JSON.stringify(sentVersion), { status: 200 });
      if (url.endsWith("/undo")) return new Response(JSON.stringify({ status: "draft" }), { status: 200 });
      const first = calls.filter(([u, i]) => i.method === "POST" && u.endsWith("/text/send")).length === 1;
      return new Response(
        JSON.stringify({
          proposal_id: P, status: "sent", format: "email", locale: "en", price_hash: HASH, reference: "ab12cd34ef56",
          link_path: first ? "/c/p/t" : "/c/p/new", link_in_text: true, version_sent: first, link_reissued: !first,
          superseded: null, subject: "S", body: "Text\n", message: { message_id: X, status: "queued" },
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const container = await mount(page());
    const send = () => container.querySelector<HTMLButtonElement>("[data-testid=text-send] button")!;
    await act(async () => {
      send().click();
    });
    expect(JSON.parse(String(calls[0][1].body)).new_link).toBeUndefined();
    // The preview is read again after the send: the version is sent now.
    expect(container.textContent).toMatch(/client has its link/);
    const undo = [...container.querySelectorAll<HTMLButtonElement>("[data-testid=text-sent] button")].find((b) => b.textContent === "Undo")!;
    await act(async () => {
      undo.click();
    });
    expect(container.querySelector("[data-testid=text-message]")!.textContent).toMatch(/link was only in that e-mail/);
    expect(container.textContent).toMatch(/has not received the link yet/);
    await act(async () => {
      send().click();
    });
    const sends = calls.filter(([u, i]) => i.method === "POST" && u.endsWith("/text/send"));
    expect(sends).toHaveLength(2);
    expect(JSON.parse(String(sends[1][1].body)).new_link).toBe(true);
    expect(container.textContent).not.toMatch(/has not received the link yet/);
  });
});
