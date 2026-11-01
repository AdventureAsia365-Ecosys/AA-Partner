// @vitest-environment jsdom
// AA-1014 (S262 CRM review video): a CSV export clicked more than 30 minutes after the page loaded failed with "The file
// could not be downloaded" on every retry. The BFF answers 401 session_expired when the access token has expired and
// the raw fetch() downloads never renewed it. bffDownload / bffFetch renew in the background (/auth/refresh with
// redirect "manual", the browser keeps the new cookie) and try once more; a second 401 reloads through /auth/refresh.
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { bffDownload, bffFetch } = await import("@/lib/bffClient");
const { ExportMenu } = await import("@/components/reports/ExportMenu");

type Call = { url: string; init: RequestInit | undefined };

function respond(status: number, body: unknown, type = "application/json"): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status, headers: { "Content-Type": type } });
}

const EXPIRED = () => respond(401, { detail: "session_expired" });
const CSV = () => respond(200, "name,total\nLotus Lane,3\n", "text/csv");
// Stands in for the opaque answer a fetch with redirect "manual" gives for the 307 of /auth/refresh (the browser has
// stored its Set-Cookie by then; the page never reads it).
const REDIRECTED = () => new Response(null, { status: 200 });

let calls: Call[];
let assign: ReturnType<typeof vi.fn>;
let saved: string[];

function stubFetch(answers: Record<string, Array<() => Response>>) {
  const queues = Object.fromEntries(Object.entries(answers).map(([k, v]) => [k, [...v]]));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const key = Object.keys(queues).find((k) => url.startsWith(k));
      const next = key ? queues[key].shift() : undefined;
      if (!next) throw new Error(`unexpected fetch ${url}`);
      return next();
    }),
  );
}

const refreshCalls = () => calls.filter((c) => c.url.startsWith("/auth/refresh"));
const bffCalls = () => calls.filter((c) => c.url.startsWith("/bff/"));

beforeEach(() => {
  calls = [];
  saved = [];
  assign = vi.fn();
  vi.stubGlobal("location", { ...window.location, pathname: "/reports", search: "?period=30d", assign });
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:file");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("bffDownload", () => {
  it("renews an expired token once in the background, then downloads", async () => {
    stubFetch({ "/bff/": [EXPIRED, CSV], "/auth/refresh": [REDIRECTED] });
    const res = await bffDownload("reports/export/pipeline?period=30d", { filename: "pipeline.csv" });
    expect(res).toEqual({ ok: true, data: null });
    expect(bffCalls().map((c) => c.url)).toEqual(["/bff/reports/export/pipeline?period=30d", "/bff/reports/export/pipeline?period=30d"]);
    expect(refreshCalls()).toHaveLength(1);
    const renew = refreshCalls()[0];
    expect(renew.url).toBe(`/auth/refresh?next=${encodeURIComponent("/reports?period=30d")}`);
    expect(renew.init).toMatchObject({ credentials: "same-origin", redirect: "manual" });
    expect(bffCalls().every((c) => c.init?.credentials === "same-origin")).toBe(true);
    expect(saved).toEqual(["pipeline.csv"]);
    expect(assign).not.toHaveBeenCalled();
  });

  it("sends the person to sign in again when the renewal did not help", async () => {
    stubFetch({ "/bff/": [EXPIRED, EXPIRED], "/auth/refresh": [REDIRECTED] });
    const res = await bffDownload("reports/export/pipeline", { filename: "pipeline.csv" });
    expect(res).toMatchObject({ ok: false, status: 401, detail: "session_expired" });
    expect(refreshCalls()).toHaveLength(1);
    expect(bffCalls()).toHaveLength(2);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(`/auth/refresh?next=${encodeURIComponent("/reports?period=30d")}`);
    expect(saved).toEqual([]);
  });

  it("downloads straight away with a valid token (no renewal)", async () => {
    stubFetch({ "/bff/": [CSV] });
    expect(await bffDownload("reports/export/pipeline", { filename: "a.csv" })).toEqual({ ok: true, data: null });
    expect(refreshCalls()).toHaveLength(0);
    expect(saved).toEqual(["a.csv"]);
  });

  it("returns the API's refusal without renewing (403 is not an expired token)", async () => {
    stubFetch({ "/bff/": [() => respond(403, { detail: "forbidden" })] });
    expect(await bffDownload("reports/export/margins", { filename: "m.csv" })).toMatchObject({ ok: false, status: 403, detail: "forbidden" });
    expect(refreshCalls()).toHaveLength(0);
    expect(assign).not.toHaveBeenCalled();
    expect(saved).toEqual([]);
  });

  it("a renewal that fails on the network still lets the retry decide", async () => {
    stubFetch({ "/bff/": [EXPIRED, CSV], "/auth/refresh": [() => { throw new TypeError("offline"); }] });
    expect(await bffDownload("access-log/export?format=csv", { filename: "log.csv" })).toEqual({ ok: true, data: null });
    expect(saved).toEqual(["log.csv"]);
  });

  it("a network failure is a plain error, not a sign-in", async () => {
    stubFetch({});
    expect(await bffDownload("reports/export/pipeline", { filename: "a.csv" })).toMatchObject({ ok: false, status: 0, detail: "network" });
    expect(assign).not.toHaveBeenCalled();
  });

  it("two downloads that expire together share one renewal", async () => {
    stubFetch({ "/bff/": [EXPIRED, EXPIRED, CSV, CSV], "/auth/refresh": [REDIRECTED] });
    const both = await Promise.all([
      bffDownload("reports/export/pipeline", { filename: "a.csv" }),
      bffDownload("reports/export/clients", { filename: "b.csv" }),
    ]);
    expect(both.every((r) => r.ok)).toBe(true);
    expect(refreshCalls()).toHaveLength(1);
  });
});

describe("bffFetch", () => {
  it("keeps the caller's headers and returns the retried response", async () => {
    stubFetch({ "/bff/": [EXPIRED, () => respond(200, { events: [], next_cursor: null })], "/auth/refresh": [REDIRECTED] });
    const res = await bffFetch("access-log/events?cursor=x", { headers: { Accept: "application/json" } });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ events: [], next_cursor: null });
    expect(bffCalls()[1].init).toMatchObject({ headers: { Accept: "application/json" }, credentials: "same-origin" });
  });
});

describe("Reports export button", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
  });

  async function clickExport(): Promise<HTMLDivElement> {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root!.render(createElement(ExportMenu, { lang: "en", period: "30d", owner: null, moneyVisible: false })));
    const button = host.querySelector("button")!;
    await act(async () => {
      button.click();
    });
    return host;
  }

  it("downloads after a silent renewal and shows no error", async () => {
    stubFetch({ "/bff/": [EXPIRED, CSV], "/auth/refresh": [REDIRECTED] });
    const page = await clickExport();
    expect(refreshCalls()).toHaveLength(1);
    expect(saved).toHaveLength(1);
    expect(page.querySelector('[role="alert"]')).toBeNull();
  });

  it("shows the error for a refusal that is not an expired token", async () => {
    stubFetch({ "/bff/": [() => respond(500, { detail: "error" })] });
    const page = await clickExport();
    expect(page.querySelector('[role="alert"]')?.textContent).toBe("The file could not be downloaded. Try again.");
  });
});

describe("no raw /bff fetch in the browser code", () => {
  // Every browser call to the BFF goes through lib/bffClient.ts, so an expired token is always renewed.
  const WEB = path.resolve(__dirname, "..");
  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = path.join(dir, name);
      if (statSync(full).isDirectory()) return files(full);
      return /\.tsx?$/.test(name) ? [full] : [];
    });
  }

  it("components and lib call /bff only through bffClient", () => {
    const offenders = [...files(path.join(WEB, "components")), ...files(path.join(WEB, "lib"))]
      .filter((f) => !f.endsWith(path.join("lib", "bffClient.ts")))
      .filter((f) => /fetch\(\s*[`'"]\/bff\//.test(readFileSync(f, "utf8")))
      .map((f) => path.relative(WEB, f));
    expect(offenders).toEqual([]);
  });
});
