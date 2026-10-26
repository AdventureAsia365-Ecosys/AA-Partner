// AA-945 (CRM-17) web, the mail history import: the kind from the file name, the 25 MB guard before any byte is sent,
// the poller that stops on done / failed / a 4xx, the error keys, the remembered ids, the raw upload through the BFF
// (bytes unchanged, capped, never cut short) and the proxy matcher that keeps Next from cloning (and cutting) it.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as bffGet, POST as bffPost } from "@/app/bff/[...path]/route";
import { MAIL_IMPORT_BODY_BYTES, matchBff, MAX_RAW_IN_FLIGHT, UNPROXIED_BFF_PREFIX } from "@/lib/bff";
import { readCapped } from "@/lib/bffBody";
import type { BffResult } from "@/lib/bffClient";
import { MESSAGES } from "@/lib/i18n";
import {
  apiMessage,
  checkFile,
  createImportPoller,
  detectKind,
  failureKey,
  formatBytes,
  importErrorKey,
  IMPORTS_STORAGE_KEY,
  MAX_IMPORT_BYTES,
  MAX_POLL_ERRORS,
  readRemembered,
  uploadImport,
  writeRemembered,
  type MailImport,
} from "@/lib/mailImport";
import { config as proxyConfig } from "@/proxy";
import { refreshCookie, sessionCookie } from "@/lib/session";
import { fakeAccessToken, testConfig } from "./helpers";

const ID = "0f0e0d0c-0b0a-4908-8706-050403020100";

function row(status: MailImport["status"], extra: Partial<MailImport> = {}): MailImport {
  return {
    mail_import_id: ID,
    source_kind: "mbox",
    size_bytes: 1200,
    status,
    total_count: 10,
    imported_count: 7,
    duplicate_count: 2,
    skipped_count: 1,
    failed_count: 0,
    error_code: null,
    created_at: "2026-10-26T01:00:00Z",
    finished_at: null,
    ...extra,
  };
}

describe("file kind from the name", () => {
  it("reads .csv and .mbox / .mbx in any case; anything else is for the member to pick", () => {
    expect(detectKind("export.csv")).toBe("csv");
    expect(detectKind("EXPORT.CSV")).toBe("csv");
    expect(detectKind("All mail Including Spam and Trash.mbox")).toBe("mbox");
    expect(detectKind("archive.MBX")).toBe("mbox");
    expect(detectKind("Inbox")).toBeNull(); // Thunderbird folder files have no extension
    expect(detectKind(".mbox")).toBeNull();
    expect(detectKind("mail.")).toBeNull();
    expect(detectKind("mail.zip")).toBeNull();
    expect(detectKind("mail.csv.txt")).toBeNull();
  });
});

describe("25 MB guard", () => {
  it("allows 1 byte up to exactly 25 MiB, refuses empty and larger files", () => {
    expect(MAX_IMPORT_BYTES).toBe(25 * 1024 * 1024);
    expect(MAIL_IMPORT_BODY_BYTES).toBe(MAX_IMPORT_BYTES);
    expect(checkFile({ size: 0 })).toBe("empty");
    expect(checkFile({ size: 1 })).toBe("ok");
    expect(checkFile({ size: MAX_IMPORT_BYTES })).toBe("ok");
    expect(checkFile({ size: MAX_IMPORT_BYTES + 1 })).toBe("too_large");
    expect(formatBytes(MAX_IMPORT_BYTES)).toBe("25.0 MB");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(12)).toBe("12 B");
  });
});

describe("status poller", () => {
  function fakeTimers() {
    const queue: Array<() => void> = [];
    return {
      timers: { set: (fn: () => void) => queue.push(fn), clear: () => queue.splice(0) },
      async fire() {
        const fn = queue.shift();
        expect(fn, "a poll was scheduled").toBeDefined();
        fn!();
        await Promise.resolve();
        await Promise.resolve();
      },
      get pending() {
        return queue.length;
      },
    };
  }

  it("asks at once, then on each tick, and stops for good on done", async () => {
    const answers: BffResult<MailImport>[] = [
      { ok: true, data: row("queued") },
      { ok: true, data: row("running") },
      { ok: true, data: row("done", { finished_at: "2026-10-26T01:02:00Z" }) },
    ];
    const fetchStatus = vi.fn(async () => answers.shift()!);
    const update = vi.fn();
    const error = vi.fn();
    const t = fakeTimers();
    const poller = createImportPoller(ID, fetchStatus, { update, error }, 3000, t.timers);
    poller.start();
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchStatus).toHaveBeenCalledTimes(1);
    await t.fire();
    await t.fire();
    expect(fetchStatus).toHaveBeenCalledTimes(3);
    expect(update.mock.calls.map((c) => (c[0] as MailImport).status)).toEqual(["queued", "running", "done"]);
    expect(t.pending).toBe(0);
    expect(poller.running).toBe(false);
    expect(error).not.toHaveBeenCalled();
  });

  it("stops on failed too", async () => {
    const fetchStatus = vi.fn(async () => ({ ok: true as const, data: row("failed", { error_code: "csv_columns" }) }));
    const t = fakeTimers();
    const poller = createImportPoller(ID, fetchStatus, { update: vi.fn(), error: vi.fn() }, 3000, t.timers);
    poller.start();
    await Promise.resolve();
    await Promise.resolve();
    expect(t.pending).toBe(0);
    expect(poller.running).toBe(false);
  });

  it("stops on a 4xx at once, and after MAX_POLL_ERRORS network / 5xx failures in a row", async () => {
    const t = fakeTimers();
    const error = vi.fn();
    const gone = createImportPoller(
      ID,
      async () => ({ ok: false, status: 404, detail: "mail_import_not_found" }),
      { update: vi.fn(), error },
      3000,
      t.timers,
    );
    gone.start();
    await Promise.resolve();
    await Promise.resolve();
    expect(error).toHaveBeenCalledWith(404, "mail_import_not_found", undefined);
    expect(t.pending).toBe(0);

    const flaky = vi.fn(async () => ({ ok: false as const, status: 502, detail: "api_unreachable" }));
    const error2 = vi.fn();
    const down = createImportPoller(ID, flaky, { update: vi.fn(), error: error2 }, 3000, t.timers);
    down.start();
    await Promise.resolve();
    await Promise.resolve();
    for (let i = 1; i < MAX_POLL_ERRORS; i++) await t.fire();
    expect(flaky).toHaveBeenCalledTimes(MAX_POLL_ERRORS);
    expect(error2).toHaveBeenCalledTimes(1);
    expect(t.pending).toBe(0);
  });

  it("stop() cancels the next tick", async () => {
    const t = fakeTimers();
    const fetchStatus = vi.fn(async () => ({ ok: true as const, data: row("running") }));
    const poller = createImportPoller(ID, fetchStatus, { update: vi.fn(), error: vi.fn() }, 3000, t.timers);
    poller.start();
    await Promise.resolve();
    await Promise.resolve();
    expect(t.pending).toBe(1);
    poller.stop();
    expect(t.pending).toBe(0);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });
});

describe("error texts", () => {
  it("maps any 413 to the size message and known codes to their own text, in both languages", () => {
    expect(importErrorKey(413, "body_too_large")).toBe("mail.error.import_too_large");
    expect(importErrorKey(413, "import_too_large")).toBe("mail.error.import_too_large");
    expect(importErrorKey(422, "empty_upload")).toBe("mail.error.empty_upload");
    expect(importErrorKey(503, "mail_ingest_not_configured")).toBe("mail.error.mail_ingest_not_configured");
    expect(importErrorKey(403, "role_forbidden")).toBe("mail.error.role_forbidden");
    expect(importErrorKey(422, "something_new")).toBe("");
    expect(importErrorKey(503, "upload_busy")).toBe("mail.error.upload_busy");
    expect(importErrorKey(400, "body_incomplete")).toBe("mail.error.body_incomplete");
    for (const code of ["empty_upload", "invalid_source_kind", "mail_ingest_not_configured", "mail_import_not_found", "api_timeout", "api_unreachable", "body_incomplete", "upload_busy"]) {
      expect(MESSAGES.en[`mail.error.${code}` as keyof typeof MESSAGES.en], code).toBeTruthy();
      expect(MESSAGES.vi[`mail.error.${code}` as keyof typeof MESSAGES.vi], code).toBeTruthy();
    }
    for (const code of ["csv_columns", "bad_payload", "import_failed", null, "new_code"]) {
      expect(MESSAGES.en[failureKey(code) as keyof typeof MESSAGES.en]).toBeTruthy();
    }
    expect(apiMessage({ detail: "x", message: "at most 25 MB" })).toBe("at most 25 MB");
    expect(apiMessage({ detail: [{ msg: "bad" }] })).toBeNull();
    expect(apiMessage(null)).toBeNull();
  });
});

describe("remembered imports (this tab only, ids and kinds)", () => {
  it("round-trips, keeps at most five, drops anything that is not an import id", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    const ids = Array.from({ length: 7 }, (_, i) => `0f0e0d0c-0b0a-4908-8706-05040302010${i}`);
    writeRemembered(storage, ids.map((id) => ({ id, kind: "csv" as const })));
    expect(readRemembered(storage).map((r) => r.id)).toEqual(ids.slice(0, 5));
    store.set(IMPORTS_STORAGE_KEY, JSON.stringify([{ id: "../me", kind: "mbox" }, { id: ID, kind: "zip" }, { id: ID, kind: "mbox", name: "x" }]));
    expect(readRemembered(storage)).toEqual([{ id: ID, kind: "mbox", name: "x" }]);
    store.set(IMPORTS_STORAGE_KEY, "{not json");
    expect(readRemembered(storage)).toEqual([]);
    expect(readRemembered(null)).toEqual([]);
  });
});

describe("browser upload (XMLHttpRequest)", () => {
  it("POSTs the file as the raw body with the kind in the query and reports progress", async () => {
    const sent: { method?: string; url?: string; body?: unknown; headers: Record<string, string> } = { headers: {} };
    class FakeXhr {
      status = 202;
      responseText = JSON.stringify({ mail_import_id: ID, status: "queued", created_at: "2026-10-26T01:00:00Z" });
      withCredentials = false;
      upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      open(method: string, url: string) {
        sent.method = method;
        sent.url = url;
      }
      setRequestHeader(k: string, v: string) {
        sent.headers[k] = v;
      }
      send(body: unknown) {
        sent.body = body;
        this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
        this.onload?.();
      }
    }
    const file = new Blob(["From a@example.test\n"]);
    const progress = vi.fn();
    const res = await uploadImport(file, "mbox", progress, FakeXhr as unknown as typeof XMLHttpRequest);
    expect(res).toEqual({ ok: true, data: { mail_import_id: ID, status: "queued", created_at: "2026-10-26T01:00:00Z" } });
    expect(sent.method).toBe("POST");
    expect(sent.url).toBe("/bff/mail/imports?source_kind=mbox");
    expect(sent.headers["Content-Type"]).toBe("application/octet-stream");
    expect(sent.body).toBe(file);
    expect(progress).toHaveBeenCalledWith(0.5);
  });
});

describe("BFF raw upload", () => {
  let cfg = testConfig();
  beforeEach(() => {
    cfg = testConfig();
  });
  afterEach(() => vi.unstubAllGlobals());

  function cookie(): string {
    const now = Math.floor(Date.now() / 1000);
    const s = sessionCookie(cfg, { at: fakeAccessToken(), atExp: now + 1200, iss: "i", sub: "sub-1", cid: "c", tid: "tenant-a" });
    const r = refreshCookie(cfg, "rt-1", "sub-1");
    return `${s.name}=${s.value}; ${r.name}=${r.value}`;
  }

  function req(
    path: string,
    init: { method?: string; body?: BodyInit; headers?: Record<string, string>; host?: string; session?: boolean } = {},
  ) {
    const host = init.host ?? "partner.example.test";
    const h = new Headers({ host, "content-type": "application/octet-stream", ...init.headers });
    if (init.session !== false) h.set("cookie", cookie());
    const streamed = init.body instanceof ReadableStream;
    const options = { method: init.method ?? "POST", headers: h, body: init.body, ...(streamed ? { duplex: "half" } : {}) };
    return new NextRequest(`https://${host}${path}`, options as ConstructorParameters<typeof NextRequest>[1]);
  }
  function post(body: BodyInit, headers: Record<string, string> = {}, host?: string) {
    return req("/bff/mail/imports?source_kind=mbox", { body, headers, host });
  }
  const params = { params: Promise.resolve({ path: ["mail", "imports"] }) };
  const statusParams = { params: Promise.resolve({ path: ["mail", "imports", ID] }) };

  /** A body stream that counts how often it was pulled (highWaterMark 0: nothing is read ahead). */
  function countedBody(bytes = new Uint8Array([1, 2, 3])) {
    const seen = { pulls: 0 };
    const stream = new ReadableStream<Uint8Array>(
      {
        pull(controller) {
          seen.pulls += 1;
          if (seen.pulls === 1) controller.enqueue(bytes);
          else controller.close();
        },
      },
      { highWaterMark: 0 },
    );
    return { stream, seen };
  }

  it("is on the allow-list as a raw route of 25 MiB with a timeout under CloudFront's 60 s", () => {
    const route = matchBff("POST", "mail/imports");
    expect(route?.raw).toBe(true);
    expect(route?.maxBody).toBe(MAIL_IMPORT_BODY_BYTES);
    expect(route?.timeoutMs).toBe(50_000);
    expect(MAX_RAW_IN_FLIGHT).toBe(2);
  });

  it("proxy matcher: the upload paths are left out; encoded and look-alike paths are still host-checked by the route", async () => {
    const matcher = new RegExp(`^${proxyConfig.matcher[0]}$`);
    expect(UNPROXIED_BFF_PREFIX).toBe("/bff/mail/imports");
    expect(matcher.test("/bff/mail/imports")).toBe(false);
    expect(matcher.test(`/bff/mail/imports/${ID}`)).toBe(false);
    expect(matcher.test("/bff/me")).toBe(true);
    expect(matcher.test("/settings/mailbox")).toBe(true);
    // Next runs the proxy when the raw OR the decoded path matches: the encoded form still goes through the proxy
    // (body cloned and possibly cut: the route's Content-Length check then answers 400), and is host-checked there.
    expect(matcher.test("/bff/mail/%69mports")).toBe(true);
    // A look-alike is excluded by the prefix; the allow-list does not know it.
    expect(matcher.test("/bff/mail/importsX")).toBe(false);
    expect(matchBff("POST", "mail/importsX")).toBeNull();

    const upstream = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", upstream);
    // Whatever the proxy did, the route itself refuses a foreign host for every one of these paths.
    const foreign = "go.example.test";
    expect((await bffGet(req("/bff/mail/%69mports", { method: "GET", host: foreign }), { params: Promise.resolve({ path: ["mail", "imports"] }) })).status).toBe(404);
    expect((await bffGet(req(`/bff/mail/imports/${ID}`, { method: "GET", host: foreign }), statusParams)).status).toBe(404);
    expect((await bffPost(req("/bff/mail/importsX", { body: new Uint8Array(1), host: foreign }), { params: Promise.resolve({ path: ["mail", "importsX"] }) })).status).toBe(404);
    expect((await bffPost(req("/bff/mail/importsX", { body: new Uint8Array(1) }), { params: Promise.resolve({ path: ["mail", "importsX"] }) })).status).toBe(404);
    expect((await bffGet(req(`/bff/mail/imports/${ID}`, { method: "GET", host: "unknown.example.test" }), statusParams)).status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("forwards the bytes unchanged (a Blob, not text) with the session's token, agency and the kind", async () => {
    const upstream = vi.fn(async () => Response.json({ mail_import_id: ID, status: "queued" }, { status: 202 }));
    vi.stubGlobal("fetch", upstream);
    const bytes = new Uint8Array([0x46, 0x72, 0x6f, 0x6d, 0x20, 0xff, 0xfe, 0x00, 0xe9, 0xc3, 0x28]); // not valid UTF-8
    const res = await bffPost(post(bytes, { "content-length": String(bytes.byteLength) }), params);
    expect(res.status).toBe(202);
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${cfg.crmApiOrigin}/crm/v1/mail/imports?source_kind=mbox`);
    expect(init.body).toBeInstanceOf(Blob);
    expect(Array.from(new Uint8Array(await (init.body as Blob).arrayBuffer()))).toEqual(Array.from(bytes));
    const h = init.headers as Headers;
    expect(h.get("content-type")).toBe("application/octet-stream");
    expect(h.get("x-aa-tenant-id")).toBe("tenant-a");
  });

  it("refuses a body over 25 MiB (declared), one cut short, a cross-site POST and no session, before the API", async () => {
    const upstream = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", upstream);
    const over = await bffPost(post(new Uint8Array(4), { "content-length": String(MAIL_IMPORT_BODY_BYTES + 1) }), params);
    expect(over.status).toBe(413);
    expect(await over.json()).toEqual({ detail: "body_too_large" });
    const short = await bffPost(post(new Uint8Array(4), { "content-length": "10" }), params);
    expect(short.status).toBe(400);
    expect(await short.json()).toEqual({ detail: "body_incomplete" });

    const cross = countedBody();
    const evil = await bffPost(post(cross.stream, { origin: "https://evil.example" }), params);
    expect(evil.status).toBe(403);
    expect(cross.seen.pulls).toBe(0);

    const anon = countedBody();
    const noSession = await bffPost(req("/bff/mail/imports?source_kind=mbox", { body: anon.stream, session: false }), params);
    expect(noSession.status).toBe(401);
    expect(anon.seen.pulls).toBe(0);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("answers 400 body_incomplete when the upload breaks off mid-way (no unhandled 500)", async () => {
    const upstream = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", upstream);
    let pulls = 0;
    const broken = new ReadableStream<Uint8Array>(
      {
        pull(controller) {
          pulls += 1;
          if (pulls === 1) controller.enqueue(new Uint8Array(8));
          else controller.error(new Error("aborted by the client"));
        },
      },
      { highWaterMark: 0 },
    );
    const res = await bffPost(post(broken), params);
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ detail: "body_incomplete" });
    expect(upstream).not.toHaveBeenCalled();
  });

  it("caps raw uploads in flight: the third waits outside with 503 upload_busy + Retry-After, unread; the slot frees after", async () => {
    const release: Array<() => void> = [];
    const upstream = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          release.push(() => resolve(Response.json({ mail_import_id: ID, status: "queued" }, { status: 202 })));
        }),
    );
    vi.stubGlobal("fetch", upstream);
    const first = bffPost(post(new Uint8Array([1])), params);
    const second = bffPost(post(new Uint8Array([2])), params);
    await vi.waitFor(() => expect(upstream).toHaveBeenCalledTimes(MAX_RAW_IN_FLIGHT));

    const waiting = countedBody();
    const busy = await bffPost(post(waiting.stream), params);
    expect(busy.status).toBe(503);
    expect(busy.headers.get("retry-after")).toBe("10");
    expect(await busy.json()).toEqual({ detail: "upload_busy" });
    expect(waiting.seen.pulls).toBe(0);
    // A status read is not an upload: never counted.
    const statusUp = bffGet(req(`/bff/mail/imports/${ID}`, { method: "GET" }), statusParams);
    await vi.waitFor(() => expect(upstream).toHaveBeenCalledTimes(MAX_RAW_IN_FLIGHT + 1));

    for (const r of release.splice(0)) r();
    expect((await first).status).toBe(202);
    expect((await second).status).toBe(202);
    expect((await statusUp).status).toBe(202);
    const again = bffPost(post(new Uint8Array([3])), params);
    await vi.waitFor(() => expect(release).toHaveLength(1));
    release[0]();
    expect((await again).status).toBe(202);
  });

  it("frees the slot when the API is unreachable too", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("down"); }));
    for (let i = 0; i < MAX_RAW_IN_FLIGHT + 1; i++) {
      expect((await bffPost(post(new Uint8Array([1])), params)).status).toBe(502);
    }
  });

  it("readCapped stops reading as soon as the cap is passed, and reports a broken stream", async () => {
    let pulled = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        if (pulled > 100) controller.close();
        else controller.enqueue(new Uint8Array(10));
      },
    });
    expect(await readCapped(stream, 25)).toBe("too_large");
    expect(pulled).toBeLessThan(10);
    const ok = await readCapped(new Blob([new Uint8Array([1, 2, 3])]).stream(), 3);
    expect(ok).toBeInstanceOf(Blob);
    expect(Array.from(new Uint8Array(await (ok as Blob).arrayBuffer()))).toEqual([1, 2, 3]);
    expect(((await readCapped(null, 3)) as Blob).size).toBe(0);
    const broken = new ReadableStream<Uint8Array>({ pull: (c) => c.error(new Error("reset")) });
    expect(await readCapped(broken, 3)).toBe("aborted");
  });
});
