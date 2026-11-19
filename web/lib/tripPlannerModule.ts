// web/lib/tripPlannerModule.ts
// AA-1139 — the agency app's TripPlanner module (Settings > Add-ons > Trip planner, TRIP_PLANNER_PATH): the planner's
// link and custom domains, where its leads go, its theme and language, its leads with their delivery status, the AI
// sessions of the month against the plan, and the webhook signing secret (shown once). The CRM API relays to CIS
// (site settings: contract tenant.addons.v1 1.1) and to TripPlanner (leads, usage and webhook signing: contract
// lead.submitted.v1 1.1). Routes: GET /crm/v1/trip-planner and GET /crm/v1/trip-planner/leads (owner, agent);
// PUT .../lead-route, PUT .../appearance and POST .../webhook-key (owner).
// Pure helpers here; the page is app/(crm)/settings/add-ons/trip-planner, the view
// components/settings/TripPlannerModuleView.tsx.

import type { MessageKey } from "./i18n";

export const TRIP_PLANNER_PATH = "/settings/add-ons/trip-planner";

export type LeadChannel = "none" | "webhook" | "email";
export type EffectiveChannel = LeadChannel | "crm";
export type DomainStatus = "pending" | "validating" | "attaching" | "active" | "failed";
export type LeadStatus = "pending" | "retrying" | "delivered" | "rejected" | "expired";
export type PlannerLocale = "en" | "vi";

/** The kit tokens the planner's brand layer themes (CIS frontend/app/_kit/core/brandTheme.ts BRAND_TOKENS). */
export const THEME_TOKENS = ["accent", "accent-deep", "accent-soft", "accent-tint", "accent-border", "on-accent"] as const;
export type ThemeToken = (typeof THEME_TOKENS)[number];
/** The two the module edits; the brand layer derives contrast for the rest (an agency may still have set others). */
export const EDITED_TOKENS: ThemeToken[] = ["accent", "accent-deep"];

export interface TripPlannerModule {
  active: boolean;
  in_service: boolean;
  planner: { url: string; domains: string[]; launcher: boolean } | null;
  domains: { domain: string; status: DomainStatus }[];
  crm_core: boolean;
  lead_route: {
    lead_channel: LeadChannel;
    lead_webhook_url: string | null;
    lead_email: string | null;
    effective_channel: EffectiveChannel;
    updated_at: string | null;
  };
  theme: Partial<Record<ThemeToken, string>>;
  locale: PlannerLocale;
  ai_sessions_monthly: number | null;
}

export interface PlannerUsage {
  month: string;
  ai_sessions: { used: number; quota: number | null };
  leads_24h: { count: number; cap: number };
}

export interface PlannerKeyInfo {
  version: number;
  rotated_at: string | null;
  available: boolean;
}

export interface PlannerPage {
  module: TripPlannerModule;
  usage: PlannerUsage | null;
  webhook_key: PlannerKeyInfo | null;
}

export interface PlannerLead {
  lead_id: string;
  created_at: string;
  channel: "crm" | "webhook" | "email";
  status: LeadStatus;
  attempts: number;
  delivered_at: string | null;
  error: string | null;
}

export interface PlannerLeadList {
  leads: PlannerLead[];
  month: Record<"total" | LeadStatus, number>;
}

export interface RotatedKey {
  version: number;
  rotated_at: string;
  key: string;
}

/** The addresses a visitor opens: the agency's own active domains first (https, /plan), then the hosted planner. */
export function plannerLinks(module: TripPlannerModule): string[] {
  if (!module.planner) return [];
  const own = module.planner.domains.map((d) => `https://${d}/plan`);
  return [...own, module.planner.url];
}

/** Share of the month's AI sessions used, 0..100 (null when the plan has no limit). */
export function sessionsPercent(usage: PlannerUsage): number | null {
  const { used, quota } = usage.ai_sessions;
  if (quota === null) return null;
  if (quota <= 0) return 100;
  return Math.min(100, Math.round((used / quota) * 100));
}

/** The tone of the sessions tile: danger once the quota is used up (new AI sessions are refused then). No warning
 * share of its own: a threshold would be a setting (the CIS AI budget alert is the agency's warning). */
export function sessionsTone(usage: PlannerUsage): "neutral" | "danger" {
  const { used, quota } = usage.ai_sessions;
  return quota !== null && used >= quota ? "danger" : "neutral";
}

export function domainTone(status: DomainStatus): "success" | "warning" | "danger" | "info" {
  if (status === "active") return "success";
  if (status === "failed") return "danger";
  return status === "attaching" ? "info" : "warning";
}

export function leadTone(status: LeadStatus): "success" | "warning" | "danger" | "neutral" | "info" {
  if (status === "delivered") return "success";
  if (status === "retrying") return "warning";
  if (status === "rejected" || status === "expired") return "danger";
  return "info";
}

const HEX = /^#[0-9a-fA-F]{6}$/;

export function isColour(value: string): boolean {
  return HEX.test(value);
}

/** The theme sent: the edited tokens that hold a colour (lower case), the other tokens kept as they were. */
export function themeToSend(current: TripPlannerModule["theme"], edited: Partial<Record<ThemeToken, string>>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const token of THEME_TOKENS) {
    const value = EDITED_TOKENS.includes(token) ? edited[token] : current[token];
    if (value && isColour(value)) out[token] = value.toLowerCase();
  }
  return out;
}

/** A client-side check before the route is sent (CIS checks again, with its public-host rule). */
export function routeProblem(channel: LeadChannel, webhookUrl: string, email: string): MessageKey | null {
  if (channel === "webhook") {
    if (!webhookUrl.trim()) return "tp.err.lead_webhook_url_required";
    if (!webhookUrl.trim().startsWith("https://")) return "tp.err.lead_webhook_url_not_https";
  }
  if (channel === "email" && !email.trim()) return "tp.err.lead_email_required";
  return null;
}

const REASON_KEYS: Record<string, MessageKey> = {
  lead_channel_invalid: "tp.err.lead_channel_invalid",
  lead_webhook_url_required: "tp.err.lead_webhook_url_required",
  lead_webhook_url_too_long: "tp.err.lead_webhook_url_too_long",
  lead_webhook_url_not_https: "tp.err.lead_webhook_url_not_https",
  lead_webhook_url_invalid: "tp.err.lead_webhook_url_invalid",
  lead_webhook_url_not_public: "tp.err.lead_webhook_url_not_public",
  lead_email_required: "tp.err.lead_email_required",
  lead_email_invalid: "tp.err.lead_email_invalid",
  lead_email_not_public: "tp.err.lead_email_not_public",
  theme_invalid: "tp.err.theme",
  theme_token_unknown: "tp.err.theme",
  theme_colour_invalid: "tp.err.theme",
  locale_invalid: "tp.err.save",
  tripplanner_not_active: "tp.err.notActive",
  tenant_offboarded: "tp.err.closing",
  webhook_key_unavailable: "tp.err.keyUnavailable",
  role_forbidden: "tp.err.ownerOnly",
};

/** The sentence for a refused change (the API's detail: CIS's or TripPlanner's reason). */
export function refusalKey(detail: string): MessageKey {
  return REASON_KEYS[detail] ?? "tp.err.save";
}

/** The field a refusal is about (to mark that control), or null. */
export function refusalField(detail: string): "lead_webhook_url" | "lead_email" | null {
  if (detail.startsWith("lead_webhook_url")) return "lead_webhook_url";
  if (detail.startsWith("lead_email")) return "lead_email";
  return null;
}

/** The page's message when the module cannot be shown (GET /crm/v1/trip-planner answered status + detail). */
export function loadErrorKey(status: number, detail: string): MessageKey {
  if (status === 403) return "tp.err.noAccess";
  if (status === 404) return "tp.err.noAccount";
  return detail === "tripplanner_unavailable" ? "tp.err.cis" : "tp.err.load";
}
