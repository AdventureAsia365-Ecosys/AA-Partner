// web/lib/nav.ts
// AA-935 — the agency information architecture (design v3 §2.2): HOME, CRM, MONEY, GROW, CONTENT STUDIO, Settings,
// Help. Every section exists now as a page with its empty state; the card that fills it is recorded here (data only,
// shown to reviewers as a data attribute, never as text an agency reads).
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
  /** Linear issue of the card that builds the page (data-card attribute). */
  card: string;
  empty?: { title: MessageKey; body: MessageKey };
}

const ALL: readonly Role[] = ["owner", "agent", "assistant"];
const NO_ASSISTANT: readonly Role[] = ["owner", "agent"];

export const SECTIONS: readonly Section[] = [
  { key: "home", path: "/", group: "home", label: "nav.home", icon: "home", roles: ALL, card: "AA-935" },
  { key: "pipeline", path: "/pipeline", group: "crm", label: "nav.pipeline", icon: "kanban", roles: ALL, card: "AA-936", empty: { title: "section.pipeline.title", body: "section.pipeline.body" } },
  { key: "clients", path: "/clients", group: "crm", label: "nav.clients", icon: "users", roles: ALL, card: "AA-936", empty: { title: "section.clients.title", body: "section.clients.body" } },
  { key: "inbox", path: "/inbox", group: "crm", label: "nav.inbox", icon: "inbox", roles: ALL, card: "AA-945", empty: { title: "section.inbox.title", body: "section.inbox.body" } },
  { key: "tasks", path: "/tasks", group: "crm", label: "nav.tasks", icon: "check", roles: ALL, card: "AA-959", empty: { title: "section.tasks.title", body: "section.tasks.body" } },
  { key: "payments", path: "/payments", group: "money", label: "nav.payments", icon: "wallet", roles: ALL, card: "AA-952", empty: { title: "section.payments.title", body: "section.payments.body" } },
  { key: "bookings", path: "/bookings", group: "money", label: "nav.bookings", icon: "ticket", roles: ALL, card: "AA-944", empty: { title: "section.bookings.title", body: "section.bookings.body" } },
  { key: "commissions", path: "/commissions", group: "money", label: "nav.commissions", icon: "percent", roles: NO_ASSISTANT, card: "AA-957", empty: { title: "section.commissions.title", body: "section.commissions.body" } },
  { key: "reports", path: "/reports", group: "grow", label: "nav.reports", icon: "chart", roles: ALL, card: "AA-966", empty: { title: "section.reports.title", body: "section.reports.body" } },
  { key: "marketing", path: "/marketing", group: "grow", label: "nav.marketing", icon: "megaphone", roles: NO_ASSISTANT, card: "AA-968", empty: { title: "section.marketing.title", body: "section.marketing.body" } },
  { key: "suppliers", path: "/suppliers", group: "grow", label: "nav.suppliers", icon: "truck", roles: ALL, card: "AA-944", empty: { title: "section.suppliers.title", body: "section.suppliers.body" } },
  { key: "library", path: "/library", group: "grow", label: "nav.library", icon: "book", roles: ALL, card: "AA-963", empty: { title: "section.library.title", body: "section.library.body" } },
  { key: "automations", path: "/automations", group: "grow", label: "nav.automations", icon: "zap", roles: ALL, card: "AA-959", empty: { title: "section.automations.title", body: "section.automations.body" } },
  { key: "settings", path: "/settings", group: "settings", label: "nav.settings", icon: "settings", roles: ALL, card: "AA-935" },
  { key: "help", path: "/help", group: "settings", label: "nav.help", icon: "help", roles: ALL, card: "AA-970", empty: { title: "section.help.title", body: "section.help.body" } },
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

/** The Content studio link (cross-link, design v3 Fix C10): the portal host's dashboard, or null when not set. */
export function contentStudioHref(contentStudioUrl: string): string | null {
  return contentStudioUrl ? `${contentStudioUrl}/portal/dashboard` : null;
}
