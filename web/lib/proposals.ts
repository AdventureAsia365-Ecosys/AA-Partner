// web/lib/proposals.ts
// AA-946 (card CRM-18) — the shapes of the thin lifecycle API (api/app/proposals, api/app/portal) and the pure
// helpers the trip's Proposal tab, Settings > Agreements and the client pages (/c/p/<token>, /c/t/<token>) share.
// Amounts are the client's sell amounts (strings with two decimals from the API); no net, cost or margin here.

import type { PortalPartyMember } from "./clientPortal";

export type ProposalStatus = "draft" | "published" | "sent" | "accepted" | "superseded" | "withdrawn";
export type ScheduleStatus = "due" | "paid" | "void";
export type PayMethod = "bank_transfer" | "cash" | "card_offline" | "other";
export const PAY_METHODS: readonly PayMethod[] = ["bank_transfer", "cash", "card_offline", "other"];
export const STAGE3_FIELDS = [
  "given_name",
  "family_name",
  "dob",
  "nationality",
  "passport_no",
  "passport_expiry",
  "passport_country",
  "emergency_contact",
] as const;
export type Stage3Field = (typeof STAGE3_FIELDS)[number];

export interface Proposal {
  proposal_id: string;
  version: number;
  status: ProposalStatus;
  body: string;
  currency: string;
  total_amount: string;
  deposit_amount: string;
  balance_due_on: string | null;
  terms_doc_id: string | null;
  price_hash: string | null;
  sent_at: string | null;
  accepted_at: string | null;
  /** AA-949: "builder" versions are built and published in the day-list builder (A-04), not in this tab's form. */
  kind?: "text" | "builder";
  published_at?: string | null;
  /** AA-950: what the client did with it (R4 viewed, the hold set, the accepted picks' amounts). */
  view_count?: number;
  last_viewed_at?: string | null;
  hold_expires_at?: string | null;
  hold_count?: number;
  accepted_total_amount?: string | null;
}

export interface ScheduleRow {
  schedule_id: string;
  proposal_id: string;
  kind: "deposit" | "balance";
  amount: string;
  currency: string;
  due_on: string;
  status: ScheduleStatus;
  paid_at: string | null;
}

export interface PaymentRow {
  payment_id: string;
  /** AA-952: null for a payment of an invoice without a schedule row (a service fee); invoice_id names its invoice. */
  schedule_id: string | null;
  invoice_id?: string | null;
  amount: string;
  currency: string;
  method: PayMethod;
  received_on: string;
}

export interface TravellerProgress {
  traveler_id: string;
  role: "lead" | "companion";
  details_done: boolean;
  missing: string[];
  waiver_signed: boolean | null;
  portal: { access_id: string; status: string; last_sign_in_at: string | null; stage3_submitted_at: string | null } | null;
}

export interface LifecycleView {
  trip_id: string;
  stage: string;
  proposals: Proposal[];
  schedule: ScheduleRow[];
  payments: PaymentRow[];
  deposit_paid: boolean;
  /** Stage 3 and signing are open: a paid deposit, or Booked with the manual_booking override (crm.portal_unlocked). */
  portal_unlocked: boolean;
  portal: { s3: number; waiver_required: boolean; travellers: TravellerProgress[] };
}

export type AgreementKind = "booking_terms" | "waiver" | "insurance_declination";
export const AGREEMENT_KINDS: readonly AgreementKind[] = ["booking_terms", "waiver", "insurance_declination"];

/** A merge field an agreement text may hold (``{{name}}``), filled in at signing (AA-955). */
export interface MergeField {
  name: string;
  description: string;
}

export interface Agreement {
  doc_id: string;
  kind: AgreementKind;
  version_label: string;
  status: "draft" | "published" | "retired";
  body_text: string;
  body_sha256: string;
  published_at: string | null;
}

/** One entry of a version's booking terms select: a published version, or the retired one the version still names. */
export interface TermsChoice {
  doc_id: string;
  version_label: string;
  /** The version is no longer the published one (retired, a draft, or removed): Publish / Send refuse it. */
  stale: boolean;
}

/**
 * S262 round 3 (AA-957 x AA-952): the terms select of a proposal version. ``terms`` is every booking terms version of
 * the agency (any status, from GET /agreements); ``pinned`` the version the proposal names. Before, the select listed
 * only published versions with ``defaultValue = pinned``: a retired pinned version had no option, so the browser
 * showed the current one while the draft still held the retired one (Publish then answered terms_not_published).
 * Now the pinned version is listed (marked stale) and selected, and ``current`` is offered as the one-click fix.
 */
export function termsChoices(
  terms: Agreement[],
  pinned: string | null | undefined,
): { options: TermsChoice[]; value: string; current: TermsChoice | null; stale: TermsChoice | null } {
  const booking = terms.filter((d) => d.kind === "booking_terms");
  const published = booking.filter((d) => d.status === "published");
  const options: TermsChoice[] = published.map((d) => ({ doc_id: d.doc_id, version_label: d.version_label, stale: false }));
  const current = options[0] ?? null;
  let stale: TermsChoice | null = null;
  if (pinned && !options.some((o) => o.doc_id === pinned)) {
    const doc = booking.find((d) => d.doc_id === pinned);
    if (booking.length === 0) {
      // The versions could not be read (GET /agreements failed): keep the named one selected, claim nothing about it.
      options.push({ doc_id: pinned, version_label: "", stale: false });
    } else {
      stale = { doc_id: pinned, version_label: doc?.version_label ?? "", stale: true };
      options.push(stale);
    }
  }
  return { options, value: pinned ?? current?.doc_id ?? "", current, stale };
}

export interface InviteResult {
  access_id: string;
  traveler_id: string;
  role: string;
  link_path: string;
  expires_at: string;
}

/** The client's proposal page (GET /crm/public/v1/p/<token>). */
export interface PublicProposal {
  agency_name: string;
  proposal_id: string;
  version: number;
  status: "sent" | "accepted";
  trip: { title: string; start_date: string | null; end_date: string | null };
  body: string;
  currency: string;
  total_amount: string;
  deposit_amount: string;
  balance_amount: string;
  balance_due_on: string | null;
  terms: { doc_id: string; version_label: string; body_text: string } | null;
  /** AA-955: newer booking terms the client must accept before the deposit (accepted proposals only). */
  terms_reaccept?: { doc_id: string; version_label: string; body_text: string; body_sha256: string } | null;
  price_hash: string;
  accepted_at: string | null;
  schedule: { kind: "deposit" | "balance"; amount: string; currency: string; due_on: string; status: ScheduleStatus }[];
  /** AA-952 slice 4 (C-04 deposit, C31): the issued invoices of an accepted version, with how and by when to pay. */
  payments?: ClientInvoice[];
}

/** One invoice in the client's pay box (api app/payments/claims.py ``client_invoices``): never a net or a margin. */
export interface ClientInvoice {
  invoice_id: string;
  number: string;
  kind: "deposit" | "balance";
  currency: string;
  total_amount: string;
  balance_amount: string;
  due_on: string | null;
  /** `terms`: the agency's newer booking terms wait for the client's re-accept (shown above the box): no details and
   * no "I have paid" until then (S261 F1; the claim answers 409 terms_reaccept_required anyway). */
  pay_window: { state: "open" | "terms" | "claimed" | "settling" | "expired" | "paid" | "closed"; until: string | null };
  /** The agency's own pay-by details, only while the window is open. */
  pay_by: { name: string; instructions: string } | null;
  can_claim: boolean;
}

/** The traveller portal home (GET /crm/public/v1/portal/<token>). */
export interface PortalHome {
  agency_name: string;
  signed_in: boolean;
  role?: "lead" | "companion";
  trip?: { title: string; start_date: string | null; end_date: string | null; stage: string };
  stage3?: { locked: boolean; fields: string[]; missing: string[]; done: boolean; submitted_at: string | null };
  waiver?: { doc_id: string; version_label: string; body_text: string; body_sha256: string; signed: boolean; signed_at: string | null } | null;
  /** AA-953 adds each member's id, name and way in (lib/clientPortal.ts PortalPartyMember). */
  party?: PortalPartyMember[];
  payments?: { kind: "deposit" | "balance"; amount: string; currency: string; due_on: string; status: ScheduleStatus }[];
  /** AA-955 (e-sign v1): the envelope documents and the envelopes this traveller signs (theirs, and a minor's as guardian). */
  agreements?: PortalAgreements | null;
}

export interface EnvelopeDoc {
  kind: AgreementKind;
  doc_id: string;
  version_label: string;
  body_text: string;
  body_sha256: string;
}

export interface PortalEnvelope {
  traveler_id: string;
  for: "self" | "minor";
  minor_number: number | null;
  signed: boolean;
  signed_at: string | null;
  signer_role: "self" | "guardian" | null;
  insurance_choice: "insured" | "declined" | null;
}

export interface PortalAgreements {
  documents: EnvelopeDoc[];
  insurance_declination: EnvelopeDoc | null;
  envelopes: PortalEnvelope[];
  guardian_signs_for_me: boolean;
}

export type InsuranceChoice = "insured" | "declined";

/** The body of POST /portal/<token>/sign: every document of the envelope exactly as shown (the declination only when
 * the traveller declines insurance), the insurance choice when the agency asks it, and the minor signed for. */
export function signBody(
  box: PortalAgreements,
  envelope: PortalEnvelope,
  input: { code: string; fullName: string; agree: boolean; insurance: InsuranceChoice | null },
): Record<string, unknown> {
  const docs = [...box.documents];
  if (input.insurance === "declined" && box.insurance_declination) docs.push(box.insurance_declination);
  const body: Record<string, unknown> = {
    code: input.code.trim(),
    full_name: input.fullName,
    agree: input.agree,
    documents: docs.map((d) => ({ doc_id: d.doc_id, doc_sha256: d.body_sha256 })),
  };
  if (box.insurance_declination) body.insurance = input.insurance;
  if (envelope.for === "minor") body.traveler_id = envelope.traveler_id;
  return body;
}

/** The trip's signed documents (GET /crm/v1/trips/<id>/agreements). */
export interface TripAgreements {
  trip_id: string;
  waiver_required: boolean;
  all_signed: boolean;
  travellers: {
    traveler_id: string;
    role: string;
    waiver_signed: boolean | null;
    signature: {
      request_id: string;
      signed_at: string;
      signer_role: "self" | "guardian";
      signer_traveler_id: string | null;
      insurance_choice: InsuranceChoice | null;
      envelope_sha256: string | null;
      seal_alg: string | null;
      with_terms: boolean;
      with_declination: boolean;
    } | null;
  }[];
  terms: {
    published: { doc_id: string; version_label: string } | null;
    reaccept_required: boolean;
    acceptances: { acceptance_id: string; source: "proposal" | "reaccept" | "envelope"; version_label: string; accepted_at: string }[];
  };
  events: { traveler_id: string; kind: "code_sent" | "code_refused" | "signed"; reason: string | null; occurred_at: string }[];
}

/** POST /crm/v1/signatures/<id>/envelope */
export interface SealedEnvelope {
  request_id: string;
  envelope_text: string;
  envelope_sha256: string;
  signed_at: string;
  seal: { alg: string; key_ref: string; value_b64: string; verified: boolean | null };
}

/** The signed link token of a client page (43 characters, URL-safe base64). */
export const CLIENT_TOKEN = /^[A-Za-z0-9_-]{43}$/;

const ERROR_CODES = new Set([
  "invoice_issued_for_row",
  "trip_not_open_for_proposal",
  "proposal_already_accepted",
  "proposal_not_draft",
  "proposal_not_open",
  "trip_not_in_proposal",
  "no_lead_traveller",
  "terms_not_published",
  "terms_not_found",
  "deposit_above_total",
  "balance_due_on_required",
  "balance_due_on_not_allowed",
  "invalid_currency",
  "invalid_amount",
  "body_required",
  "schedule_not_due",
  "deposit_not_voidable",
  "proposal_not_accepted",
  "received_in_future",
  "trip_closed",
  "trip_cancelled",
  "traveler_not_on_trip",
  "traveler_archived",
  "already_revoked",
  "agreement_not_draft",
  "version_label_taken",
  "role_forbidden",
  "assigned_only",
  "proposal_not_published",
  "proposal_not_sent",
  "terms_reaccept_required",
  "unknown_merge_field",
]);

/** The i18n key of an API refusal code on the agency side (proposal.error.<code>, else a generic one). */
export function proposalErrorKey(code: string): string {
  return ERROR_CODES.has(code) ? `proposal.error.${code}` : "proposal.error.other";
}

const PUBLIC_CODES = new Set([
  "price_changed",
  "terms_changed",
  "link_not_found",
  "code_invalid",
  "code_expired",
  "code_delivery_unavailable",
  "no_email_on_file",
  "too_many_codes",
  "portal_sign_in_required",
  "stage3_locked",
  "document_changed",
  "nothing_to_sign",
  "guardian_signs",
  "not_guardian",
  "esign_not_configured",
  "seal_unavailable",
  "nothing_to_accept",
  "invalid_submission",
  "origin_not_allowed",
  "rate_limited",
  "pay_link_expired",
  "invoice_not_payable",
  "terms_reaccept_required",
  // AA-953 (CRM-24a): the per-person portal.
  "not_your_traveller",
  "lead_only",
  "email_in_use",
  "email_on_file",
  "already_invited",
  "ask_your_agent",
  "too_many_attempts",
  "invalid_email",
  "traveler_not_on_trip",
  "document_write_only",
  "document_not_shared",
  "document_not_yours",
  "document_not_found",
  "invalid_flight",
  "invalid_content",
  "invalid_size",
  "content_mismatch",
  "invalid_content_type",
  "invalid_filename",
  "body_too_large",
  "scan_limit",
]);

/** The i18n key of a refusal on a client page (client.error.<code>, else a generic one). */
export function clientErrorKey(code: string): string {
  return PUBLIC_CODES.has(code) ? `client.error.${code}` : "client.error.other";
}

/** "1,000.00 USD" in the reader's locale; VND without decimals. */
export function formatAmount(amount: string | null | undefined, currency: string, locale: string): string {
  if (amount == null) return "—";
  const n = Number(amount);
  if (!Number.isFinite(n)) return `${amount} ${currency}`;
  const digits = currency === "VND" ? 0 : 2;
  return `${n.toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${currency}`;
}

/** A plain decimal amount from form text ("1,200.5" -> "1200.5"), or null when it is not one (> 0, 2 decimals). */
export function amountOf(text: string): string | null {
  const clean = text.replace(/[,\s]/g, "");
  if (!/^\d{1,12}(\.\d{1,2})?$/.test(clean) || Number(clean) <= 0) return null;
  return clean;
}

export interface DraftForm {
  body: string;
  currency: string;
  total: string;
  deposit: string;
  balanceDueOn: string;
  termsDocId: string;
}

/** The POST/PATCH body of a draft, or the i18n key of the first problem. */
export function draftBody(form: DraftForm): { ok: true; body: Record<string, unknown> } | { ok: false; key: string } {
  if (!form.body.trim()) return { ok: false, key: "proposal.error.body_required" };
  const currency = form.currency.trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(currency)) return { ok: false, key: "proposal.error.invalid_currency" };
  const total = amountOf(form.total);
  const deposit = amountOf(form.deposit);
  if (!total || !deposit) return { ok: false, key: "proposal.error.invalid_amount" };
  if (Number(deposit) > Number(total)) return { ok: false, key: "proposal.error.deposit_above_total" };
  const balanceLeft = Number(deposit) < Number(total);
  if (balanceLeft && !form.balanceDueOn) return { ok: false, key: "proposal.error.balance_due_on_required" };
  return {
    ok: true,
    body: {
      body: form.body.trim(),
      currency,
      total_amount: total,
      deposit_amount: deposit,
      balance_due_on: balanceLeft ? form.balanceDueOn : null,
      terms_doc_id: form.termsDocId || null,
    },
  };
}

/** The Stage 3 body the portal posts: only the fields the traveller filled in (write-only: nothing is read back). */
export function stage3Body(values: Partial<Record<Stage3Field, string>>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const name of STAGE3_FIELDS) {
    const v = (values[name] ?? "").trim();
    if (!v) continue;
    out[name] = name === "nationality" || name === "passport_country" ? v.toUpperCase() : v;
  }
  return out;
}

/** The latest open (draft or sent) version, else the accepted one, else the newest. */
export function currentProposal(list: Proposal[]): Proposal | null {
  return (
    list.find((p) => p.status === "accepted") ??
    list.find((p) => p.status === "sent" || p.status === "draft") ??
    list[0] ??
    null
  );
}

/** The full URL of a client page from the API's path ("/c/p/<token>") and the client host's origin. */
export function clientUrl(origin: string | null, path: string): string {
  return origin ? `${origin.replace(/\/$/, "")}${path}` : path;
}
