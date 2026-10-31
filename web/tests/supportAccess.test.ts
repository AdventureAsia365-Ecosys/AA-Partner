// AA-971 (CRM-13b): AA support access — which banner shows (emergency red before support amber), the day choices,
// refusal wording, the BFF allow-list lines, the settings card for the owner (form) and another role (read only), the
// banner with [End now] for the owner only and never on an emergency session.
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  BREAKGLASS_REASONS,
  bannerState,
  dayChoices,
  reasonKey,
  supportErrorKey,
  type SupportAccess,
  type SupportSession,
} from "@/lib/supportAccess";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));
const { SupportAccessView } = await import("@/components/settings/SupportAccessView");
const { SupportBanner } = await import("@/components/support/SupportBanner");

const OFF: SupportAccess = {
  grant: null,
  active_sessions: [],
  recent_sessions: [],
  can_manage: true,
  days_min: 1,
  days_max: 7,
  days_default: 1,
};
const GRANT = {
  grant_id: "00000000-0000-4000-8000-0000000000a1",
  show_identity: false,
  starts_at: "2026-11-02T09:00:00+00:00",
  expires_at: "2026-11-03T09:00:00+00:00",
  granted_by_member_id: "00000000-0000-4000-8000-0000000000a2",
};
function session(kind: "support" | "breakglass", status: SupportSession["status"] = "active"): SupportSession {
  return {
    session_id: `00000000-0000-4000-8000-0000000000${kind === "support" ? "b1" : "b2"}`,
    kind,
    status,
    staff_ref: "aa-5b1c0d9e",
    ticket_ref: kind === "support" ? "SUP-1042" : "INC-7",
    reason_code: kind === "breakglass" ? "service_outage" : null,
    show_identity: kind === "breakglass",
    started_at: "2026-11-02T10:00:00+00:00",
    expires_at: "2026-11-02T11:00:00+00:00",
    ended_at: null,
    owner_notified: kind === "breakglass",
  };
}

describe("support access helpers", () => {
  it("shows emergency first, then a support person, then an unused window, else nothing", () => {
    expect(bannerState(null)).toBeNull();
    expect(bannerState(OFF)).toBeNull();
    expect(bannerState({ ...OFF, grant: GRANT })?.kind).toBe("support_grant");
    expect(bannerState({ ...OFF, grant: GRANT, active_sessions: [session("support")] })?.kind).toBe("support_session");
    const both = { ...OFF, grant: GRANT, active_sessions: [session("support"), session("breakglass")] };
    expect(bannerState(both)).toEqual({ kind: "breakglass", until: "2026-11-02T11:00:00+00:00", reason: "service_outage" });
  });

  it("offers 1 to 7 days and names refusals", () => {
    expect(dayChoices(OFF)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(dayChoices({ days_min: 0, days_max: 30 })).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(supportErrorKey("support_grant_active")).toBe("support.error.support_grant_active");
    expect(supportErrorKey("something_else")).toBe("support.error.generic");
    expect(reasonKey("legal_request")).toBe("support.reason.legal_request");
    expect(reasonKey("curiosity")).toBe("support.reason.other");
  });

  it("has wording for every reason, status and kind in both languages", () => {
    for (const lang of ["en", "vi"] as const) {
      const m = MESSAGES[lang] as Record<string, string>;
      for (const r of BREAKGLASS_REASONS) expect(m[`support.reason.${r}`]).toBeTruthy();
      for (const s of ["active", "ended", "revoked", "expired"]) expect(m[`support.status.${s}`]).toBeTruthy();
      expect(m["mail.senders.path.breakglass_notice"]).toBeTruthy();
    }
  });

  it("lets the browser read and change support access through the BFF, nothing else of it", () => {
    expect(matchBff("GET", "support-access")?.card).toBe("AA-971");
    expect(matchBff("POST", "support-access")?.card).toBe("AA-971");
    expect(matchBff("POST", "support-access/revoke")?.card).toBe("AA-971");
    expect(matchBff("DELETE", "support-access")).toBeNull();
    expect(matchBff("PUT", "support-access")).toBeNull();
  });
});

describe("support access UI", () => {
  const render = (view: SupportAccess) =>
    renderToString(createElement(SupportAccessView, { view, lang: "en", locale: "en-GB" }));

  it("gives the owner the form (24 hours by default, names hidden by default)", () => {
    const html = render(OFF);
    expect(html).toContain("Allow AA support");
    expect(html).toContain("1 day (24 hours)");
    expect(html).toContain("Off: AA support cannot see your data.");
    expect(html).not.toContain("End now");
  });

  it("shows another role the state only", () => {
    const html = render({ ...OFF, grant: GRANT, can_manage: false });
    expect(html).toContain("Only the agency owner can allow or end AA support access.");
    expect(html).not.toContain("Allow AA support");
    expect(html).not.toContain("End now");
  });

  it("lists the recent sessions with the staff reference, ticket and emergency reason", () => {
    const html = render({
      ...OFF,
      grant: GRANT,
      recent_sessions: [session("breakglass"), session("support", "revoked")],
    });
    expect(html).toContain("End now");
    expect(html).toContain("aa-5b1c0d9e");
    expect(html).toContain("INC-7");
    expect(html).toContain("Emergency");
    expect(html).toContain("service outage");
    expect(html).toContain("Ended by you");
  });

  it("renders the banner amber with End now for the owner, red without it for an emergency", () => {
    const amber = renderToString(
      createElement(SupportBanner, {
        state: { kind: "support_session", until: "2026-11-02T11:00:00+00:00", showIdentity: false },
        canManage: true,
        lang: "en",
        locale: "en-GB",
      }),
    );
    expect(amber).toContain('data-testid="support-banner"');
    expect(amber).toContain("End now");
    const notOwner = renderToString(
      createElement(SupportBanner, {
        state: { kind: "support_grant", until: "2026-11-03T09:00:00+00:00", showIdentity: true },
        canManage: false,
        lang: "en",
        locale: "en-GB",
      }),
    );
    expect(notOwner).not.toContain("End now");
    const red = renderToString(
      createElement(SupportBanner, {
        state: { kind: "breakglass", until: "2026-11-02T11:00:00+00:00", reason: "data_corruption" },
        canManage: true,
        lang: "en",
        locale: "en-GB",
      }),
    );
    expect(red).toContain('data-testid="support-banner-emergency"');
    expect(red).toContain("damaged data");
    expect(red).not.toContain("End now");
  });
});
