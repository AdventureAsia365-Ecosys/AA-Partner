// AA-941 (CRM-13a): what an agency sees of the L1 console. Settings > Data and privacy shows an export or deletion
// request AA raised with Confirm / Decline (EN by default, VI strings present); a locked agency shows "This agency is
// locked by AA" on the chooser (403 tenant_locked, or the membership listed as locked).
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { lockedNotice, type Membership } from "@/lib/crmApi";
import { MESSAGES, type MessageKey } from "@/lib/i18n";
import { aaRequestKey, type LifecycleView, type Offboarding, offboardingStep } from "@/lib/lifecycle";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));
const { DataLifecycleView } = await import("@/components/settings/DataLifecycleView");

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const TENANT = "6c950969-ae80-4258-ab68-6ed80f2bbe71";

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
    purpose: "offboard",
    raised_by: "aa",
    awaiting_owner: true,
    ...over,
  };
}

function view(offboarding: Offboarding | null): LifecycleView {
  return {
    rules: [],
    settings: { trash_days: 7, auto_archive_months: 36 },
    lost_reasons: [],
    archive_reasons: [],
    hold_reasons: ["other"],
    deletion_dry_run: false,
    backup_purge_days: 30,
    role: "owner",
    holds: [],
    runs: [],
    archived_inactive: [],
    offboarding,
  };
}

function render(offboarding: Offboarding | null, lang: "en" | "vi" = "en"): string {
  return renderToString(
    createElement(DataLifecycleView, { view: view(offboarding), trash: [], tenantId: TENANT, lang, locale: "en-AU" }),
  );
}

describe("an AA-raised data request", () => {
  it("waits for the owner's Confirm / Decline, in English by default", () => {
    const html = render(off({}));
    expect(html).toContain('data-testid="data-aa-request"');
    expect(html).toContain("AA raised an agency deletion request");
    expect(html).toContain(">Confirm<");
    expect(html).toContain(">Decline<");
    expect(html).not.toContain("Will remove");
    expect(render(off({ purpose: "export_only" }))).toContain("AA raised an agency export request");
  });

  it("speaks Vietnamese when the person picked it", () => {
    const html = render(off({}), "vi");
    expect(html).toContain("AA đã tạo yêu cầu xoá đại lý");
    expect(html).toContain(">Xác nhận<");
    expect(html).toContain(">Từ chối<");
  });

  it("steps: an AA export never offers the erase; an owner request is unchanged", () => {
    expect(offboardingStep(off({}))).toBe("aa_request");
    expect(offboardingStep(off({ awaiting_owner: false, purpose: "export_only" }))).toBe("wait_export");
    expect(offboardingStep(off({ awaiting_owner: false, purpose: "export_only", status: "export_ready" }))).toBe(
      "export_ready",
    );
    expect(offboardingStep(off({ awaiting_owner: false, status: "export_ready" }))).toBe("confirm");
    expect(offboardingStep(off({ awaiting_owner: undefined, purpose: undefined, raised_by: undefined }))).toBe(
      "wait_export",
    );
    const ready = render(off({ awaiting_owner: false, purpose: "export_only", status: "export_ready", export_ready: true }));
    expect(ready).toContain("Download export");
    expect(ready).not.toContain('data-testid="data-erase-confirm"');
    expect(aaRequestKey(off({ purpose: "export_only" }))).toBe("data.aa.export");
  });

  it("is allowed through the BFF for the owner's confirmation only", () => {
    expect(matchBff("POST", `lifecycle/offboarding/${ID}/accept`)?.card).toBe("AA-941");
    expect(matchBff("GET", `lifecycle/offboarding/${ID}/accept`)).toBeNull();
  });

  it("has every new string in EN and VI", () => {
    const keys = Object.keys(MESSAGES.en).filter((k) => k.startsWith("data.aa.") || k.startsWith("locked.")) as MessageKey[];
    expect(keys.length).toBeGreaterThanOrEqual(9);
    for (const k of keys) {
      expect(MESSAGES.vi[k], k).toBeTruthy();
      expect(MESSAGES.vi[k], k).not.toBe(MESSAGES.en[k]);
    }
  });
});

describe("a locked agency", () => {
  const member = (tenant_id: string, tenant_status: Membership["tenant_status"]): Membership => ({
    tenant_id,
    member_id: "m1",
    member_role: "owner",
    display_name: "Sen Vàng",
    default_locale: "vi-VN",
    tenant_status,
  });

  it("shows the notice after a 403 tenant_locked or when the open agency is listed as locked", () => {
    expect(lockedNotice([], undefined, "1")).toBe(true);
    expect(lockedNotice([member(TENANT, "locked")], TENANT)).toBe(true);
    expect(lockedNotice([member(TENANT, "active")], TENANT)).toBe(false);
    expect(lockedNotice([member(TENANT, "locked")], undefined)).toBe(false);
    expect(lockedNotice([member(ID, "locked")], TENANT)).toBe(false);
    expect(MESSAGES.en["locked.title"]).toBe("This agency is locked by AA");
  });
});
