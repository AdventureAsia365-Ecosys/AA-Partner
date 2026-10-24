// Cognito code + PKCE: authorize / logout URLs, token calls (Basic auth, no secret in the body), claim checks.
import { describe, expect, it, vi } from "vitest";
import {
  accessClaims,
  authorizeUrl,
  exchangeCode,
  logoutUrl,
  OAuthError,
  pkceChallenge,
  refreshTokens,
  revoke,
} from "@/lib/oauth";
import { ENV, fakeAccessToken, testConfig } from "./helpers";

const cfg = testConfig();

describe("URLs", () => {
  it("authorize carries PKCE S256, state, nonce, scopes, the callback and the language", () => {
    const url = new URL(authorizeUrl(cfg, { state: "st", verifier: "ver", nonce: "no", lang: "vi" }));
    expect(url.origin + url.pathname).toBe("https://auth.example.test/oauth2/authorize");
    const q = url.searchParams;
    expect(q.get("client_id")).toBe(ENV.COGNITO_CLIENT_ID);
    expect(q.get("redirect_uri")).toBe("https://partner.example.test/auth/callback");
    expect(q.get("code_challenge")).toBe(pkceChallenge("ver"));
    expect(q.get("code_challenge_method")).toBe("S256");
    expect(q.get("scope")).toBe("openid email profile");
    expect(q.get("lang")).toBe("vi");
    expect(url.toString()).not.toContain(ENV.COGNITO_CLIENT_SECRET);
    // RFC 7636 appendix B test vector.
    expect(pkceChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });

  it("logout returns to /signed-out", () => {
    const url = new URL(logoutUrl(cfg));
    expect(url.pathname).toBe("/logout");
    expect(url.searchParams.get("logout_uri")).toBe("https://partner.example.test/signed-out");
  });
});

describe("token calls", () => {
  it("exchange the code with Basic auth and the verifier", async () => {
    const fetchImpl = vi.fn(async () =>
      Response.json({ access_token: "at", refresh_token: "rt", expires_in: 1800, id_token: "id" }),
    );
    const tokens = await exchangeCode(cfg, "code-1", "ver-1", fetchImpl as unknown as typeof fetch);
    expect(tokens).toEqual({ accessToken: "at", refreshToken: "rt", expiresIn: 1800 });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://auth.example.test/oauth2/token");
    const body = new URLSearchParams(String(init.body));
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code_verifier")).toBe("ver-1");
    expect(String(init.body)).not.toContain(ENV.COGNITO_CLIENT_SECRET);
    const auth = (init.headers as Record<string, string>).Authorization;
    expect(Buffer.from(auth.replace("Basic ", ""), "base64").toString()).toBe(
      `${ENV.COGNITO_CLIENT_ID}:${ENV.COGNITO_CLIENT_SECRET}`,
    );
  });

  it("map refusals and outages to OAuthError codes", async () => {
    const refused = vi.fn(async () => new Response("{}", { status: 400 }));
    await expect(refreshTokens(cfg, "rt", refused as unknown as typeof fetch)).rejects.toMatchObject({ code: "invalid_grant" });
    const down = vi.fn(async () => {
      throw new TypeError("network");
    });
    await expect(refreshTokens(cfg, "rt", down as unknown as typeof fetch)).rejects.toMatchObject({
      code: "token_endpoint_unreachable",
    });
    const odd = vi.fn(async () => Response.json({ nope: true }));
    await expect(exchangeCode(cfg, "c", "v", odd as unknown as typeof fetch)).rejects.toBeInstanceOf(OAuthError);
    const fivexx = vi.fn(async () => new Response("", { status: 503 }));
    await expect(exchangeCode(cfg, "c", "v", fivexx as unknown as typeof fetch)).rejects.toMatchObject({ code: "token_endpoint_error" });
  });

  it("revoke is best effort", async () => {
    expect(await revoke(cfg, "rt", (async () => new Response("", { status: 200 })) as unknown as typeof fetch)).toBe(true);
    expect(
      await revoke(cfg, "rt", (async () => {
        throw new Error("x");
      }) as unknown as typeof fetch),
    ).toBe(false);
  });
});

describe("access token claims", () => {
  it("accept this client's access token and refuse others", () => {
    expect(accessClaims(cfg, fakeAccessToken())).toMatchObject({ sub: "sub-1", clientId: ENV.COGNITO_CLIENT_ID });
    expect(() => accessClaims(cfg, fakeAccessToken({ token_use: "id" }))).toThrow("token_wrong_client");
    expect(() => accessClaims(cfg, fakeAccessToken({ client_id: "other" }))).toThrow("token_wrong_client");
    expect(() => accessClaims(cfg, fakeAccessToken({ sub: 5 }))).toThrow("token_malformed");
    expect(() => accessClaims(cfg, "a.b")).toThrow("token_malformed");
    expect(() => accessClaims(cfg, "a.%%%.c")).toThrow("token_malformed");
  });
});
