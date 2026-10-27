// web/lib/textProposal.ts — AA-951 (card CRM-22): the text proposal (A-05), the builder's Before you go day 0 and the
// message template library, the shapes the API answers and the small helpers the pages share. The API renders every
// text (EN / VI e-mail and WhatsApp from one snapshot, with the version's price hash); the page never builds one.
import type { MessageKey } from "./i18n";

export type TextFormat = "email" | "whatsapp";
export type TextLang = "en" | "vi";
export const TEXT_FORMATS: readonly TextFormat[] = ["email", "whatsapp"];
export const TEXT_LANGS: readonly TextLang[] = ["en", "vi"];

/** GET /proposals/{id}/text */
export interface TextPreview {
  proposal_id: string;
  trip_id: string;
  version: number;
  kind: "text" | "builder";
  status: string;
  format: TextFormat;
  locale: TextLang;
  subject: string | null;
  body: string;
  price_hash: string;
  reference: string;
  unfilled: string[];
  link: "added_on_send" | "client_has_it";
}

/** POST /proposals/{id}/text/send */
export interface TextSent {
  proposal_id: string;
  status: string;
  format: TextFormat;
  locale: TextLang;
  price_hash: string;
  reference: string;
  link_path: string | null;
  link_in_text: boolean;
  version_sent: boolean;
  /** A sent version's link re-issued into this text (the request asked for new_link). */
  link_reissued: boolean;
  superseded: string | null;
  subject: string | null;
  body: string;
  message?: { message_id: string; status: string; dispatch_id?: string; undo_until?: string };
}

/** The builder view's day 0 (and GET /proposals/{id}/before-you-go). */
export interface BeforeYouGo {
  aa_items: number;
  countries: string[];
  fitness_warning: boolean;
  insurance_reminder: boolean;
  selected: { group_key: string; title: string; content_version: number; locales: string[]; suggested: boolean }[];
  suggested: { group_key: string; title: string; countries: string[]; locales: string[]; selected: boolean }[];
  packs_not_chosen: boolean;
}

export interface MessageTemplate {
  message_template_id: string;
  locale: TextLang;
  title: string;
  body: string;
  source: "agency" | "aa_starter";
  aa_group_key: string | null;
  aa_version: number | null;
  updated_at: string | null;
}

export interface TemplateLibrary {
  templates: MessageTemplate[];
  aa_starters: { group_key: string; locale: TextLang; title: string; version: number }[];
}

/** The version kinds and statuses a text renders from (the API answers 409 for the others). */
export function canRenderText(kind: string, status: string): boolean {
  if (kind === "builder") return ["published", "sent", "accepted"].includes(status);
  return ["draft", "sent", "accepted"].includes(status);
}

/** The order of picks after a checkbox toggles: the existing order kept, a new pick last. */
export function togglePack(current: string[], groupKey: string, on: boolean): string[] {
  const rest = current.filter((k) => k !== groupKey);
  return on ? [...rest, groupKey] : rest;
}

/** The picks to save when the agent takes every suggestion (the ones already picked first, in their order). */
export function withSuggestions(byg: BeforeYouGo): string[] {
  const picked = byg.selected.map((s) => s.group_key);
  return [...picked, ...byg.suggested.map((s) => s.group_key).filter((k) => !picked.includes(k))];
}

/** After the agent undid an e-mail: when that e-mail was the only one carrying the client's link (the send made the
 * link, the version stayed sent), the next send must ask for a new link, or the client never gets one. */
export function linkLostOnUndo(sent: TextSent): boolean {
  return sent.format === "email" && sent.link_in_text;
}

/** The note under the preview: the link added on send, a new link to come (after an undone e-mail that carried the
 * only link), or none (the client has it). */
export function linkNoteKey(preview: Pick<TextPreview, "link">, relink: boolean): MessageKey {
  if (relink) return "text.linkRelink";
  return preview.link === "added_on_send" ? "text.linkOnSend" : "text.linkClientHas";
}

/** The send request body. ``relink``: re-issue the version's link into this text. */
export function sendBody(input: {
  format: TextFormat;
  locale: TextLang;
  priceHash: string;
  templateId: string;
  messageId: string;
  relink: boolean;
}): Record<string, unknown> {
  const body: Record<string, unknown> = { format: input.format, locale: input.locale, price_hash: input.priceHash };
  if (input.templateId) body.template_id = input.templateId;
  if (input.format === "email") body.message_id = input.messageId;
  if (input.relink) body.new_link = true;
  return body;
}

/** A WhatsApp share link with the text only (no phone number: the agent picks the chat). */
export function whatsappShareUrl(text: string): string {
  return `https://wa.me/?text=${encodeURIComponent(text)}`;
}

/** The message key of an API refusal on these pages. */
export function textErrorKey(detail: string | undefined): MessageKey {
  const known: Record<string, MessageKey> = {
    price_hash_mismatch: "text.err.price_hash_mismatch",
    proposal_not_published: "text.err.proposal_not_published",
    proposal_not_sendable: "text.err.proposal_not_sendable",
    mailbox_not_connected: "text.err.mailbox_not_connected",
    trip_not_in_proposal: "text.err.trip_not_in_proposal",
    no_lead_traveller: "text.err.no_lead_traveller",
    terms_not_published: "text.err.terms_not_published",
    role_forbidden: "text.err.role_forbidden",
    no_aa_items: "text.err.no_aa_items",
    pack_not_found: "text.err.pack_not_found",
    template_not_found: "text.err.template_not_found",
  };
  return (detail && known[detail]) || "text.err.generic";
}
