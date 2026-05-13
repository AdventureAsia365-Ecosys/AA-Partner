import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";

// Only these prefixes/patterns are reachable through the playground.
// Prevents the proxy from being used as an open relay.
const ALLOWED_PATHS: Array<string | RegExp> = [
  "/v1/tours",                         // list catalog + get single tour
  /^\/v1\/tours\/[\w-]+$/,             // /v1/tours/:id
  "/v1/webhooks",                      // webhook setup
];

function isAllowed(endpoint: string): boolean {
  return ALLOWED_PATHS.some(p =>
    typeof p === "string" ? endpoint.startsWith(p) : p.test(endpoint)
  );
}

export async function POST(req: NextRequest) {
  // Auth — reuse the same cookie-based pattern as /api/tenant/[...path]
  const cookieStore = await cookies();
  const token = cookieStore.get("cis_tenant_token")?.value;
  if (!token) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  let body: { endpoint: string; params?: Record<string, string>; method?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400 });
  }

  const { endpoint, params, method = "GET" } = body;

  if (!endpoint || !isAllowed(endpoint)) {
    return NextResponse.json({ error: "Endpoint not in playground scope" }, { status: 403 });
  }

  // Build upstream URL — append query params for GET requests
  const url = new URL(`${API_URL}${endpoint}`);
  if (method === "GET" && params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v && k !== "id") url.searchParams.set(k, v);
    });
  }

  const startTime = Date.now();
  try {
    const apiRes = await fetch(url.toString(), {
      method,
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      // POST/PATCH body (e.g. webhook setup)
      ...(method !== "GET" && method !== "HEAD" && params
        ? { body: JSON.stringify(params) }
        : {}),
    });

    let data: unknown;
    try { data = await apiRes.json(); } catch { data = {}; }

    return NextResponse.json({
      status: apiRes.status,
      statusText: apiRes.statusText,
      responseTime: Date.now() - startTime,
      data,
    });
  } catch {
    return NextResponse.json({
      status: 0,
      statusText: "Network Error",
      responseTime: Date.now() - startTime,
      data: { error: "Could not reach API" },
    });
  }
}
