// AA-937 (CRM-09): client helpers (masking, search kind, links, base64, file checks), the BFF allow-list lines and
// the document body limit, and the EN/VI strings of every code the client pages label.
import { describe, expect, it } from "vitest";
import { openSearch, SEARCH_TTL_S, searchCookie, searchCookieName } from "@/lib/clientSearch";
import type { Session } from "@/lib/session";
import { testConfig } from "./helpers";
import { BFF_ROUTES, DOCUMENT_BODY_BYTES, matchBff } from "@/lib/bff";
import {
  AGE_BANDS,
  DIETARY,
  DOC_KINDS,
  DOC_MAX_BYTES,
  MASK,
  MEDICAL,
  MOBILITY,
  PROFILE_CHOICES,
  RELATION_KINDS,
  REVEAL_FIELDS,
  REVEAL_REASONS,
  bandLabel,
  consentLabel,
  directoryHref,
  fileProblem,
  fileSize,
  fromBase64,
  label,
  maskedValue,
  parseDirectory,
  relationLabel,
  revealText,
  searchKind,
  toBase64,
  tripsCount,
} from "@/lib/clients";
import { MESSAGES, type MessageKey } from "@/lib/i18n";
import { SECTIONS } from "@/lib/nav";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const ID2 = "1f8fad5b-d9cb-469f-a165-70867728950e";

describe("masking and reveal text", () => {
  it("never shows a digit or a letter of a masked value (Fix B15)", () => {
    expect(maskedValue(true)).toBe(MASK);
    expect(MASK).toMatch(/^•+$/);
    expect(maskedValue(false)).toBe("—");
  });

  it("formats revealed values and health codes", () => {
    expect(revealText("en", "email", "anna@example.test")).toBe("anna@example.test");
    expect(revealText("en", "phone", null)).toBe("—");
    expect(revealText("en", "health", { dietary: ["vegan"], mobility: ["no_stairs"], medical: ["asthma"] })).toBe("Vegan, No stairs, Asthma");
    expect(revealText("vi", "health", { dietary: [], mobility: [], medical: [] })).toBe("Không có mã sức khoẻ");
  });
});

describe("directory helpers", () => {
  it("detects what an exact search matches on (R35)", () => {
    expect(searchKind("")).toBe("none");
    expect(searchKind("Anna@Example.test")).toBe("email");
    expect(searchKind("+84 912 345 678")).toBe("phone");
    expect(searchKind("Nguyễn Hoa")).toBe("name");
  });

  it("builds filter links and parses them back", () => {
    expect(directoryHref({})).toBe("/clients");
    // The search term is never in a link: only the "search applies" flag.
    expect(directoryHref({ search: true, archived: true, owner: "me", page: 3 })).toBe("/clients?s=1&archived=true&owner=me&page=3");
    expect(parseDirectory({ s: "1", q: "anna@example.test", archived: "true", owner: "me", page: "2" })).toEqual({ search: true, archived: true, owner: "me", page: 2 });
    expect(parseDirectory({ page: "0", owner: "x" })).toEqual({ search: false, archived: false, owner: "anyone", page: 1 });
    expect(parseDirectory({ page: "abc" }).page).toBe(1);
  });

  it("labels codes, bands, relations and consent in both languages", () => {
    expect(label("en", "dietary", "vegetarian")).toBe("Vegetarian");
    expect(label("en", "doc", "passport_scan")).toBe("Passport scan");
    // S262 round 3: every lead source the API writes has a label (lib/i18nTripClosed.ts); an unknown code stays raw here.
    expect(label("en", "source", "website_form")).toBe("Website form");
    expect(label("en", "source", "facebook_ads")).toBe("facebook_ads");
    expect(label("en", "doc", null)).toBe("—");
    expect(bandLabel("en", "35_44")).toBe("35–44");
    expect(bandLabel("vi", null)).toBe("—");
    expect(relationLabel("en", "referrer")).toBe("Referred this client");
    expect(relationLabel("vi", "child")).toBe("Con");
    const consent = { marketing: { value: false, at: "2026-10-24T00:00:00Z", source: "form" }, contact: { value: true, at: "2026-10-24T00:00:00Z", source: "form" } };
    expect(consentLabel("en", consent, "contact")).toBe("Yes");
    expect(consentLabel("en", consent, "marketing")).toBe("No");
    expect(consentLabel("vi", consent, "health_data")).toBe("Chưa hỏi");
    expect(tripsCount("en", 2)).toBe("2 trips");
  });
});

describe("documents in the browser", () => {
  it("round-trips bytes through base64 and checks size and type before sending", () => {
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x00, 0xff, 0x10]);
    expect(Array.from(fromBase64(toBase64(bytes)))).toEqual(Array.from(bytes));
    const big = new Uint8Array(70_000).fill(7);
    expect(fromBase64(toBase64(big)).length).toBe(70_000);
    expect(fileProblem({ size: 10, type: "application/pdf" })).toBeNull();
    expect(fileProblem({ size: DOC_MAX_BYTES + 1, type: "application/pdf" })).toBe("too_big");
    expect(fileProblem({ size: 10, type: "text/html" })).toBe("type");
    expect(fileSize(512)).toBe("512 B");
    expect(fileSize(2048)).toBe("2 KB");
    expect(fileSize(3 * 1024 * 1024)).toBe("3.0 MB");
  });

  it("keeps a 7 MiB file's base64 under the document body limit and that under the proxy's 10 MiB", () => {
    expect(Math.ceil((DOC_MAX_BYTES * 4) / 3) + 1024).toBeLessThan(DOCUMENT_BODY_BYTES);
    expect(DOCUMENT_BODY_BYTES).toBeLessThanOrEqual(10 * 1024 * 1024);
  });
});

describe("BFF allow-list (AA-937 lines)", () => {
  it("forwards the client, party and document routes and nothing near them", () => {
    for (const [method, path] of [
      ["GET", "travelers"],
      ["POST", "travelers"],
      ["POST", "travelers/search"],
      ["GET", `travelers/${ID}`],
      ["PATCH", `travelers/${ID}`],
      ["PATCH", `travelers/${ID}/profile`],
      ["PUT", `travelers/${ID}/health`],
      ["POST", `travelers/${ID}/reveal`],
      ["POST", `travelers/${ID}/relations`],
      ["DELETE", `travelers/${ID}/relations/${ID2}`],
      ["POST", `trips/${ID}/travelers`],
      ["DELETE", `trips/${ID}/travelers/${ID2}`],
      ["GET", `trips/${ID}/documents`],
      ["POST", "documents"],
      ["POST", `documents/${ID}/download`],
      ["PATCH", `documents/${ID}`],
      ["DELETE", `documents/${ID}`],
    ] as const) {
      expect(matchBff(method, path)?.tenant, `${method} ${path}`).toBe(true);
    }
    for (const [method, path] of [
      ["DELETE", `travelers/${ID}/profile`], // AA-942 opened DELETE travelers/<id> (to the trash), nothing below it
      ["GET", `documents/${ID}`],
      ["GET", `travelers/${ID}/reveal`],
      ["PUT", `travelers/${ID}`],
      ["POST", `travelers/${ID}/health`],
      ["GET", `documents/${ID}/download`],
      ["POST", `travelers/not-an-id/reveal`],
    ] as const) {
      expect(matchBff(method, path), `${method} ${path}`).toBeNull();
    }
  });

  it("raises the body limit for the uploads only (documents; AA-945 the mail history file; AA-962 the client import)", () => {
    expect(matchBff("POST", "documents")?.maxBody).toBe(DOCUMENT_BODY_BYTES);
    expect(BFF_ROUTES.filter((r) => r.maxBody !== undefined).map((r) => `${r.method} ${r.pattern.source}`)).toEqual([
      "POST ^documents$",
      "POST ^mail\\/imports$",
      "POST ^client-imports(\\/preview)?$",
    ]);
  });
});

describe("strings", () => {
  it("label every code the client pages show, in EN and VI", () => {
    const keys: string[] = [
      ...REVEAL_FIELDS.map((f) => `field.${f}`),
      ...REVEAL_REASONS.map((r) => `why.${r}`),
      ...RELATION_KINDS.map((k) => `relation.${k}`),
      ...DOC_KINDS.map((k) => `doc.${k}`),
      ...DIETARY.map((c) => `dietary.${c}`),
      ...MOBILITY.map((c) => `mobility.${c}`),
      ...MEDICAL.map((c) => `medical.${c}`),
      ...AGE_BANDS.map((b) => `age.${b}`),
      ...Object.keys(PROFILE_CHOICES).map((k) => `profile.${k}`),
    ];
    const prefixes: Record<string, string> = {
      group_pref: "group",
      fitness_band: "fitness",
      trek_experience: "trek",
      accommodation_tier: "tier",
      occasion: "occasion",
      budget_flex: "flex",
      decision_maker: "decider",
      booking_timeline: "timeline",
      budget_band: "budget",
      pace: "style",
    };
    for (const [k, codes] of Object.entries(PROFILE_CHOICES)) for (const c of codes) keys.push(`${prefixes[k]}.${c}`);
    for (const key of keys) {
      expect(MESSAGES.en[key as MessageKey], `en ${key}`).toBeTruthy();
      expect(MESSAGES.vi[key as MessageKey], `vi ${key}`).toBeTruthy();
    }
  });

  it("lists the Clients page in the CRM group (no issue ID on the page)", () => {
    const clients = SECTIONS.find((s) => s.key === "clients");
    expect(clients?.path).toBe("/clients");
    expect(clients?.group).toBe("crm");
    expect(Object.values(clients ?? {}).join(" ")).not.toMatch(/AA-\d+/);
  });
});

describe("search term out of URLs (lib/clientSearch.ts)", () => {
  const cfg = testConfig();
  const session = { v: 1, at: "a", sub: "user-1", tid: ID, exp: 9_999_999_999 } as unknown as Session;

  it("seals the term for this person and agency only, for 15 minutes", () => {
    const c = searchCookie(cfg, session, "anna@example.test", 1000);
    expect(c.path).toBe("/clients");
    expect(c.httpOnly).toBe(true);
    expect(c.maxAge).toBe(SEARCH_TTL_S);
    expect(c.value).not.toContain("anna");
    expect(openSearch(cfg, session, c.value, 1001)).toBe("anna@example.test");
    expect(openSearch(cfg, { ...session, sub: "user-2" }, c.value, 1001)).toBe("");
    expect(openSearch(cfg, { ...session, tid: ID2 }, c.value, 1001)).toBe("");
    expect(openSearch(cfg, session, c.value, 1000 + SEARCH_TTL_S)).toBe("");
    expect(openSearch(cfg, null, c.value, 1001)).toBe("");
    expect(openSearch(cfg, session, "garbage", 1001)).toBe("");
  });

  it("an empty term or no session clears the cookie", () => {
    expect(searchCookie(cfg, session, "", 1000)).toMatchObject({ value: "", maxAge: 0 });
    expect(searchCookie(cfg, null, "anna", 1000)).toMatchObject({ value: "", maxAge: 0 });
    expect(searchCookieName({ secureCookies: true })).toBe("__Secure-aa_crm_q");
  });
});
