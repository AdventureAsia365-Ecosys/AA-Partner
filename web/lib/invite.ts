// web/lib/invite.ts — the invitation join flow on the partner host (AA-939, card CRM-11; root ADR 0006b decision 5).
// Server-only (node:crypto).
//
//   1. The AA invitation mail links to /join?t=<token>. app/join/route.ts seals the token into a cookie (purpose
//      "invite", path /join, 2 hours) and redirects to /join/invite, so the token leaves the address bar and the
//      browser history at once and is never sent to any other path.
//   2. /join/invite shows "Open invitation"; the form POSTs /join/claim, which calls the API's public claim
//      (POST /crm/public/v1/invitations/claim, server to server). The API creates the partner-pool account (or finds
//      the existing one) and answers the one-time temporary password. The web keeps that answer only in a second
//      sealed cookie (purpose "invite-claim", path /join, 15 minutes) and shows it on /join/invite: never in a URL,
//      never in page JavaScript beyond the rendered text, never logged. Both answer cookies name the invitation they
//      answer (a fingerprint of the token), so the answer of another invitation never shows on this one, and GET /join
//      with a new link drops them. S263, found live: a second "Open invitation" POST got already_claimed and
//      overwrote the cookie that held the one-time password, which nobody then saw. Now an "already_claimed" answer
//      goes into its own cookie (purpose "invite-notice"), never over the claim cookie, so even two POSTs that race
//      (the second sent before the first answer's cookie arrived) leave the password in place; and
//      app/join/claim/route.ts does not ask the API again while the claim cookie holds a password.
//   3. "Continue to sign in" -> managed login (temporary password -> own password -> authenticator app; the partner
//      pool requires MFA) -> back to /join/accept (session required), whose form POSTs /join/confirm: the API's
//      POST /crm/v1/invitations/accept with the session's token and the sealed invitation token. The membership binds
//      to the token's (iss, sub); both invitation cookies are cleared and the person lands on their agency.
import { createHash } from "node:crypto";
import type { WebConfig } from "./config";
import { nowSeconds, open, seal } from "./seal";
import type { CookieSpec } from "./session";

export const INVITE_TTL_S = 2 * 60 * 60;
export const CLAIM_TTL_S = 15 * 60;
const API_TIMEOUT_MS = 10_000;
const TOKEN = /^[A-Za-z0-9_-]{43}$/;

export type ClaimState = "created" | "existing" | "already_claimed";

export interface ClaimResult {
  state: ClaimState;
  email: string;
  temporaryPassword: string | null;
}

export type ClaimOutcome = { ok: true; claim: ClaimResult } | { ok: false; status: number; detail: string };

export function isInviteToken(value: string | null | undefined): value is string {
  return typeof value === "string" && TOKEN.test(value);
}

export function inviteCookieNames(cfg: Pick<WebConfig, "secureCookies">): { token: string; claim: string; notice: string } {
  return cfg.secureCookies
    ? { token: "__Secure-aa_crm_inv", claim: "__Secure-aa_crm_invc", notice: "__Secure-aa_crm_invn" }
    : { token: "aa_crm_inv", claim: "aa_crm_invc", notice: "aa_crm_invn" };
}

function cookie(cfg: WebConfig, name: string, value: string, maxAge: number): CookieSpec {
  return { name, value, httpOnly: true, secure: cfg.secureCookies, sameSite: "lax", path: "/join", maxAge };
}

export function inviteCookie(cfg: WebConfig, token: string, now: number = nowSeconds()): CookieSpec {
  return cookie(cfg, inviteCookieNames(cfg).token, seal(cfg.sessionSecret, "invite", { t: token, exp: now + INVITE_TTL_S }), INVITE_TTL_S);
}

export function openInvite(cfg: WebConfig, sealed: string | undefined, now: number = nowSeconds()): string | null {
  const v = open<{ t: string; exp: number }>(cfg.sessionSecret, "invite", sealed, now);
  return v && isInviteToken(v.t) ? v.t : null;
}

/** Which invitation a claim answer belongs to (inside the sealed value; never the token itself). */
function tokenFingerprint(token: string): string {
  return createHash("sha256").update(`aa-crm-invite-claim:${token}`).digest("base64url").slice(0, 22);
}

type SealedClaim = { s: ClaimState; e: string; p: string | null; t: string; exp: number };

/** The cookie that keeps a claim answer: "created" / "existing" in the claim cookie, "already_claimed" in the notice
 * cookie (purpose "invite-notice"), so an "already_claimed" answer can never overwrite a one-time password. */
export function claimCookie(cfg: WebConfig, claim: ClaimResult, token: string, now: number = nowSeconds()): CookieSpec {
  const notice = claim.state === "already_claimed";
  const value = seal(cfg.sessionSecret, notice ? "invite-notice" : "invite-claim", {
    s: claim.state,
    e: claim.email,
    p: notice ? null : claim.temporaryPassword,
    t: tokenFingerprint(token),
    exp: now + CLAIM_TTL_S,
  } satisfies SealedClaim);
  const names = inviteCookieNames(cfg);
  return cookie(cfg, notice ? names.notice : names.claim, value, CLAIM_TTL_S);
}

function openSealed(cfg: WebConfig, purpose: "invite-claim" | "invite-notice", sealed: string | undefined, token: string | null, now: number): ClaimResult | null {
  if (!token) return null;
  const v = open<SealedClaim>(cfg.sessionSecret, purpose, sealed, now);
  if (!v || typeof v.e !== "string" || v.t !== tokenFingerprint(token)) return null;
  const states: ClaimState[] = purpose === "invite-notice" ? ["already_claimed"] : ["created", "existing"];
  if (!states.includes(v.s)) return null;
  return { state: v.s, email: v.e, temporaryPassword: purpose === "invite-claim" && typeof v.p === "string" ? v.p : null };
}

/** The claim answer ("created" / "existing") of the invitation `token`; null when absent, expired, tampered with or of
 * another invitation. */
export function openClaim(cfg: WebConfig, sealed: string | undefined, token: string | null, now: number = nowSeconds()): ClaimResult | null {
  return openSealed(cfg, "invite-claim", sealed, token, now);
}

/** The "already_claimed" answer of the invitation `token` (notice cookie); null otherwise. */
export function openNotice(cfg: WebConfig, sealed: string | undefined, token: string | null, now: number = nowSeconds()): ClaimResult | null {
  return openSealed(cfg, "invite-notice", sealed, token, now);
}

/** What /join/invite shows: the claim answer of this invitation first (it holds the password), else its notice. */
export function shownAnswer(cfg: WebConfig, jar: { get(name: string): { value: string } | undefined }, token: string | null, now: number = nowSeconds()): ClaimResult | null {
  const names = inviteCookieNames(cfg);
  return openClaim(cfg, jar.get(names.claim)?.value, token, now) ?? openNotice(cfg, jar.get(names.notice)?.value, token, now);
}

/**
 * What POST /join/claim does with a claim answer already in the browser (S263):
 * - "keep": the cookie already holds this invitation's one-time password, so the person is shown it again and the
 *   API is not asked (an answer can only say "already_claimed" now, and showing the password again is the point);
 * - "ask": anything else (no answer yet, or one without a password).
 */
export function claimStep(prior: ClaimResult | null): "keep" | "ask" {
  return prior?.temporaryPassword ? "keep" : "ask";
}

/** GET /join with a link: drop the answer cookies that belong to another invitation (or to none), so a stale answer
 * of an earlier link (another address, "opened before") never meets the new one. Answers of this link stay. */
export function staleAnswerCookies(cfg: WebConfig, jar: { get(name: string): { value: string } | undefined }, token: string | null, now: number = nowSeconds()): CookieSpec[] {
  const names = inviteCookieNames(cfg);
  const stale: CookieSpec[] = [];
  const claim = jar.get(names.claim)?.value;
  if (claim !== undefined && !openClaim(cfg, claim, token, now)) stale.push(cookie(cfg, names.claim, "", 0));
  const notice = jar.get(names.notice)?.value;
  if (notice !== undefined && !openNotice(cfg, notice, token, now)) stale.push(cookie(cfg, names.notice, "", 0));
  return stale;
}

export function clearedInviteCookies(cfg: WebConfig): CookieSpec[] {
  const names = inviteCookieNames(cfg);
  return [cookie(cfg, names.token, "", 0), cookie(cfg, names.claim, "", 0), cookie(cfg, names.notice, "", 0)];
}

/** The API's public claim (server to server; the person has no account yet, so no bearer). */
export async function claimInvitation(cfg: WebConfig, token: string, fetchImpl: typeof fetch = fetch): Promise<ClaimOutcome> {
  let res: Response;
  try {
    res = await fetchImpl(`${cfg.crmApiOrigin}/crm/public/v1/invitations/claim`, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, status: 0, detail: "api_unreachable" };
  }
  const json = (await res.json().catch(() => null)) as Record<string, unknown> | null;
  if (!res.ok || !json) {
    return { ok: false, status: res.status, detail: typeof json?.detail === "string" ? json.detail : "error" };
  }
  const state = json.state as ClaimState;
  if (!["created", "existing", "already_claimed"].includes(state) || typeof json.email !== "string") {
    return { ok: false, status: 502, detail: "bad_claim_answer" };
  }
  return {
    ok: true,
    claim: {
      state,
      email: json.email,
      temporaryPassword: typeof json.temporary_password === "string" ? json.temporary_password : null,
    },
  };
}

/** Error codes the join pages word (anything else reads as a generic failure). */
export const JOIN_ERRORS = [
  "invitation_not_found",
  "invitation_expired",
  "invitation_used",
  "invitation_not_claimed",
  "invitation_identity_mismatch",
  "account_setup_pending",
  "already_member",
  "invitation_race",
  "no_invitation",
] as const;
export type JoinError = (typeof JOIN_ERRORS)[number] | "failed";

export function joinError(detail: string | null | undefined): JoinError {
  return (JOIN_ERRORS as readonly string[]).includes(detail ?? "") ? (detail as JoinError) : "failed";
}
