// @vitest-environment jsdom
// AA-953 (card CRM-24a) — the traveller portal per person: the link cookie exchange (§2.9 rule 1), the pure helpers
// (flights in airport local time, R33; the passport scan checks), the EN + VI strings, and the page itself: the "Who
// are you?" picker posts the same code with the chosen traveller (Fix E4), the home shows the person's own flights,
// documents and the group (lead only), and never a Stage 3 value.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GET } from "@/app/c/t/[token]/route";
import { clientMessages } from "@/lib/clientMessages";
import {
  flightBody,
  flightDayKey,
  fileSize,
  PORTAL_API,
  PORTAL_LINK_COOKIE,
  portalCookie,
  scanProblem,
  toBase64,
  type PortalFlight,
} from "@/lib/clientPortal";
import { dictionary } from "@/lib/i18n";
import { clientErrorKey } from "@/lib/proposals";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TOKEN = "b".repeat(43);
const LEAD = "00000000-0000-4000-8000-0000000009a1";
const CHILD = "00000000-0000-4000-8000-0000000009a2";

function flight(over: Partial<PortalFlight> = {}): PortalFlight {
  return {
    direction: "arrival",
    flight_no: "RA401",
    airport: "KTM",
    local_time: "2026-11-30T23:55",
    local_date: "2026-11-30",
    tz: "Asia/Kathmandu",
    utc_offset: "+05:45",
    days_from_start: -1,
    ...over,
  };
}

describe("the portal link cookie (§2.9 rule 1)", () => {
  it("is host-only, httpOnly, Secure, SameSite=Strict and scoped to the traveller routes", () => {
    const c = portalCookie(TOKEN);
    expect(c.startsWith(`${PORTAL_LINK_COOKIE}=${TOKEN};`)).toBe(true);
    for (const part of ["HttpOnly", "Secure", "SameSite=Strict", `Path=${PORTAL_API}`]) expect(c).toContain(part);
    expect(c).not.toContain("Domain=");
  });

  it("the landing exchanges the token and redirects to /c/t; a bad token is 404", async () => {
    const res = await GET(new Request(`https://go.test/c/t/${TOKEN}`), { params: Promise.resolve({ token: TOKEN }) });
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toBe("/c/t");
    expect(res.headers.get("referrer-policy")).toBe("no-referrer");
    expect(res.headers.get("set-cookie")).toContain(`${PORTAL_LINK_COOKIE}=${TOKEN}`);
    const bad = await GET(new Request("https://go.test/c/t/short"), { params: Promise.resolve({ token: "short" }) });
    expect(bad.status).toBe(404);
  });
});

describe("pure helpers", () => {
  it("flights: the body, the fields to fix, and the trip day (R33)", () => {
    expect(flightBody("arrival", { flight_no: "ra 401", airport: "ktm ", local_time: "2026-11-30T23:55", tz: "Asia/Kathmandu" })).toEqual({
      body: { direction: "arrival", flight_no: "RA401", airport: "KTM", local_time: "2026-11-30T23:55", tz: "Asia/Kathmandu" },
    });
    expect(flightBody("departure", { flight_no: "?", airport: "Kathmandu", local_time: "soon", tz: "" })).toEqual({
      fields: ["flight_no", "airport", "local_time", "tz"],
    });
    expect(flightDayKey(flight())).toEqual({ key: "client.flightDay.dayBefore", n: 1 });
    expect(flightDayKey(flight({ days_from_start: -3 }))).toEqual({ key: "client.flightDay.before", n: 3 });
    expect(flightDayKey(flight({ days_from_start: 0 }))).toEqual({ key: "client.flightDay.first", n: 1 });
    expect(flightDayKey(flight({ days_from_start: 7 }))).toEqual({ key: "client.flightDay.during", n: 8 });
    expect(flightDayKey(flight({ days_from_start: null }))).toBeNull();
  });

  it("the passport scan checks, sizes and base64", () => {
    expect(scanProblem({ type: "image/jpeg", size: 1000 })).toBeNull();
    expect(scanProblem({ type: "image/gif", size: 1000 })).toBe("type");
    expect(scanProblem({ type: "application/pdf", size: 8 * 1024 * 1024 })).toBe("size");
    expect(fileSize(500)).toBe("1 KB");
    expect(fileSize(1.5 * 1024 * 1024)).toBe("1.5 MB");
    expect(toBase64(new TextEncoder().encode("Pip").buffer as ArrayBuffer)).toBe("UGlw");
  });

  it("every new client string exists in EN and VI and each refusal code has its text", () => {
    const en = dictionary("en") as Record<string, string>;
    const vi = dictionary("vi") as Record<string, string>;
    const keys = Object.keys(en).filter((k) => /^client\.(whoAreYou|flight|scan|document|reach|portal\.|invite|companion|switchTo|signedInAs|service\.|tripTitle)/.test(k));
    expect(keys.length).toBeGreaterThan(30);
    for (const k of keys) expect(vi[k] && vi[k] !== en[k], k).toBeTruthy();
    for (const code of ["not_your_traveller", "lead_only", "email_in_use", "already_invited", "ask_your_agent", "too_many_attempts", "document_write_only", "document_not_yours", "invalid_flight", "body_too_large"]) {
      expect(clientErrorKey(code)).toBe(`client.error.${code}`);
      expect(en[`client.error.${code}`]).toBeTruthy();
    }
    expect(Object.keys(clientMessages("en")).every((k) => k.startsWith("client.") || k.startsWith("common."))).toBe(true);
  });
});

describe("the portal page", () => {
  let root: Root | null = null;
  afterEach(() => {
    if (root) act(() => root!.unmount());
    root = null;
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  async function mount(): Promise<HTMLElement> {
    const { PortalPage } = await import("@/components/portal/PortalPage");
    const el = document.createElement("div");
    document.body.appendChild(el);
    root = createRoot(el);
    await act(async () => {
      root!.render(createElement(PortalPage, { base: PORTAL_API, dict: clientMessages("en"), locale: "en-AU" }));
    });
    return el;
  }

  function reply(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  const HOME = {
    agency_name: "Sen Travel",
    signed_in: true,
    role: "lead",
    trip: { title: "Nordic lakes", start_date: "2026-12-01", end_date: "2026-12-08", stage: "booked" },
    stage3: { locked: false, fields: ["passport_no"], missing: [], done: true, submitted_at: null },
    waiver: null,
    agreements: null,
    me: { traveler_id: LEAD, name: "Mira Castell", role: "lead" },
    switch_to: [{ traveler_id: CHILD, name: "Pip Castell", role: "companion" }],
    flights: [flight()],
    documents: [
      { document_id: "d1", kind: "voucher", filename: "mira-voucher.pdf", content_type: "application/pdf", size_bytes: 2048, for: "me", created_at: "2026-10-30T10:00:00+00:00" },
    ],
    services: [{ title: "Ferry pass", from: "2026-12-02", to: null, status: "planning", confirm_class: null, pay_on_arrival: true }],
    itinerary: [],
    passport_scan_on_file: true,
    party: [
      { traveler_id: LEAD, name: "Mira Castell", role: "lead", details_done: true, waiver_signed: null, me: true, has_email: true, via_you: false, portal: "active" },
      { traveler_id: CHILD, name: "Pip Castell", role: "companion", details_done: false, waiver_signed: null, me: false, has_email: false, via_you: true, portal: "invited" },
    ],
    payments: [],
  };

  it("Fix E4: a code that opens two travellers shows the picker, and the pick posts the same code with the choice", async () => {
    const calls: [string, RequestInit][] = [];
    let signedIn = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push([url, init]);
        if (init.method === "GET") return reply(200, signedIn ? HOME : { agency_name: "Sen Travel", signed_in: false });
        const body = JSON.parse(String(init.body ?? "{}")) as Record<string, string>;
        if (url.endsWith("/session") && !body.traveler_id) {
          return reply(200, { signed_in: false, pick: [{ traveler_id: LEAD, name: "Mira Castell", role: "lead", link: true }, { traveler_id: CHILD, name: "Pip Castell", role: "companion", link: false }] });
        }
        if (url.endsWith("/session")) {
          signedIn = true;
          return reply(200, { signed_in: true });
        }
        return reply(202, { sent: true, to_hint: "m***@e******.test" });
      }),
    );
    const el = await mount();
    const input = el.querySelector<HTMLInputElement>('[data-testid="portal-code"]')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "123456");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="portal-verify"]')!.click());
    expect(el.querySelector('[data-testid="portal-picker"]')?.textContent).toContain("Pip Castell");
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="portal-pick-companion"]')!.click());
    const picked = calls.find(([u, i]) => u === `${PORTAL_API}/session` && String(i.body).includes(CHILD));
    expect(picked && JSON.parse(String(picked[1].body))).toEqual({ code: "123456", traveler_id: CHILD });
    expect(picked![1].credentials).toBe("same-origin");
    expect(el.querySelector('[data-testid="portal-home"]')).not.toBeNull();
  });

  it("the home: the person, the switch, flights in local time, documents, the group and no Stage 3 value", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(200, HOME)));
    const el = await mount();
    expect(el.querySelector('[data-testid="portal-me"]')?.textContent).toContain("Signed in as Mira Castell");
    expect(el.querySelector('[data-testid="portal-switch-companion"]')?.textContent).toContain("Switch to Pip Castell");
    const saved = el.querySelector('[data-testid="portal-flight-arrival-saved"]')?.textContent ?? "";
    expect(saved).toContain("RA401");
    expect(saved).toContain("23:55 (UTC+05:45)");
    expect(saved).toContain("the day before your trip starts");
    expect(el.querySelector('[data-testid="portal-documents"]')?.textContent).toContain("mira-voucher.pdf");
    expect(el.querySelector('[data-testid="portal-scan-on-file"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="portal-services"]')?.textContent).toContain("paid on arrival");
    const child = el.querySelectorAll('[data-testid="portal-party-member"]')[1];
    expect(child.getAttribute("data-reach")).toBe("via_you");
    expect(child.textContent).toContain("through your e-mail");
    expect(el.querySelector<HTMLInputElement>('[data-testid="portal-field-passport_no"]')!.value).toBe("");
  });

  it("a companion given their own address: the link goes to them, the lead sees no link", async () => {
    const posted: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        if (init.method === "GET") return reply(200, HOME);
        posted.push(JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>);
        return reply(200, { traveler_id: CHILD, reach: "own", link_path: null, expires_at: "2026-12-01T00:00:00+00:00" });
      }),
    );
    const el = await mount();
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="portal-invite-open-1"]')!.click());
    const input = el.querySelector<HTMLInputElement>('[data-testid="portal-invite-email"]')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "pip.castell@example.test");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="portal-invite-send"]')!.click());
    expect(posted.at(-1)).toEqual({ traveler_id: CHILD, email: "pip.castell@example.test" });
    const note = el.querySelector('[data-testid="portal-note"]')?.textContent ?? "";
    expect(note).toContain("Pip Castell's link goes to their own e-mail address");
    expect(note).not.toContain("/c/t/");
  });
});
