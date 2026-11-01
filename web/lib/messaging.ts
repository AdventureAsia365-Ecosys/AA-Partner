// web/lib/messaging.ts
// AA-958 (card CRM-28) — the shapes of the SMS / WhatsApp API (api/app/messaging, /crm/v1/messaging*) and the pure
// helpers Settings > Channels and the trip's WhatsApp / SMS panel share: the WhatsApp window left, the template text
// with its values, the error keys, the delivery tone.

export type TextChannel = "sms" | "whatsapp";
export type ConnectionStatus = "not_set_up" | "connected" | "needs_attention" | "disconnected";
export type DeliveryStatus = "queued" | "sending" | "sent" | "delivered" | "read" | "undelivered" | "failed";

export interface ChannelTemplate {
  channel_template_id?: string;
  content_sid: string;
  name: string;
  language: string;
  category?: string | null;
  approval?: string;
  body: string;
  variables: string[];
  synced_at?: string | null;
}

export interface MessagingConnection {
  messaging_connection_id: string;
  provider: "twilio" | "sandbox";
  account: string | null;
  sms_sender: string | null;
  whatsapp_sender: string | null;
  failure_class: string | null;
  last_checked_at: string | null;
  last_ok_at: string | null;
  last_test_at: string | null;
  last_test_result: "ok" | "failed" | null;
  last_inbound_at: string | null;
  last_status_at: string | null;
  connected_at: string | null;
  hook_url: string;
  status_callback_url: string;
}

export interface MessagingView {
  status: ConnectionStatus;
  channels: Record<TextChannel, boolean>;
  providers: ("twilio" | "sandbox")[];
  can_manage: boolean;
  connection?: MessagingConnection;
  templates?: ChannelTemplate[];
}

export interface ChannelState {
  available: boolean;
  reason: string | null;
  window_until?: string | null;
}

export interface ComposeState {
  traveler_id: string | null;
  channels: Record<TextChannel, ChannelState>;
  templates: ChannelTemplate[];
}

export interface Delivery {
  template: string | null;
  status: DeliveryStatus | null;
  error_code: string | null;
  status_at: string | null;
}

export const TEXT_CHANNELS: readonly TextChannel[] = ["whatsapp", "sms"];

export function isTextChannel(channel: string): channel is TextChannel {
  return channel === "sms" || channel === "whatsapp";
}

/** Whole hours (rounded up) left in the WhatsApp window, 0 when it is closed or never opened. */
export function windowHoursLeft(until: string | null | undefined, now: number = Date.now()): number {
  if (!until) return 0;
  const left = Date.parse(until) - now;
  return Number.isFinite(left) && left > 0 ? Math.ceil(left / 3_600_000) : 0;
}

/** The placeholders of a template body, in first-seen order ({{1}}, {{ name }}). */
export function templateVariables(t: Pick<ChannelTemplate, "body" | "variables">): string[] {
  if (t.variables.length) return t.variables;
  const out: string[] = [];
  for (const m of t.body.matchAll(/\{\{\s*([A-Za-z0-9_]{1,40})\s*\}\}/g)) if (!out.includes(m[1])) out.push(m[1]);
  return out;
}

/** The template text with the values filled; an empty value keeps its placeholder (the preview shows what is missing). */
export function renderTemplate(body: string, values: Record<string, string>): string {
  return body.replace(/\{\{\s*([A-Za-z0-9_]{1,40})\s*\}\}/g, (all, name: string) => (values[name]?.trim() ? values[name].trim() : all));
}

/** A template value WhatsApp accepts: one line, no tab, no run of five spaces, at most 1,024 characters. */
export function variableOk(value: string): boolean {
  const v = value.trim();
  return v.length > 0 && v.length <= 1024 && !/[\r\n\t]| {5,}/.test(v);
}

/** The most a message may hold per channel (the API's limits). */
export const MAX_TEXT: Record<TextChannel, number> = { sms: 1600, whatsapp: 4096 };

/** Badge tone of a delivery state (read / delivered good, failures danger, the rest in progress). */
export function deliveryTone(status: DeliveryStatus | null | undefined): "success" | "danger" | "info" | "neutral" {
  if (status === "read" || status === "delivered") return "success";
  if (status === "failed" || status === "undelivered") return "danger";
  return status ? "info" : "neutral";
}

const ERRORS = new Set([
  "template_required",
  "channel_not_connected",
  "no_client_phone",
  "client_opted_out",
  "template_not_approved",
  "template_variables_missing",
  "invalid_template_variable",
  "conversation_unmatched",
  "channel_mismatch",
  "body_required",
  "body_too_long",
  "message_id_taken",
  "traveler_archived",
  "already_dispatched",
  "provider_not_available",
  "invalid_account_sid",
  "invalid_auth_token",
  "invalid_api_key_sid",
  "invalid_api_key_secret",
  "invalid_sms_sender",
  "invalid_whatsapp_sender",
  "sender_required",
  "invalid_test_number",
  "messaging_auth_revoked",
  "messaging_vendor_rejected",
  "messaging_unreachable",
  "messaging_rate_limited",
  "messaging_hook_not_configured",
  "messaging_not_connected",
  "messaging_not_configured",
  "role_forbidden",
  "assigned_only",
  "network",
]);

export function messagingErrorKey(detail: string): string {
  return ERRORS.has(detail) ? `msg.error.${detail}` : "msg.error.other";
}

/** The failure codes a text message may carry (crm.message.failure_code) that have their own words. */
export const FAILURE_CODES = new Set([
  "window_closed",
  "channel_disconnected",
  "channel_not_configured",
  "no_client_phone",
  "template_not_approved",
  "recipient_opted_out",
  "invalid_number",
  "region_not_enabled",
  "sender_not_configured",
  "invalid_template_variables",
  "vendor_rejected",
  "retries_exhausted",
  "send_uncertain",
  "no_consent",
  "archived",
]);

export function failureKey(code: string | null | undefined): string {
  return code && FAILURE_CODES.has(code) ? `msg.failure.${code}` : "msg.failure.other";
}
