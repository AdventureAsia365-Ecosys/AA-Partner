// AA-962 (card CRM-31) web: the Home list of steps, the import file guard and query, the error keys (a missing
// column names the columns), the template header, the raw upload, the merge refusal shape, the BFF allow-list entries
// and every onboarding string in both languages. Invented values only.
import { describe, expect, it } from "vitest";
import { CLIENT_IMPORT_BODY_BYTES, matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import { onboardingEn, onboardingVi } from "@/lib/i18nOnboarding";
import {
  ACK_VERSION,
  checkImportFile,
  connectionTone,
  homeSteps,
  importErrorKey,
  importQuery,
  MAX_IMPORT_FILE_BYTES,
  mergeBlock,
  missingColumns,
  normaliseRegion,
  templateCsv,
  uploadRaw,
  type OnboardingView,
  type Step,
} from "@/lib/onboarding";

const ID = "00000000-0000-4000-8000-0000000000a9";

function step(key: Step["key"], over: Partial<Step> = {}): Step {
  return { key, required: false, done: false, marked_done: false, later: false, can_mark_done: false, href: "/", help: "/help", ...over };
}

describe("checklist", () => {
  it("Home shows the required steps, then the optional ones neither done nor on later", () => {
    const view = {
      steps: [
        step("connect_mailbox", { required: true }),
        step("invite_team", { required: true, done: true }),
        step("embed_form", { required: true }),
        step("import_clients"),
        step("review_duplicates", { later: true }),
        step("templates", { done: true }),
      ],
    } as OnboardingView;
    expect(homeSteps(view).map((s) => s.key)).toEqual(["connect_mailbox", "invite_team", "embed_form", "import_clients"]);
  });

  it("maps the four connection states to badge tones", () => {
    expect(["not_set_up", "connected", "needs_attention", "disconnected"].map((s) => connectionTone(s as never))).toEqual([
      "neutral",
      "success",
      "warning",
      "danger",
    ]);
  });

  it("pins the acknowledgement text version the API checks", () => {
    expect(ACK_VERSION).toMatch(/^\d{4}-\d{2}-\d{2}\.\d+$/);
  });
});

describe("import", () => {
  it("guards the file before any byte is sent", () => {
    expect(checkImportFile({ size: 10, name: "clients.CSV" })).toBe("ok");
    expect(checkImportFile({ size: 10, name: "clients.xlsx" })).toBe("ok");
    expect(checkImportFile({ size: 0, name: "a.csv" })).toBe("empty");
    expect(checkImportFile({ size: MAX_IMPORT_FILE_BYTES + 1, name: "a.csv" })).toBe("too_large");
    expect(checkImportFile({ size: 10, name: "a.pdf" })).toBe("wrong_type");
  });

  it("builds the query with the previewed hash and an optional phone country", () => {
    expect(normaliseRegion(" vn ")).toBe("VN");
    expect(normaliseRegion("VNM")).toBe("");
    expect(importQuery("clients", "VN")).toBe("entity=clients&phone_region=VN");
    expect(importQuery("notes", "", "ab".repeat(32))).toBe(`entity=notes&sha256=${"ab".repeat(32)}`);
  });

  it("names the error, the missing columns included", () => {
    expect(importErrorKey(413, "body_too_large")).toBe("onb.err.import_too_large");
    expect(importErrorKey(422, "missing_columns")).toBe("onb.err.missing_columns");
    expect(importErrorKey(500, "boom")).toBe("onb.err.other");
    expect(missingColumns({ detail: "missing_columns", columns: ["name", "client_ref or trip_ref"] })).toEqual([
      "name",
      "client_ref or trip_ref",
    ]);
    expect(missingColumns(null)).toEqual([]);
  });

  it("offers a template header with every column once", () => {
    expect(templateCsv("notes")).toBe("text,client_ref,trip_ref,note_ref,date\n");
    expect(templateCsv("clients").startsWith("client_ref,name,")).toBe(true);
  });

  it("POSTs the raw file to the BFF path and returns the API's error body", async () => {
    const sent: { url?: string; headers: Record<string, string>; body?: unknown } = { headers: {} };
    class FakeXhr {
      status = 422;
      responseText = JSON.stringify({ detail: "missing_columns", columns: ["name"] });
      withCredentials = false;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      open(_m: string, url: string) {
        sent.url = url;
      }
      setRequestHeader(k: string, v: string) {
        sent.headers[k] = v;
      }
      send(body: unknown) {
        sent.body = body;
        this.onload?.();
      }
    }
    const file = new Blob(["client_ref\nC-1\n"]);
    const res = await uploadRaw("client-imports/preview?entity=clients", file, FakeXhr as unknown as typeof XMLHttpRequest);
    expect(sent.url).toBe("/bff/client-imports/preview?entity=clients");
    expect(sent.headers["Content-Type"]).toBe("application/octet-stream");
    expect(sent.body).toBe(file);
    expect(res).toEqual({ ok: false, status: 422, detail: "missing_columns", body: { detail: "missing_columns", columns: ["name"] } });
  });

  it("reads a merge refusal", () => {
    expect(mergeBlock({ detail: "merge_blocked", blockers: ["client_links", 3], swap_possible: true })).toEqual({
      blockers: ["client_links"],
      swap: true,
    });
    expect(mergeBlock(null)).toEqual({ blockers: [], swap: false });
  });
});

describe("BFF allow-list", () => {
  it("lists the onboarding, import, duplicate and merge routes", () => {
    for (const [method, path] of [
      ["GET", "onboarding"],
      ["PUT", "onboarding/steps/kickoff_call"],
      ["PUT", "onboarding/controller"],
      ["POST", "onboarding/acknowledgement"],
      ["GET", "client-imports"],
      ["GET", `client-imports/${ID}`],
      ["GET", "client-duplicates"],
      ["POST", `travelers/${ID}/merge`],
    ] as const) {
      expect(matchBff(method, path)?.tenant, `${method} ${path}`).toBe(true);
    }
    for (const path of ["client-imports", "client-imports/preview"]) {
      const route = matchBff("POST", path);
      expect(route?.raw).toBe(true);
      expect(route?.maxBody).toBe(CLIENT_IMPORT_BODY_BYTES);
    }
    expect(CLIENT_IMPORT_BODY_BYTES).toBe(MAX_IMPORT_FILE_BYTES);
    expect(matchBff("POST", "onboarding/steps/../x")).toBeNull();
    expect(matchBff("DELETE", "client-imports")).toBeNull();
  });
});

describe("strings", () => {
  it("has every onboarding key in English and Vietnamese, and spreads them into the dictionary", () => {
    expect(Object.keys(onboardingVi).sort()).toEqual(Object.keys(onboardingEn).sort());
    for (const key of Object.keys(onboardingEn) as (keyof typeof onboardingEn)[]) {
      expect(MESSAGES.en[key]).toBe(onboardingEn[key]);
      expect(MESSAGES.vi[key]).toBe(onboardingVi[key]);
      expect(onboardingVi[key].length).toBeGreaterThan(0);
    }
  });
});
