// web/lib/oauth.ts
// AA-935 — Cognito managed login, authorization code flow with PKCE (S256), confidential client (client secret on the
// server only). The web never verifies the access token itself: the CRM API does on every call (route classes, ADR
// 0006b decision 6). Here the token's claims are only read to know whose session it is (sub, iss, client) and when it
// expires; a token whose token_use / client_id do not match this client is refused anyway.

import { createHash, randomBytes } from "node:crypto";
import type { WebConfig } from "./config";

export const SCOPES = "openid email profile";
const TOKEN_TIMEOUT_MS = 10_000;

export type Fetch = typeof fetch;

export interface TokenSet {
  accessToken: string;
  refreshToken: string | null;
  expiresIn: number;
}

export interface AccessClaims {
  sub: string;
  iss: string;
  clientId: string;
  exp: number;
}

export class OAuthError extends Error {
  constructor(
    readonly code: string,
    readonly status: number = 0,
  ) {
    super(code);
  }
}

export function randomToken(bytes = 32): string {
  return randomBytes(bytes).toString("base64url");
}

export function pkceChallenge(verifier: string): string {
  return createHash("sha256").update(verifier).digest("base64url");
}

export function callbackUrl(cfg: WebConfig): string {
  return `${cfg.publicOrigin}/auth/callback`;
}

export function authorizeUrl(
  cfg: WebConfig,
  p: { state: string; verifier: string; nonce: string; lang: "en" | "vi" },
): string {
  const q = new URLSearchParams({
    response_type: "code",
    client_id: cfg.cognitoClientId,
    redirect_uri: callbackUrl(cfg),
    scope: SCOPES,
    state: p.state,
    nonce: p.nonce,
    code_challenge: pkceChallenge(p.verifier),
    code_challenge_method: "S256",
    lang: p.lang,
  });
  return `${cfg.cognitoDomain}/oauth2/authorize?${q.toString()}`;
}

export function logoutUrl(cfg: WebConfig): string {
  const q = new URLSearchParams({ client_id: cfg.cognitoClientId, logout_uri: `${cfg.publicOrigin}/signed-out` });
  return `${cfg.cognitoDomain}/logout?${q.toString()}`;
}

function basicAuth(cfg: WebConfig): string {
  const id = encodeURIComponent(cfg.cognitoClientId);
  const secret = encodeURIComponent(cfg.cognitoClientSecret);
  return `Basic ${Buffer.from(`${id}:${secret}`).toString("base64")}`;
}

async function tokenCall(cfg: WebConfig, body: URLSearchParams, fetchImpl: Fetch): Promise<TokenSet> {
  let res: Response;
  try {
    res = await fetchImpl(`${cfg.cognitoDomain}/oauth2/token`, {
      method: "POST",
      headers: { Authorization: basicAuth(cfg), "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
      cache: "no-store",
      signal: AbortSignal.timeout(TOKEN_TIMEOUT_MS),
    });
  } catch {
    throw new OAuthError("token_endpoint_unreachable");
  }
  if (!res.ok) {
    // Cognito answers 400 invalid_grant for a used / expired code or a revoked refresh token.
    throw new OAuthError(res.status === 400 ? "invalid_grant" : "token_endpoint_error", res.status);
  }
  const json = (await res.json()) as { access_token?: unknown; refresh_token?: unknown; expires_in?: unknown };
  if (typeof json.access_token !== "string" || typeof json.expires_in !== "number") {
    throw new OAuthError("token_response_invalid", res.status);
  }
  return {
    accessToken: json.access_token,
    refreshToken: typeof json.refresh_token === "string" ? json.refresh_token : null,
    expiresIn: json.expires_in,
  };
}

export function exchangeCode(cfg: WebConfig, code: string, verifier: string, fetchImpl: Fetch = fetch) {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: cfg.cognitoClientId,
    code,
    redirect_uri: callbackUrl(cfg),
    code_verifier: verifier,
  });
  return tokenCall(cfg, body, fetchImpl);
}

export function refreshTokens(cfg: WebConfig, refreshToken: string, fetchImpl: Fetch = fetch) {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: cfg.cognitoClientId,
    refresh_token: refreshToken,
  });
  return tokenCall(cfg, body, fetchImpl);
}

/** Revoke a refresh token at sign-out (best effort: the session cookies are cleared either way). */
export async function revoke(cfg: WebConfig, refreshToken: string, fetchImpl: Fetch = fetch): Promise<boolean> {
  try {
    const res = await fetchImpl(`${cfg.cognitoDomain}/oauth2/revoke`, {
      method: "POST",
      headers: { Authorization: basicAuth(cfg), "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: refreshToken, client_id: cfg.cognitoClientId }).toString(),
      cache: "no-store",
      signal: AbortSignal.timeout(TOKEN_TIMEOUT_MS),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** The claims of a Cognito access token issued to THIS client, or an OAuthError. No signature check (see above). */
export function accessClaims(cfg: WebConfig, token: string): AccessClaims {
  const parts = token.split(".");
  if (parts.length !== 3) throw new OAuthError("token_malformed");
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")) as Record<string, unknown>;
  } catch {
    throw new OAuthError("token_malformed");
  }
  const { sub, iss, client_id: clientId, exp, token_use: use } = payload;
  if (use !== "access" || clientId !== cfg.cognitoClientId) throw new OAuthError("token_wrong_client");
  if (typeof sub !== "string" || typeof iss !== "string" || typeof exp !== "number") {
    throw new OAuthError("token_malformed");
  }
  return { sub, iss, clientId, exp };
}
