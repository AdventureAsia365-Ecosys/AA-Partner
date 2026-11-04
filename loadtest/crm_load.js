// loadtest/crm_load.js — AA-969 (CRM-37a): the bounded load test of the AA-Partner API on Dev, 50 synthetic agencies.
//
// Never run this file by hand: loadtest/crm_loadtest.py runs it after its pre-checks, watches the job tables, the
// alarms and the API while it runs, and stops it on any abort condition (loadtest/README.md). Reads only: every
// request is a GET or the read-only client search; nothing is written to the agencies.
//
// Scenarios (one token: the load-test identity, an agent of the agencies lt-01..lt-50):
//   calm   every agency but the first, round robin, a constant CALM_RPS for the whole run (the SLO is measured here);
//   noisy  the first NOISY_AGENCIES agencies (default 2: two busy agencies at once fill the request share of the pool
//          one slot each), alternating, NOISY_RPS in total from minute 2 to one minute before the end, with up to
//          NOISY_MAX_VUS requests in flight (they should meet 429 tenant_busy / rate_limited, never push the calm
//          agencies past their SLO).
// Thresholds = the SLO of docs/operations/slo.md for the calm agencies (route groups session, agency-read, reports),
// plus their error and refusal rates; the error rate aborts the run early (abortOnFail).
import http from "k6/http";
import exec from "k6/execution";
import { Counter, Rate } from "k6/metrics";

// Hard ceilings the environment cannot raise (the runner checks the same numbers before it starts).
const HARD_MAX_RPS = 30;
const HARD_MAX_MINUTES = 10;
const MIN_MINUTES = 4;

const BASE = (__ENV.LT_BASE_URL || "").replace(/\/+$/, "");
const TOKEN = open(__ENV.LT_TOKEN_FILE).trim();
const TENANTS = JSON.parse(open(__ENV.LT_TENANTS_FILE));
const MINUTES = parseInt(__ENV.LT_DURATION_MIN || "8", 10);
const CALM_RPS = parseInt(__ENV.LT_CALM_RPS || "6", 10);
const NOISY_RPS = parseInt(__ENV.LT_NOISY_RPS || "12", 10);
const NOISY_MAX_VUS = parseInt(__ENV.LT_NOISY_MAX_VUS || "12", 10);
const NOISY_AGENCIES = parseInt(__ENV.LT_NOISY_AGENCIES || "2", 10);

function refuse(why) {
  throw new Error(`crm_load.js refused: ${why}`);
}
if (!/^https:\/\/[a-z0-9.-]*-dev\.[a-z0-9.-]+$/.test(BASE)) refuse("LT_BASE_URL must be the https Dev partner origin");
if (!Array.isArray(TENANTS) || TENANTS.length < 2 || TENANTS.length > 50) refuse("2 to 50 tenant ids");
if (!(MINUTES >= MIN_MINUTES && MINUTES <= HARD_MAX_MINUTES)) refuse(`duration ${MIN_MINUTES}-${HARD_MAX_MINUTES} min`);
if (!(CALM_RPS >= 1 && NOISY_RPS >= 0 && CALM_RPS + NOISY_RPS <= HARD_MAX_RPS)) refuse(`RPS above ${HARD_MAX_RPS}`);
if (!(NOISY_MAX_VUS >= 1 && NOISY_MAX_VUS <= 30)) refuse("noisy VUs 1-30");
if (!(NOISY_AGENCIES >= 1 && NOISY_AGENCIES <= 2)) refuse("noisy agencies 1-2");
if (TENANTS.length <= NOISY_AGENCIES) refuse("at least one calm agency besides the noisy ones");

const NOISY = TENANTS.slice(0, NOISY_AGENCIES);
const CALM = TENANTS.slice(NOISY_AGENCIES);

// Route groups of docs/operations/slo.md. Weights add up to 100 per scenario.
const CALM_MIX = [
  [30, "session", "GET", "/crm/v1/me"],
  [25, "agency-read", "GET", "/crm/v1/pipeline"],
  [15, "agency-read", "GET", "/crm/v1/tasks"],
  [15, "agency-read", "GET", "/crm/v1/travelers"],
  [15, "reports", "GET", "/crm/v1/reports/tiles"],
];
const NOISY_MIX = [
  [40, "reports", "GET", "/crm/v1/reports/summary"],
  [30, "agency-read", "GET", "/crm/v1/pipeline"],
  [30, "agency-read", "POST", "/crm/v1/travelers/search"],
];

const calmErrors = new Rate("calm_errors"); // 5xx, network errors and any non-2xx but 429 / 503 for calm agencies
const calmRefused = new Rate("calm_refused"); // 429 / 503 for calm agencies (the fair share must not hit them)
const noisyRefused = new Counter("noisy_refused"); // expected: the noisy agency meets its share

export const options = {
  discardResponseBodies: true,
  insecureSkipTLSVerify: false,
  noConnectionReuse: false,
  userAgent: "aa-crm-loadtest/1 (AA-969)",
  scenarios: {
    calm: {
      executor: "constant-arrival-rate",
      exec: "calm",
      rate: CALM_RPS,
      timeUnit: "1s",
      duration: `${MINUTES}m`,
      preAllocatedVUs: 10,
      maxVUs: 30,
      tags: { tenant_kind: "calm" },
    },
    ...(NOISY_RPS > 0
      ? {
          noisy: {
            executor: "constant-arrival-rate",
            exec: "noisy",
            rate: NOISY_RPS,
            timeUnit: "1s",
            startTime: "2m",
            duration: `${MINUTES - 3}m`,
            preAllocatedVUs: Math.min(NOISY_MAX_VUS, 10),
            maxVUs: NOISY_MAX_VUS,
            tags: { tenant_kind: "noisy" },
          },
        }
      : {}),
  },
  thresholds: {
    "http_req_duration{tenant_kind:calm,group:session}": ["p(95)<300"],
    "http_req_duration{tenant_kind:calm,group:agency-read}": ["p(95)<800"],
    "http_req_duration{tenant_kind:calm,group:reports}": ["p(95)<1500"],
    calm_errors: [{ threshold: "rate<0.01", abortOnFail: true, delayAbortEval: "1m" }],
    calm_refused: ["rate<0.005"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

function pick(mix, n) {
  let x = n % 100;
  for (const [weight, group, method, path] of mix) {
    if (x < weight) return { group, method, path };
    x -= weight;
  }
  return { group: mix[0][1], method: mix[0][2], path: mix[0][3] };
}

function call(tenant, route, kind) {
  const params = {
    headers: { Authorization: `Bearer ${TOKEN}`, "X-AA-Tenant-Id": tenant, "Content-Type": "application/json" },
    tags: { group: route.group, tenant_kind: kind, name: `${route.method} ${route.path}` },
    timeout: "15s",
    responseCallback: http.expectedStatuses(200, 429, 503),
  };
  const body = route.method === "POST" ? JSON.stringify({ q: "load", page: 1 }) : null;
  return http.request(route.method, `${BASE}${route.path}`, body, params);
}

export function calm() {
  const n = exec.scenario.iterationInTest;
  const tenant = CALM[n % CALM.length];
  const res = call(tenant, pick(CALM_MIX, Math.floor(n / CALM.length) * 37 + n), "calm");
  const refused = res.status === 429 || res.status === 503;
  calmRefused.add(refused);
  calmErrors.add(res.status === 0 || res.status >= 500 || (res.status >= 400 && !refused));
}

export function noisy() {
  const n = exec.scenario.iterationInTest;
  const res = call(NOISY[n % NOISY.length], pick(NOISY_MIX, Math.floor(n / NOISY.length) * 37 + n), "noisy");
  if (res.status === 429 || res.status === 503) noisyRefused.add(1);
}

export function handleSummary(data) {
  // The runner reads this file; no token, no tenant data in it (ids are synthetic).
  return { [__ENV.LT_SUMMARY_FILE || "loadtest-summary.json"]: JSON.stringify(data, null, 2) };
}
