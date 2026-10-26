// portal/lib/sentryScrub.ts
// AA-979 P2b — what the Content studio sends to Sentry (instrumentation.ts, instrumentation-client.ts): no personal
// data and no credential. dataCollection is off for users, cookies, headers, query strings and bodies; this also drops cookies, the Authorization / Cookie headers, the
// request body and the query string (tokens and ids could ride there) and the user object. No imports (node --test).

type Dict = Record<string, unknown>;

const DROP_HEADERS = new Set(["authorization", "cookie", "set-cookie", "x-api-key", "cloudfront-viewer-address"]);

export function scrubEvent<T>(event: T): T {
  if (!event || typeof event !== "object") return event;
  const e = event as Dict;
  delete e.user;
  const request = e.request as Dict | undefined;
  if (request && typeof request === "object") {
    delete request.cookies;
    delete request.data;
    delete request.query_string;
    const headers = request.headers as Dict | undefined;
    if (headers && typeof headers === "object") {
      for (const name of Object.keys(headers)) {
        if (DROP_HEADERS.has(name.toLowerCase())) delete headers[name];
      }
    }
    if (typeof request.url === "string") request.url = request.url.split("?")[0];
  }
  return event;
}
