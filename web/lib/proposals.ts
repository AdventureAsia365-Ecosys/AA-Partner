// web/lib/proposals.ts
// AA-946 (card CRM-18) — the shapes of the thin lifecycle API (api/app/proposals, api/app/portal) and the pure
// helpers the trip's Proposal tab, Settings > Agreements and the client pages (/c/p/<token>, /c/t/<token>) share.
// Amounts are the client's sell amounts (strings with two decimals from the API); no net, cost or margin here.

export type ProposalStatus = "draft" | "sent" | "accepted" | "superseded" | "withdrawn";
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
  schedule_id: string;
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
  portal: { s3: number; waiver_required: boolean; travellers: TravellerProgress[] };
}

export interface Agreement {
  doc_id: string;
  kind: "booking_terms" | "waiver";
  version_label: string;
  status: "draft" | "published" | "retired";
  body_text: string;
  body_sha256: string;
  published_at: string | null;
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
  price_hash: string;
  accepted_at: string | null;
  schedule: { kind: "deposit" | "balance"; amount: string; currency: string; due_on: string; status: ScheduleStatus }[];
}

/** The traveller portal home (GET /crm/public/v1/portal/<token>). */
export interface PortalHome {
  agency_name: string;
  signed_in: boolean;
  role?: "lead" | "companion";
  trip?: { title: string; start_date: string | null; end_date: string | null; stage: string };
  stage3?: { locked: boolean; fields: string[]; missing: string[]; done: boolean; submitted_at: string | null };
  waiver?: { doc_id: string; version_label: string; body_text: string; body_sha256: string; signed: boolean; signed_at: string | null } | null;
  party?: { role: string; details_done: boolean; waiver_signed: boolean | null; me: boolean }[];
  payments?: { kind: "deposit" | "balance"; amount: string; currency: string; due_on: string; status: ScheduleStatus }[];
}

/** The signed link token of a client page (43 characters, URL-safe base64). */
export const CLIENT_TOKEN = /^[A-Za-z0-9_-]{43}$/;

const ERROR_CODES = new Set([
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
  "proposal_not_accepted",
  "received_in_future",
  "trip_closed",
  "traveler_not_on_trip",
  "traveler_archived",
  "already_revoked",
  "agreement_not_draft",
  "version_label_taken",
  "role_forbidden",
  "assigned_only",
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
  "invalid_submission",
  "origin_not_allowed",
  "rate_limited",
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
