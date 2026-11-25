// AA-1011 part 3: the Calendar card's helpers, the OAuth callback that routes a calendar consent back to Account >
// Integrations (the mailbox keeps its own return), and the (server-only) allow-list lines of the calendar connection routes.
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { GET as mailCallback } from "@/app/connect/mail/callback/route";
import { matchBff, SERVER_ONLY_ROUTES } from "@/lib/bff";
import {
  calendarReturn,
  connectionHealth,
  connectionOf,
  errorKey,
  isCalendarState,
  type CalendarConnection,
  type CalendarConnectionsView,
} from "@/lib/calendarSync";
import { testConfig } from "./helpers";

const UUID = "00000000-0000-4000-8000-00000000c011";

function connection(over: Partial<CalendarConnection> = {}): CalendarConnection {
  return {
    calendar_connection_id: UUID,
    member_id: UUID,
    provider: "google",
    scope: "mine",
    status: "active",
    address: "lan@example.test",
    failure_class: null,
    last_sync_at: null,
    last_ok_at: null,
    event_count: 0,
    revoked_at: null,
    created_at: null,
    ...over,
  };
}

describe("calendar OAuth callback", () => {
  function request(query: string) {
    const headers = new Headers({ host: "partner.example.test" });
    return new NextRequest(`https://partner.example.test/connect/mail/callback${query}`, { headers });
  }

  it("sends a calendar consent back to Account > Integrations and a mailbox one to Settings > Mailbox", async () => {
    testConfig();
    const cal = await mailCallback(request("?code=abc&state=cal.def"));
    expect(cal.status).toBe(307);
    expect(cal.headers.get("location")).toBe(
      "https://partner.example.test/portal/account/integrations?calendar_code=abc&calendar_state=cal.def",
    );
    const denied = await mailCallback(request("?error=access_denied&state=cal.def&error_description=%3Cb%3E"));
    expect(denied.headers.get("location")).toBe(
      "https://partner.example.test/portal/account/integrations?calendar_error=denied",
    );
    const mail = await mailCallback(request("?code=abc&state=def"));
    expect(mail.headers.get("location")).toBe("https://partner.example.test/settings/mailbox?code=abc&state=def");
  });

  it("reads the return strictly", () => {
    expect(isCalendarState("cal.x")).toBe(true);
    expect(isCalendarState("x")).toBe(false);
    expect(isCalendarState(null)).toBe(false);
    expect(calendarReturn(new URLSearchParams("calendar_code=c&calendar_state=cal.s"))).toEqual({ code: "c", state: "cal.s" });
    expect(calendarReturn(new URLSearchParams("calendar_code=c&calendar_state=s"))).toBeNull();
    expect(calendarReturn(new URLSearchParams("calendar_error=denied"))).toEqual({ error: "denied" });
    expect(calendarReturn(new URLSearchParams("calendar_error=%3Cscript%3E"))).toEqual({ error: "provider" });
    expect(calendarReturn(new URLSearchParams(""))).toBeNull();
  });
});

describe("calendar card helpers", () => {
  it("says what each connection is doing", () => {
    expect(connectionHealth(connection())).toBe("waiting");
    expect(connectionHealth(connection({ last_ok_at: "2030-03-10T03:00:00Z" }))).toBe("syncing");
    expect(connectionHealth(connection({ failure_class: "auth_revoked" }))).toBe("needs_reconnect");
    expect(connectionHealth(connection({ failure_class: "unreachable", last_ok_at: "x" }))).toBe("syncing");
    expect(connectionHealth(connection({ status: "revoked" }))).toBe("disconnecting");
  });

  it("finds a provider's connection and words known errors only", () => {
    const view: CalendarConnectionsView = { connections: [connection()], providers: { google: true, microsoft: false }, kinds: [] };
    expect(connectionOf(view, "google")?.address).toBe("lan@example.test");
    expect(connectionOf(view, "microsoft")).toBeNull();
    expect(connectionOf(null, "google")).toBeNull();
    expect(errorKey("mail_oauth_not_configured")).toBe("mail_oauth_not_configured");
    expect(errorKey("calendar_disconnect_pending")).toBe("calendar_disconnect_pending");
    expect(errorKey("something_else")).toBe("generic");
    expect(errorKey(null)).toBe("generic");
  });
});

describe("BFF lines", () => {
  // S294: server-only (the studio's route handler app/api/studio/calendar asks them with the studio's tenant); through
  // the browser /bff a studio session got 409 no_agency_selected on page load.
  it("allows exactly the calendar connection calls, server side only", () => {
    expect(matchBff("GET", "calendar/connections", SERVER_ONLY_ROUTES)?.card).toBe("AA-1011");
    expect(matchBff("POST", "calendar/connections/oauth/start", SERVER_ONLY_ROUTES)?.tenant).toBe(true);
    expect(matchBff("POST", "calendar/connections/oauth/complete", SERVER_ONLY_ROUTES)?.card).toBe("AA-1011");
    expect(matchBff("POST", `calendar/connections/${UUID}/disconnect`, SERVER_ONLY_ROUTES)?.card).toBe("AA-1011");
    expect(matchBff("GET", "calendar/connections")).toBeNull();
    expect(matchBff("POST", `calendar/connections/${UUID}/disconnect`)).toBeNull();
    expect(matchBff("DELETE", "calendar/connections", SERVER_ONLY_ROUTES)).toBeNull();
    expect(matchBff("GET", "calendar/connections/oauth/start", SERVER_ONLY_ROUTES)).toBeNull();
    expect(matchBff("POST", "calendar/connections/not-a-uuid/disconnect", SERVER_ONLY_ROUTES)).toBeNull();
  });
});
