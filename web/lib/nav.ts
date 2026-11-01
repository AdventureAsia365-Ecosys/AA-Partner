// web/lib/nav.ts
// AA-935 — the agency information architecture (design v3 §2.2): HOME, CRM, MONEY, GROW, CONTENT STUDIO, Settings,
// Help. Every section exists now as a page with its empty state; the card that fills it is noted in a comment on
// its line (never rendered: no issue ID reaches a page).
//
// Role visibility follows design v3 §2.2 / §4.6: Commissions is hidden for the assistant, Marketing is owner / agent
// only. Hiding is a convenience; the API enforces the matrix (AA-939). Edge-safe.

import type { MessageKey } from "./i18n";

export type Role = "owner" | "agent" | "assistant";
export type Group = "home" | "crm" | "money" | "grow" | "studio" | "settings";
export type IconName =
  | "home"
  | "kanban"
  | "users"
  | "inbox"
  | "check"
  | "plane"
  | "calendar"
  | "wallet"
  | "ticket"
  | "percent"
  | "chart"
  | "megaphone"
  | "truck"
  | "book"
  | "zap"
  | "studio"
  | "settings"
  | "help";

export interface Section {
  key: string;
  path: string;
  group: Group;
  label: MessageKey;
  icon: IconName;
  roles: readonly Role[];
  empty?: { title: MessageKey; body: MessageKey };
}

const ALL: readonly Role[] = ["owner", "agent", "assistant"];
const NO_ASSISTANT: readonly Role[] = ["owner", "agent"];

export const SECTIONS: readonly Section[] = [
  { key: "home", path: "/", group: "home", label: "nav.home", icon: "home", roles: ALL }, // AA-935
  { key: "pipeline", path: "/pipeline", group: "crm", label: "nav.pipeline", icon: "kanban", roles: ALL, empty: { title: "section.pipeline.title", body: "section.pipeline.body" } }, // AA-936
  { key: "clients", path: "/clients", group: "crm", label: "nav.clients", icon: "users", roles: ALL, empty: { title: "section.clients.title", body: "section.clients.body" } }, // AA-937
  { key: "inbox", path: "/inbox", group: "crm", label: "nav.inbox", icon: "inbox", roles: ALL, empty: { title: "section.inbox.title", body: "section.inbox.body" } }, // AA-945
  { key: "tasks", path: "/tasks", group: "crm", label: "nav.tasks", icon: "check", roles: ALL }, // AA-959
  { key: "travelling", path: "/travelling", group: "crm", label: "nav.travelling", icon: "plane", roles: ALL }, // AA-961
  { key: "calendar", path: "/calendar", group: "crm", label: "nav.calendar", icon: "calendar", roles: ALL }, // AA-961
  { key: "payments", path: "/payments", group: "money", label: "nav.payments", icon: "wallet", roles: ALL, empty: { title: "section.payments.title", body: "section.payments.body" } }, // AA-952
  { key: "bookings", path: "/bookings", group: "money", label: "nav.bookings", icon: "ticket", roles: ALL, empty: { title: "section.bookings.title", body: "section.bookings.body" } }, // AA-944
  { key: "commissions", path: "/commissions", group: "money", label: "nav.commissions", icon: "percent", roles: NO_ASSISTANT, empty: { title: "section.commissions.title", body: "section.commissions.body" } }, // AA-957
  { key: "reports", path: "/reports", group: "grow", label: "nav.reports", icon: "chart", roles: ALL }, // AA-966
  { key: "marketing", path: "/marketing", group: "grow", label: "nav.marketing", icon: "megaphone", roles: NO_ASSISTANT, empty: { title: "section.marketing.title", body: "section.marketing.body" } }, // AA-968
  { key: "suppliers", path: "/suppliers", group: "grow", label: "nav.suppliers", icon: "truck", roles: ALL, empty: { title: "section.suppliers.title", body: "section.suppliers.body" } }, // AA-944
  { key: "library", path: "/library", group: "grow", label: "nav.library", icon: "book", roles: ALL, empty: { title: "section.library.title", body: "section.library.body" } }, // AA-963
  { key: "automations", path: "/automations", group: "grow", label: "nav.automations", icon: "zap", roles: ALL }, // AA-959
  { key: "settings", path: "/settings", group: "settings", label: "nav.settings", icon: "settings", roles: ALL }, // AA-935
  { key: "help", path: "/help", group: "settings", label: "nav.help", icon: "help", roles: ALL, empty: { title: "section.help.title", body: "section.help.body" } }, // AA-970
];

export const GROUP_LABEL: Partial<Record<Group, MessageKey>> = {
  crm: "nav.group.crm",
  money: "nav.group.money",
  grow: "nav.group.grow",
  studio: "nav.group.studio",
};

export const GROUP_ORDER: readonly Group[] = ["home", "crm", "money", "grow", "studio", "settings"];

export function isRole(value: unknown): value is Role {
  return value === "owner" || value === "agent" || value === "assistant";
}

export function visibleSections(role: Role): Section[] {
  return SECTIONS.filter((s) => s.roles.includes(role));
}

/** The section of a top-level path segment ("pipeline"), or null for an unknown one. */
export function sectionByKey(key: string): Section | null {
  return SECTIONS.find((s) => s.key === key && s.key !== "home") ?? null;
}

export function activeSection(pathname: string): Section | null {
  if (pathname === "/") return SECTIONS[0];
  const first = pathname.split("/")[1] ?? "";
  return sectionByKey(first);
}

export const CONTENT_STUDIO_PATH = "/portal/dashboard";

/**
 * The Content studio link (cross-link, design v3 Fix C10): the portal host's dashboard, or null when not set.
 * AA-979 P2b: on the agency host itself (`publicOrigin` = CONTENT_STUDIO_URL's origin) it is the same-origin path, one
 * host and one sign-in; from partner-dev. it stays the absolute portal. URL.
 */
export function contentStudioHref(contentStudioUrl: string, publicOrigin = ""): string | null {
  if (!contentStudioUrl) return null;
  if (publicOrigin && new URL(contentStudioUrl).origin === publicOrigin) return CONTENT_STUDIO_PATH;
  return `${contentStudioUrl}${CONTENT_STUDIO_PATH}`;
}
