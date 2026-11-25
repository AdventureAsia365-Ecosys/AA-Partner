// web/lib/calendarSync.ts
// AA-1011 part 3: the shapes of GET /calendar/connections (a member's own Google / Microsoft 365 calendar, into which
// the CRM pushes departures and tasks one way) and the pure helpers of the Calendar card on Account > Integrations.
// The OAuth consent comes back through /connect/mail/callback (the mailbox's redirect URI); a calendar state starts
// with "cal." so the callback sends the browser here. No route returns a token. Edge-safe.

export type CalendarProvider = "google" | "microsoft";
export type CalendarScope = "mine" | "agency";

export interface CalendarConnection {
  calendar_connection_id: string;
  member_id: string;
  provider: CalendarProvider;
  scope: CalendarScope;
  status: "active" | "revoked";
  address: string;
  failure_class: string | null;
  last_sync_at: string | null;
  last_ok_at: string | null;
  event_count: number;
  revoked_at: string | null;
  created_at: string | null;
}

export interface CalendarConnectionsView {
  connections: CalendarConnection[];
  providers: Record<CalendarProvider, boolean>;
  kinds: string[];
}

export const CALENDAR_PROVIDERS: readonly CalendarProvider[] = ["google", "microsoft"];
export const CALENDAR_STATE_PREFIX = "cal.";
/** Where the OAuth callback sends a calendar consent back to. */
export const CALENDAR_RETURN = "/portal/account/integrations";

export function isCalendarState(state: string | null | undefined): boolean {
  return typeof state === "string" && state.startsWith(CALENDAR_STATE_PREFIX);
}

/** The completion the callback handed over in the page's query (``calendar_code`` + ``calendar_state``), or the
 *  provider's error code, or nothing. */
export function calendarReturn(search: URLSearchParams):
  | { code: string; state: string }
  | { error: string }
  | null {
  const error = search.get("calendar_error");
  if (error) return { error: ["denied", "provider", "invalid"].includes(error) ? error : "provider" };
  const code = search.get("calendar_code");
  const state = search.get("calendar_state");
  if (!code || !isCalendarState(state)) return null;
  return { code, state: state as string };
}

export type ConnectionHealth = "syncing" | "needs_reconnect" | "disconnecting" | "waiting";

/** What the card says about one connection: disconnecting (the clean-up runs), needs a reconnect (the provider
 *  refused the token or a permission), syncing (a push succeeded) or waiting for its first push. */
export function connectionHealth(c: CalendarConnection): ConnectionHealth {
  if (c.status === "revoked") return "disconnecting";
  if (c.failure_class === "auth_revoked" || c.failure_class === "permission_missing") return "needs_reconnect";
  return c.last_ok_at ? "syncing" : "waiting";
}

export const HEALTH_TONE: Record<ConnectionHealth, "success" | "danger" | "warning" | "neutral"> = {
  syncing: "success",
  needs_reconnect: "danger",
  disconnecting: "warning",
  waiting: "neutral",
};

/** The connection of one provider, if any. */
export function connectionOf(view: CalendarConnectionsView | null, provider: CalendarProvider): CalendarConnection | null {
  return view?.connections.find((c) => c.provider === provider) ?? null;
}

/** Error codes the card words itself; anything else gets the generic line. */
export const KNOWN_ERRORS = [
  "mail_oauth_not_configured",
  "oauth_state_expired",
  "oauth_state_invalid",
  "calendar_disconnect_pending",
  "denied",
] as const;
export type KnownError = (typeof KNOWN_ERRORS)[number] | "generic";

export function errorKey(detail: string | null | undefined): KnownError {
  return (KNOWN_ERRORS as readonly string[]).includes(detail ?? "") ? (detail as KnownError) : "generic";
}
