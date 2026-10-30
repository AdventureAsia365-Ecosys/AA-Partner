// AA-967 (CRM-35, Module 18): Settings > Data and privacy, exports and the data-request log — the pure helpers, the
// BFF allow-list lines, and the EN/VI words for every code the API returns.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES, type MessageKey } from "@/lib/i18n";
import {
  addDays,
  type DataExport,
  erasedCount,
  erasureLines,
  erasureRefusal,
  exportFileName,
  formatBytes,
  progressPercent,
  shortDigest,
  statusTone,
} from "@/lib/portability";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

function exp(over: Partial<DataExport>): DataExport {
  return {
    export_id: ID,
    scope: "tenant",
    subject_id: ID,
    status: "building",
    schema_version: 1,
    progress: { done: 3, total: 12 },
    rows: 0,
    tables: 0,
    documents_included: 0,
    documents_skipped: 0,
    size_bytes: null,
    content_sha256: null,
    created_at: "2026-10-30T09:00:00+00:00",
    completed_at: null,
    expires_at: null,
    downloaded_at: null,
    download_count: 0,
    failure_class: null,
    data_request_id: null,
    ...over,
  };
}

describe("exports", () => {
  it("shows progress, size, the short checksum and the file name", () => {
    expect(progressPercent(exp({}))).toBe(25);
    expect(progressPercent(exp({ progress: { done: 0, total: 0 } }))).toBe(0);
    expect(progressPercent(exp({ status: "ready" }))).toBe(100);
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(null)).toBe("–");
    expect(shortDigest("ab".repeat(32))).toBe("abab abab abab abab");
    expect(exportFileName(exp({ scope: "traveler" }))).toBe("crm-export-traveler-0f8fad5b.zip");
    expect(statusTone("ready")).toBe("success");
    expect(statusTone("open", true)).toBe("danger");
    expect(statusTone("cancelled")).toBe("neutral");
  });
});

describe("client erasure", () => {
  it("counts what goes, names what stays and why, and maps refusals", () => {
    const counts = { traveler: 1, consent: 2, document_object: 3, signature_event_kept: 2, referrer_unlinked: 1 };
    expect(erasedCount(counts)).toBe(3);
    const lines = erasureLines(
      { traveler_id: ID, dry_run: true, counts, kept: { signature_event: 2 }, exceptions: ["signed_agreements_claim_period", "accounting_records"], exports_withdrawn: 1 },
      "en",
    );
    expect(lines[0]).toContain("3 records");
    expect(lines.some((l) => l.includes("2 signed records"))).toBe(true);
    expect(lines.some((l) => l.includes("Invoices and payments"))).toBe(true);
    expect(lines.some((l) => l.includes("1 exports"))).toBe(true);
    expect(erasureRefusal("traveler_on_trip")).toBe("data.era.refused.traveler_on_trip");
    expect(erasureRefusal("other")).toBeNull();
    expect(addDays("2026-10-01", 30)).toBe("2026-10-31");
    expect(addDays("2026-10-01", 92)).toBe("2027-01-01");
  });
});

describe("words", () => {
  it("has EN and VI words for every kind, channel, status and outcome the API returns", () => {
    const keys = [
      ...["access", "portability", "erasure", "rectification", "restriction", "objection", "other"].map((k) => `data.req.kind.${k}`),
      ...["email", "phone", "in_person", "post", "client_portal", "aa_forwarded", "other"].map((k) => `data.req.channel.${k}`),
      ...["open", "extended", "done", "refused", "withdrawn"].map((k) => `data.req.status.${k}`),
      ...["fulfilled", "partly_fulfilled", "no_data", "identity_not_verified", "exempt", "manifestly_unfounded", "withdrawn_by_subject"].map(
        (k) => `data.req.outcome.${k}`,
      ),
      ...["queued", "building", "ready", "failed", "cancelled", "expired"].map((k) => `data.exp.status.${k}`),
      ...["tenant", "traveler"].map((k) => `data.exp.scope.${k}`),
      "data.era.exception.signed_agreements_claim_period",
      "data.era.exception.accounting_records",
      "data.off.scheduled",
      "data.off.coolOff",
    ];
    for (const lang of ["en", "vi"] as const) {
      for (const k of keys) expect(MESSAGES[lang][k as MessageKey], `${lang} ${k}`).toBeTruthy();
    }
    expect(MESSAGES.en["data.exp.help"]).toContain("{minutes}");
    expect(MESSAGES.vi["data.exp.help"]).toContain("{days}");
  });
});

describe("BFF allow-list", () => {
  it("forwards the export and request routes with the agency, and nothing else under data/", () => {
    for (const [method, path] of [
      ["GET", "data/exports"],
      ["POST", "data/exports"],
      ["POST", `data/exports/${ID}/link`],
      ["GET", `data/exports/${ID}/download`],
      ["GET", "data/requests"],
      ["POST", "data/requests"],
      ["POST", `data/requests/${ID}/extend`],
      ["POST", `data/requests/${ID}/close`],
    ] as const) {
      expect(matchBff(method, path)?.tenant, `${method} ${path}`).toBe(true);
    }
    for (const [method, path] of [
      ["DELETE", `data/exports/${ID}`],
      ["POST", `data/exports/${ID}/download`],
      ["GET", "data/exports/x/download"],
      ["DELETE", `data/requests/${ID}`],
      ["PATCH", `data/requests/${ID}`],
    ] as const) {
      expect(matchBff(method, path), `${method} ${path}`).toBeNull();
    }
  });
});
