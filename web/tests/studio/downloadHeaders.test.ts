// AA-1040 (EU AI Act Art. 50(2)) — the Content studio BFF keeps the AI mark of a download: the X-AI-* headers CIS
// sends on a piece export and the DOCX file name pass the tenant proxy (checked, rebuilt), the bodies stream through
// untouched (data-ai-* on the <article>, <meta name="ai-*">), and nothing else from upstream leaks through.
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as tenantProxyGet } from "@/app/api/tenant/[...path]/route";
import { sessionCookie } from "@/lib/session";
import { AI_MARK_HEADERS, dispositionFileName, studioDownloadHeaders } from "@/lib/studio/downloadHeaders";
import { fakeAccessToken, testConfig } from "../helpers";

const ORIGIN = "https://partner.example.test";
const CIS = "https://cis.example.test";
const TENANT = "5b0c7a1e-2d3f-4e6a-9b8c-1d2e3f4a5b6c";
const PIECE = "0f1e2d3c-4b5a-4968-8776-655443322110";
const VERSION = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const GENERATOR = "Adventure Asia content writer";
const MODEL = "example-writer-model-1";

let cfg = testConfig({ CIS_API_URL: CIS });
beforeEach(() => {
  cfg = testConfig({ CIS_API_URL: CIS });
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("studioDownloadHeaders (the allow-list)", () => {
  it("passes the AI mark and a checked file name, nothing else", () => {
    const out = studioDownloadHeaders(new Headers({
      "X-AI-Generated": "true",
      "X-AI-Generator": GENERATOR,
      "X-AI-Model": MODEL,
      "Content-Disposition": "attachment; filename=tour_1a2b3c4d_v3.docx",
      "Set-Cookie": "cis=1",
      "Access-Control-Allow-Origin": "*",
      "X-Other": "x",
    }));
    expect(out).toEqual({
      "X-AI-Generated": "true",
      "X-AI-Generator": GENERATOR,
      "X-AI-Model": MODEL,
      "Content-Disposition": 'attachment; filename="tour_1a2b3c4d_v3.docx"',
    });
    expect([...AI_MARK_HEADERS]).toEqual(["X-AI-Generated", "X-AI-Generator", "X-AI-Model"]);
  });

  it("an ordinary JSON read gets no extra header", () => {
    expect(studioDownloadHeaders(new Headers({ "Content-Type": "application/json" }))).toEqual({});
  });

  it("drops the whole mark when X-AI-Generated is not exactly true or false", () => {
    for (const bad of ["yes", "1", "TRUE ", "true; x"]) {
      const out = studioDownloadHeaders(new Headers({ "X-AI-Generated": bad, "X-AI-Generator": GENERATOR }));
      expect(out, bad).toEqual({});
    }
    expect(studioDownloadHeaders(new Headers({ "X-AI-Generated": "false" }))).toEqual({ "X-AI-Generated": "false" });
  });

  it("drops a generator or model that is not printable ASCII of 1 to 200 characters", () => {
    const out = studioDownloadHeaders(new Headers({
      "X-AI-Generated": "true",
      "X-AI-Generator": "x".repeat(201),
      "X-AI-Model": "modèle",
    }));
    expect(out).toEqual({ "X-AI-Generated": "true" });
  });
});

describe("dispositionFileName", () => {
  it("reads the quoted and the unquoted form of the three download types", () => {
    expect(dispositionFileName("attachment; filename=tour_ab12_v2.docx")).toBe("tour_ab12_v2.docx");
    expect(dispositionFileName('attachment; filename="lotus-lane.html"')).toBe("lotus-lane.html");
    expect(dispositionFileName('Attachment;filename="notes.txt"')).toBe("notes.txt");
  });

  it("refuses anything else", () => {
    for (const bad of [
      null,
      "",
      "inline; filename=a.docx",
      "attachment; filename=../a.docx",
      'attachment; filename="a b.docx"',
      "attachment; filename=a.exe",
      "attachment; filename=.docx",
      "attachment; filename=a.docx; filename*=UTF-8''x.exe",
      `attachment; filename=${"a".repeat(160)}.docx`,
    ]) {
      expect(dispositionFileName(bad), String(bad)).toBeNull();
    }
  });
});

// ── the tenant proxy end to end, with a fake CIS ──

function studioToken(): string {
  const b = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b({ alg: "HS256" })}.${b({ sub: "t", amr: "partner", exp: Math.floor(Date.now() / 1000) + 900 })}.sig`;
}

function session(): string {
  const now = Math.floor(Date.now() / 1000);
  const c = sessionCookie(cfg, {
    at: fakeAccessToken({ sub: "sub-1" }), atExp: now + 1200, iss: "i", sub: "sub-1", cid: "c", tid: TENANT,
  });
  return `${c.name}=${c.value}`;
}

function req(path: string): NextRequest {
  return new NextRequest(`${ORIGIN}${path}`, { headers: { host: "partner.example.test", cookie: session() } });
}

const MARKED_HTML =
  `<article data-ai-generated="true" data-ai-generator="${GENERATOR}" data-ai-model="${MODEL}">\n<h1>Lantern Bay</h1>\n</article>` +
  `\n<meta name="ai-generated" content="true"><meta name="ai-generator" content="${GENERATOR}">`;

function fakeCis(downloads: Record<string, () => Response>) {
  return vi.fn(async (url: string) => {
    if (url.endsWith("/auth/tenant-session")) {
      return Response.json({
        token: studioToken(), tenant_id: TENANT, tenant_name: "Harbour Lantern Tours", plan_tier: "growth",
        content_studio_role: "owner", expires_in: 900,
      });
    }
    if (url.endsWith("/auth/verify-tenant")) {
      return Response.json({ tenant_id: TENANT, name: "Harbour Lantern Tours", plan_tier: "growth" });
    }
    const key = Object.keys(downloads).find((k) => url.startsWith(`${CIS}${k}`));
    return key ? downloads[key]() : Response.json({ ok: true });
  });
}

describe("tenant proxy keeps the AI mark of a download", () => {
  it("piece HTML export: X-AI-* headers pass, the marked body streams through unchanged", async () => {
    vi.stubGlobal("fetch", fakeCis({
      [`/v1/content-writing/pieces/${PIECE}/export`]: () => new Response(MARKED_HTML, {
        headers: {
          "Content-Type": "text/html; charset=utf-8", "X-AI-Generated": "true", "X-AI-Generator": GENERATOR,
          "X-AI-Model": MODEL, "Set-Cookie": "upstream=1",
        },
      }),
    }));
    const res = await tenantProxyGet(
      req(`/api/tenant/v1/content-writing/pieces/${PIECE}/export?format=html&mode=fragment`),
      { params: Promise.resolve({ path: ["v1", "content-writing", "pieces", PIECE, "export"] }) },
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("x-ai-generated")).toBe("true");
    expect(res.headers.get("x-ai-generator")).toBe(GENERATOR);
    expect(res.headers.get("x-ai-model")).toBe(MODEL);
    expect(res.headers.get("content-type")).toBe("text/html; charset=utf-8");
    expect(res.headers.getSetCookie().join("\n")).not.toContain("upstream=");
    const body = await res.text();
    expect(body).toBe(MARKED_HTML);
    expect(body).toContain('data-ai-generated="true"');
    expect(body).toContain('<meta name="ai-generated" content="true">');
  });

  it("piece text export: the headers are the only mark and they pass", async () => {
    vi.stubGlobal("fetch", fakeCis({
      [`/v1/content-writing/pieces/${PIECE}/export`]: () => new Response("Lantern Bay at dawn.", {
        headers: { "Content-Type": "text/plain", "X-AI-Generated": "true", "X-AI-Generator": GENERATOR },
      }),
    }));
    const res = await tenantProxyGet(
      req(`/api/tenant/v1/content-writing/pieces/${PIECE}/export?format=text`),
      { params: Promise.resolve({ path: ["v1", "content-writing", "pieces", PIECE, "export"] }) },
    );
    expect(res.headers.get("x-ai-generated")).toBe("true");
    expect(res.headers.get("x-ai-generator")).toBe(GENERATOR);
    expect(res.headers.get("x-ai-model")).toBeNull();
  });

  it("T2 DOCX: the file name passes (quoted) and the bytes are the upstream bytes", async () => {
    const bytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1, 2, 3]);
    vi.stubGlobal("fetch", fakeCis({
      [`/v1/tours/versions/${VERSION}/export-docx`]: () => new Response(bytes, {
        headers: {
          "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "Content-Disposition": "attachment; filename=tour_1a2b3c4d_v3.docx",
        },
      }),
    }));
    const res = await tenantProxyGet(
      req(`/api/tenant/v1/tours/versions/${VERSION}/export-docx`),
      { params: Promise.resolve({ path: ["v1", "tours", "versions", VERSION, "export-docx"] }) },
    );
    expect(res.status).toBe(200);
    const cd = res.headers.get("content-disposition");
    expect(cd).toBe('attachment; filename="tour_1a2b3c4d_v3.docx"');
    expect(dispositionFileName(cd)).toBe("tour_1a2b3c4d_v3.docx");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(bytes);
  });

  it("a JSON read gains no download header", async () => {
    vi.stubGlobal("fetch", fakeCis({}));
    const res = await tenantProxyGet(req("/api/tenant/v1/quota"), { params: Promise.resolve({ path: ["v1", "quota"] }) });
    expect(res.status).toBe(200);
    for (const name of [...AI_MARK_HEADERS, "Content-Disposition"]) expect(res.headers.get(name), name).toBeNull();
  });
});
