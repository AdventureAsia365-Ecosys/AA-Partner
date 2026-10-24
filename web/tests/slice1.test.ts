// AA-936 (CRM-08 Slice 1): board helpers, the inquiry form helpers and CSP, the BFF allow-list lines, EN/VI keys.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES, fmt } from "@/lib/i18n";
import {
  clearFormMemo,
  embedSnippet,
  formKey,
  frameAncestors,
  hostedUrl,
  loadForm,
  MISS_LIMIT_PER_MINUTE,
  parseAges,
  viewerKey,
  type PublicForm,
} from "@/lib/intakeForms";
import {
  boardHref,
  canMove,
  codeLabel,
  dueIn,
  memberLabel,
  parseFilter,
  partyLabel,
  partySummary,
  shortName,
  stageLabel,
  waitingLabel,
  whenLabel,
} from "@/lib/pipeline";
import { proxy } from "@/proxy";
import { testConfig } from "./helpers";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const KEY = "AbCdEfGhIjKlMnOpQrStUv";

const FORM: PublicForm = {
  schema_key: "stage1.v1",
  version: 1,
  fields: [{ name: "destinations", type: "multi_select", required: true, options: ["vietnam", "laos"] }],
  honeypot_field: "website",
  agency_name: "Lữ hành Sen Vàng",
  default_locale: "vi-VN",
  allowed_origins: ["https://www.agency.example", "javascript:alert(1)"],
  privacy_notice_url: null,
};

describe("board helpers", () => {
  it("opens only the Slice 1 moves", () => {
    expect(canMove("inquiry", "qualifying")).toBe(true);
    expect(canMove("qualifying", "inquiry")).toBe(true);
    expect(canMove("lost", "inquiry")).toBe(true);
    expect(canMove("inquiry", "proposal")).toBe(false);
    expect(canMove("booked", "lost")).toBe(false);
  });

  it("shortens names for cards and counts companions", () => {
    expect(shortName("Anna Mai")).toBe("Anna M.");
    expect(shortName("Nguyễn Thị Hoa")).toBe("Nguyễn Thị H.");
    expect(shortName("Cher")).toBe("Cher");
    expect(shortName(null)).toBe("—");
    expect(partyLabel({ lead_name: "Tom Kent", companions: 2 })).toBe("Tom K. +2");
  });

  it("spells the month (R32) and the due time", () => {
    expect(whenLabel({ month: "2027-03-01", start_date: null }, "en-AU")).toMatch(/Mar.*2027/);
    expect(whenLabel({ month: "2027-03-01", start_date: null }, "vi-VN")).toMatch(/3.*2027/);
    expect(whenLabel({ month: null, start_date: null }, "en-AU")).toBeNull();
    expect(whenLabel({ month: null, start_date: "2027-04-03" }, "en-US")).toMatch(/Apr 3/);
    const now = Date.parse("2026-10-24T08:00:00Z");
    expect(dueIn("2026-10-24T10:00:00Z", now)).toEqual({ label: "2 h", overdue: false });
    expect(dueIn("2026-10-24T08:40:00Z", now)).toEqual({ label: "40 min", overdue: false });
    expect(dueIn("2026-10-27T08:00:00Z", now)).toEqual({ label: "3 d", overdue: false });
    expect(dueIn("2026-10-24T07:00:00Z", now)?.overdue).toBe(true);
    expect(dueIn(null, now)).toBeNull();
    expect(dueIn("nope", now)).toBeNull();
  });

  it("labels codes in both languages", () => {
    expect(waitingLabel("en", "agency")).toBe("Me");
    expect(waitingLabel("vi", "client")).toBe("Khách");
    expect(stageLabel("vi", "inquiry")).toBe("Yêu cầu mới");
    expect(codeLabel("en", "budget", "2000_3500")).toBe("USD 2–3.5k");
    expect(codeLabel("en", "budget", null)).toBe("—");
    expect(partySummary("en", 2, [7, 12])).toBe("2 adults, children aged 7, 12");
    expect(partySummary("vi", 1, [])).toBe("1 người lớn");
    expect(memberLabel("en", { member_id: "abcd-1", role: "agent", status: "active", is_me: false })).toBe("Agent · abcd");
    expect(memberLabel("vi", { member_id: "abcd-1", role: "owner", status: "active", is_me: true })).toBe("Tôi");
  });

  it("filters are links and are parsed strictly", () => {
    expect(boardHref({})).toBe("/pipeline");
    expect(boardHref({ waiting_on: "client", owner: "me" })).toBe("/pipeline?waiting_on=client&owner=me");
    expect(parseFilter({ waiting_on: "client", owner: "me" })).toEqual({ waiting_on: "client", owner: "me" });
    expect(parseFilter({ waiting_on: "nobody", owner: ["me"] })).toEqual({ waiting_on: null, owner: "anyone" });
  });
});

describe("BFF allow-list (AA-936 lines)", () => {
  it("forwards the slice routes and nothing near them", () => {
    expect(matchBff("GET", "pipeline")?.tenant).toBe(true);
    expect(matchBff("GET", `trips/${ID}`)).not.toBeNull();
    expect(matchBff("PATCH", `trips/${ID}`)).not.toBeNull();
    for (const action of ["stage", "owner", "notes"]) expect(matchBff("POST", `trips/${ID}/${action}`)).not.toBeNull();
    expect(matchBff("POST", `tasks/${ID}/status`)).not.toBeNull();
    expect(matchBff("POST", `travelers/${ID}/reveal`)).not.toBeNull();
    expect(matchBff("GET", "members")).not.toBeNull();
    expect(matchBff("POST", "intake-forms")).not.toBeNull();
    expect(matchBff("PATCH", `intake-forms/${ID}`)).not.toBeNull();
    expect(matchBff("DELETE", `trips/${ID}`)).toBeNull();
    expect(matchBff("GET", "trips/not-a-uuid")).toBeNull();
    expect(matchBff("POST", `trips/${ID}/cancel`)).toBeNull();
    expect(matchBff("GET", `travelers/${ID}/reveal`)).toBeNull();
    expect(matchBff("GET", "pipeline/../me")).toBeNull();
  });
});

describe("inquiry form helpers", () => {
  beforeEach(() => clearFormMemo());

  it("recognises form paths and builds links and embed code", () => {
    expect(formKey(`/c/f/${KEY}`)).toBe(KEY);
    expect(formKey(`/c/f/${KEY}/`)).toBe(KEY);
    expect(formKey("/c/f/short")).toBeNull();
    expect(formKey(`/c/f/${KEY}/x`)).toBeNull();
    expect(hostedUrl([], KEY)).toBeNull();
    const url = hostedUrl(["go.example.test"], KEY)!;
    expect(url).toBe(`https://go.example.test/c/f/${KEY}`);
    expect(embedSnippet(url, 'Web "form" <x>')).toBe(
      `<iframe src="${url}" title="Web form x" style="width:100%;min-height:960px;border:0" loading="lazy"></iframe>`,
    );
  });

  it("frames only from the form's https origins", () => {
    expect(frameAncestors(FORM)).toBe("'self' https://www.agency.example");
    expect(frameAncestors(null)).toBe("'self'");
  });

  it("parses children's ages", () => {
    expect(parseAges("7, 12")).toEqual([7, 12]);
    expect(parseAges("3 5;9")).toEqual([3, 5, 9]);
    expect(parseAges("18")).toBeNull();
    expect(parseAges("seven")).toBeNull();
  });

  it("memoises found and missing forms, never a network error", async () => {
    const ok = vi.fn(async () => new Response(JSON.stringify(FORM), { status: 200 }));
    expect((await loadForm("https://api.test", KEY, { fetchImpl: ok, now: 0 })).ok).toBe(true);
    await loadForm("https://api.test", KEY, { fetchImpl: ok, now: 30_000 });
    expect(ok).toHaveBeenCalledTimes(1);
    await loadForm("https://api.test", KEY, { fetchImpl: ok, now: 61_000 });
    expect(ok).toHaveBeenCalledTimes(2);
    expect(String((ok.mock.calls[0] as unknown[])[0])).toBe(`https://api.test/crm/public/v1/forms/${KEY}`);
    const gone = vi.fn(async () => new Response("{}", { status: 404 }));
    expect(await loadForm("https://api.test", "Z".repeat(22), { fetchImpl: gone, now: 0 })).toEqual({ ok: false, status: 404 });
    await loadForm("https://api.test", "Z".repeat(22), { fetchImpl: gone, now: 1 });
    expect(gone).toHaveBeenCalledTimes(1);
    const down = vi.fn(async () => {
      throw new Error("offline");
    });
    expect(await loadForm("https://api.test", "Y".repeat(22), { fetchImpl: down, now: 0 })).toEqual({ ok: false, status: 0 });
    await loadForm("https://api.test", "Y".repeat(22), { fetchImpl: down, now: 1 });
    expect(down).toHaveBeenCalledTimes(2);
  });

  it("keeps the last good form when a refresh fails or is rate limited", async () => {
    clearFormMemo();
    const ok = vi.fn(async () => new Response(JSON.stringify(FORM), { status: 200 }));
    await loadForm("https://api.test", KEY, { fetchImpl: ok, now: 0 });
    const limited = vi.fn(async () => new Response("{}", { status: 429 }));
    const stale = await loadForm("https://api.test", KEY, { fetchImpl: limited, now: 61_000 });
    expect(stale.ok).toBe(true);
    const down = vi.fn(async () => {
      throw new Error("offline");
    });
    expect((await loadForm("https://api.test", KEY, { fetchImpl: down, now: 62_000 })).ok).toBe(true);
    const gone = vi.fn(async () => new Response("{}", { status: 404 }));
    expect(await loadForm("https://api.test", KEY, { fetchImpl: gone, now: 63_000 })).toEqual({ ok: false, status: 404 });
  });

  it("caps memo misses per viewer and keeps random keys from evicting real forms", async () => {
    clearFormMemo();
    const ok = vi.fn(async () => new Response(JSON.stringify(FORM), { status: 200 }));
    await loadForm("https://api.test", KEY, { fetchImpl: ok, now: 0, viewer: "198.51.100.7" });
    const gone = vi.fn(async () => new Response("{}", { status: 404 }));
    const results = [];
    for (let i = 0; i < MISS_LIMIT_PER_MINUTE + 5; i++) {
      results.push(await loadForm("https://api.test", `r${String(i).padStart(21, "x")}`, { fetchImpl: gone, now: 1, viewer: "203.0.113.9" }));
    }
    expect(gone).toHaveBeenCalledTimes(MISS_LIMIT_PER_MINUTE); // the rest never reach the API
    expect(results.at(-1)).toEqual({ ok: false, status: 429 });
    // Another viewer is not affected, and the real form is still memoised.
    expect((await loadForm("https://api.test", KEY, { fetchImpl: gone, now: 2, viewer: "198.51.100.8" })).ok).toBe(true);
    expect(ok).toHaveBeenCalledTimes(1);
    // A new minute: the viewer may miss again.
    await loadForm("https://api.test", "n".repeat(22), { fetchImpl: gone, now: 61_000, viewer: "203.0.113.9" });
    expect(gone).toHaveBeenCalledTimes(MISS_LIMIT_PER_MINUTE + 1);
  });

  it("derives the viewer key from CloudFront-Viewer-Address", () => {
    expect(viewerKey("203.0.113.7:46532")).toBe("203.0.113.7");
    expect(viewerKey("2001:db8:1:2:3:4:5:6:443")).toBe("2001:db8:1:2::/64");
    expect(viewerKey("2001:db8::1:443")).toBe("2001:db8:0:0::/64");
    expect(viewerKey(null)).toBe("local");
    expect(viewerKey("")).toBe("local");
  });
});

describe("form page CSP (proxy)", () => {
  beforeEach(() => {
    testConfig();
    clearFormMemo();
  });
  afterEach(() => vi.unstubAllGlobals());

  function req(url: string) {
    const u = new URL(url);
    return new NextRequest(url, { headers: { host: u.host } });
  }

  it("lets the agency's websites frame its form, nothing else", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(FORM), { status: 200 })));
    const res = await proxy(req(`https://go.example.test/c/f/${KEY}`));
    expect(res.headers.get("content-security-policy")).toContain("frame-ancestors 'self' https://www.agency.example;");
    const other = await proxy(req("https://go.example.test/c/inquiry"));
    expect(other.headers.get("content-security-policy")).toContain("frame-ancestors 'none'");
  });

  it("an unknown form is framable by the client host only", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 })));
    const res = await proxy(req(`https://go.example.test/c/f/${KEY}`));
    expect(res.headers.get("content-security-policy")).toContain("frame-ancestors 'self';");
    expect((await proxy(req(`https://partner.example.test/c/f/${KEY}`))).status).toBe(404);
  });
});

describe("EN/VI", () => {
  it("every AA-936 key exists in both languages and fills its slots", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => /^(pipeline|trip|intake|forms|stage|waiting|budget|lost)\./.test(k));
    expect(keys.length).toBeGreaterThan(80);
    for (const k of keys) expect(MESSAGES.vi[k as keyof typeof MESSAGES.vi], k).toBeTruthy();
    expect(fmt(MESSAGES.vi["intake.title"], { agency: FORM.agency_name })).toBe("Lên kế hoạch chuyến đi cùng Lữ hành Sen Vàng");
    expect(fmt("{a} {b}", { a: 1 })).toBe("1 {b}");
  });
});
