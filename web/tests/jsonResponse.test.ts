// S294 hardening: every JSON answer of the CRM web names its charset (lib/jsonResponse.ts). The last test is the guard:
// a new NextResponse.json / Response.json in app/ or lib/ that skips jsonInit, or a hand-built response with a bare
// "application/json" content type, turns this file red.
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { GET as healthz } from "@/app/api/healthz/route";
import { JSON_CONTENT_TYPE, jsonInit } from "@/lib/jsonResponse";
import { json } from "@/lib/respond";
import { unauthorizedStudio } from "@/lib/studio/auth-server";
import { refuseForeignWrite } from "@/lib/writeGuard";

const WEB = path.resolve(__dirname, "..");

describe("jsonInit", () => {
  it("sets the JSON content type with its charset and keeps the status and other headers", () => {
    expect(JSON_CONTENT_TYPE).toBe("application/json; charset=utf-8");
    const init = jsonInit({ status: 403, headers: { "Cache-Control": "no-store", "Content-Type": "application/json" } });
    const headers = new Headers(init.headers);
    expect(init.status).toBe(403);
    expect(headers.get("content-type")).toBe(JSON_CONTENT_TYPE);
    expect(headers.get("cache-control")).toBe("no-store");
    expect(new Headers(jsonInit().headers).get("content-type")).toBe(JSON_CONTENT_TYPE);
  });

  it("a Response built with it carries the charset and the same body", async () => {
    const res = Response.json({ ok: true }, jsonInit({ status: 200 }));
    expect(res.headers.get("content-type")).toBe(JSON_CONTENT_TYPE);
    expect(await res.json()).toEqual({ ok: true });
  });
});

describe("the web's JSON answers", () => {
  it("respond.ts json() names the charset and stays no-store", async () => {
    const res = json({ detail: "Not found" }, 404);
    expect(res.status).toBe(404);
    expect(res.headers.get("content-type")).toBe(JSON_CONTENT_TYPE);
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(await res.json()).toEqual({ detail: "Not found" });
  });

  it("the cross-site refusal names the charset although the vendored guard answers a bare application/json", () => {
    const req = new NextRequest("https://partner.example.test/bff/clients", {
      method: "POST",
      headers: { origin: "https://elsewhere.example.test" },
    });
    const refused = refuseForeignWrite({ publicOrigin: "https://partner.example.test" }, req);
    expect(refused?.status).toBe(403);
    expect(refused?.headers.get("content-type")).toBe(JSON_CONTENT_TYPE);
    expect(refused?.headers.get("cache-control")).toBe("no-store");
  });

  it("the studio 401 and the liveness answer name the charset", () => {
    const unauthorized = unauthorizedStudio();
    expect(unauthorized.status).toBe(401);
    expect(unauthorized.headers.get("content-type")).toBe(JSON_CONTENT_TYPE);
    const health = healthz();
    expect(health.headers.get("content-type")).toBe(JSON_CONTENT_TYPE);
    expect(health.headers.get("cache-control")).toBe("no-store");
  });
});

/** Source files of a folder (ts/tsx), tests excluded. */
function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sources(full));
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(full);
  }
  return out;
}

/** The argument text of each `<marker>...)` call (balanced parentheses; good enough for this code base). */
function calls(text: string, marker: string): string[] {
  const out: string[] = [];
  for (let at = text.indexOf(marker); at !== -1; at = text.indexOf(marker, at + 1)) {
    // "Response.json(" must not count the "NextResponse.json(" calls twice.
    if (marker.startsWith("Response") && /[A-Za-z_$]/.test(text[at - 1] ?? "")) continue;
    let depth = 0;
    let end = at + marker.length;
    for (; end < text.length; end++) {
      const c = text[end];
      if (c === "(") depth++;
      else if (c === ")") {
        if (depth === 0) break;
        depth--;
      }
    }
    out.push(text.slice(at + marker.length, end));
  }
  return out;
}

// lib/crossSiteGuard.ts is a byte copy of AA-CIS-App (CI "Vendored CIS copies"): its one caller, lib/writeGuard.ts,
// sets the charset on its answer (tested above).
const VENDORED = new Set([path.join(WEB, "lib", "crossSiteGuard.ts")]);
const BARE_JSON = /["']content-type["']\s*:\s*["']application\/json["']/i;

describe("guard", () => {
  it("every JSON answer in app/ and lib/ goes through jsonInit, and no response is built with a bare application/json", () => {
    const files = [...sources(path.join(WEB, "app")), ...sources(path.join(WEB, "lib")), path.join(WEB, "proxy.ts")];
    const bare: string[] = [];
    for (const file of files) {
      if (VENDORED.has(file)) continue;
      const text = readFileSync(file, "utf8");
      const rel = path.relative(WEB, file);
      for (const marker of ["NextResponse.json(", "Response.json("]) {
        for (const args of calls(text, marker)) {
          if (!args.includes("jsonInit(")) bare.push(`${rel}: ${marker}${args.slice(0, 60)}`);
        }
      }
      for (const marker of ["new NextResponse(", "new Response("]) {
        for (const args of calls(text, marker)) {
          if (BARE_JSON.test(args)) bare.push(`${rel}: ${marker}${args.slice(0, 60)}`);
        }
      }
    }
    expect(bare).toEqual([]);
  });
});
