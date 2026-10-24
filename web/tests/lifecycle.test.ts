// AA-942 (CRM-14): Settings > Data and privacy helpers (rule windows, counts, the typed erase confirmation, the
// offboarding step, the trip delete refusal), the BFF allow-list lines, and the EN/VI strings the page needs.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES, type MessageKey } from "@/lib/i18n";
import {
  confirmsErase,
  deleteRefusal,
  type Offboarding,
  offboardingStep,
  type Rule,
  ruleWindow,
  totalCount,
} from "@/lib/lifecycle";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const SETTINGS = { trash_days: 7, auto_archive_months: 36 };

function rule(over: Partial<Rule>): Rule {
  return { code: "trash", window: "setting", amount: null, mechanism: "trash_purge", enforced_by: "crm_lifecycle", setting: "trash_days", ...over };
}

function off(over: Partial<Offboarding>): Offboarding {
  return {
    request_id: ID,
    status: "requested",
    dry_run_counts: {},
    export_waived: false,
    export_ready: false,
    export_expires_at: null,
    export_downloaded_at: null,
    executed_at: null,
    backup_purge_by: null,
    failure_class: null,
    created_at: "2026-10-24T00:00:00+00:00",
    ...over,
  };
}

describe("rule windows", () => {
  it("words the agency's own setting, fixed windows and the open-ended ones", () => {
    expect(ruleWindow(rule({}), SETTINGS, "en")).toBe("7 days (your setting)");
    expect(ruleWindow(rule({ setting: "auto_archive_months", code: "traveler_inactive" }), { ...SETTINGS, auto_archive_months: 24 }, "en")).toBe(
      "Archived after 24 months (your setting)",
    );
    expect(ruleWindow(rule({ window: "days", amount: 90, setting: null }), SETTINGS, "vi")).toBe("90 ngày");
    expect(ruleWindow(rule({ window: "months", amount: 12, setting: null }), SETTINGS, "en")).toBe("12 months");
    expect(ruleWindow(rule({ window: "until_deleted", setting: null }), SETTINGS, "en")).toBe("Until you delete it or leave");
    expect(ruleWindow(rule({ window: "with_parent", setting: null }), SETTINGS, "en")).toBe("With the record it belongs to");
  });

  it("has EN and VI words for every rule and mechanism the API returns", () => {
    const codes = [
      "traveler_pii", "traveler_inactive", "trips", "trip_stage_history", "documents", "consent", "trash",
      "form_submissions", "llm_calls", "audit_in_app", "audit_archive", "placeholder_map", "job_rows", "export_files",
      "erased_in_backups",
    ];
    for (const lang of ["en", "vi"] as const) {
      for (const c of codes) expect(MESSAGES[lang][`data.rule.${c}` as MessageKey], c).toBeTruthy();
      for (const k of ["litigation", "regulator_request", "dispute", "insurance_claim", "other"]) {
        expect(MESSAGES[lang][`data.hold.reason.${k}` as MessageKey], k).toBeTruthy();
      }
      for (const k of ["trip", "traveler", "task", "note", "document"]) {
        expect(MESSAGES[lang][`data.trash.kind.${k}` as MessageKey], k).toBeTruthy();
      }
    }
  });
});

describe("offboarding", () => {
  it("counts the rows to remove, not the files or unlinked references", () => {
    expect(totalCount({ traveler: 2, trip: 1, document_object: 4, form_submission_unlinked: 3 })).toBe(3);
    expect(totalCount({})).toBe(0);
  });

  it("confirms only with the agency's own id", () => {
    expect(confirmsErase(` ${ID.toUpperCase()} `, ID)).toBe(true);
    expect(confirmsErase("delete", ID)).toBe(false);
    expect(confirmsErase("", "")).toBe(false);
  });

  it("walks request -> export -> confirm -> running -> done", () => {
    expect(offboardingStep(null)).toBe("request");
    expect(offboardingStep(off({ status: "cancelled" }))).toBe("request");
    expect(offboardingStep(off({}))).toBe("wait_export");
    expect(offboardingStep(off({ export_waived: true }))).toBe("confirm");
    expect(offboardingStep(off({ status: "export_ready", export_ready: true }))).toBe("confirm");
    expect(offboardingStep(off({ status: "erasing" }))).toBe("running");
    expect(offboardingStep(off({ status: "failed" }))).toBe("running");
    expect(offboardingStep(off({ status: "erased" }))).toBe("done");
  });
});

describe("trip delete refusal", () => {
  it("names the blocker and points to Cancel trip; anything else is the generic error", () => {
    expect(deleteRefusal("has_money")).toBe("trip.blocked.has_money");
    expect(MESSAGES.en[deleteRefusal("has_money")]).toContain("Cancel the trip");
    expect(MESSAGES.vi[deleteRefusal("has_aa_line")]).toContain("huỷ chuyến");
    expect(deleteRefusal("not_your_trip")).toBe("trip.error");
  });
});

describe("BFF allow-list", () => {
  it("forwards the lifecycle routes with the agency and nothing more", () => {
    const allowed: [string, string][] = [
      ["GET", "lifecycle"],
      ["PATCH", "lifecycle/settings"],
      ["POST", "lifecycle/holds"],
      ["POST", `lifecycle/holds/${ID}/release`],
      ["GET", "trash"],
      ["POST", `trash/trip/${ID}/restore`],
      ["POST", `trash/document/${ID}/restore`],
      ["DELETE", `trips/${ID}`],
      ["DELETE", `travelers/${ID}`],
      ["DELETE", `tasks/${ID}`],
      ["DELETE", `notes/${ID}`],
      ["POST", `travelers/${ID}/archive`],
      ["POST", `travelers/${ID}/unarchive`],
      ["POST", `travelers/${ID}/erasure`],
      ["GET", "lifecycle/offboarding"],
      ["POST", "lifecycle/offboarding"],
      ["POST", `lifecycle/offboarding/${ID}/cancel`],
      ["POST", `lifecycle/offboarding/${ID}/erase`],
      ["GET", `lifecycle/offboarding/${ID}/export`],
    ];
    for (const [method, path] of allowed) {
      const route = matchBff(method, path);
      expect(route?.tenant, `${method} ${path}`).toBe(true);
    }
    for (const [method, path] of [
      ["POST", `trash/audit_event/${ID}/restore`],
      ["DELETE", `trash/trip/${ID}`],
      ["DELETE", "lifecycle/holds"],
      ["POST", `lifecycle/offboarding/${ID}/download`],
      ["DELETE", `travelers/${ID}/erasure`],
      ["PATCH", "lifecycle"],
    ] as const) {
      expect(matchBff(method, path), `${method} ${path}`).toBeNull();
    }
  });
});
