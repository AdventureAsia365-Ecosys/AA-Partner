// Shared test fixtures: a complete configuration (fake values only) and a fake Cognito access token.
import { loadConfig, setConfigForTests, type WebConfig } from "@/lib/config";

export const ENV = {
  ENVIRONMENT: "dev",
  WEB_PUBLIC_ORIGIN: "https://partner.example.test",
  WEB_PARTNER_HOSTS: "partner.example.test",
  WEB_CLIENT_HOSTS: "go.example.test",
  CRM_API_ORIGIN: "https://partner.example.test",
  COGNITO_DOMAIN: "https://auth.example.test",
  COGNITO_CLIENT_ID: "testclient123",
  COGNITO_CLIENT_SECRET: "cccccccc",
  WEB_SESSION_SECRET: "ssssssssssssssssssssssssssssssssssssssss",
  CONTENT_STUDIO_URL: "https://portal.example.test",
  APP_VERSION: "0.1.0",
  GIT_SHA: "abc123",
};

export function testConfig(overrides: Record<string, string | undefined> = {}): WebConfig {
  const cfg = loadConfig({ ...ENV, ...overrides });
  setConfigForTests(cfg);
  return cfg;
}

export const ISS = "https://cognito-idp.example.test/pool";

export function fakeAccessToken(claims: Record<string, unknown> = {}): string {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", kid: "k" })).toString("base64url");
  const body = Buffer.from(
    JSON.stringify({
      sub: "sub-1",
      iss: ISS,
      client_id: ENV.COGNITO_CLIENT_ID,
      token_use: "access",
      exp: Math.floor(Date.now() / 1000) + 1800,
      ...claims,
    }),
  ).toString("base64url");
  return `${header}.${body}.signature`;
}

/** Cookie header value from the Set-Cookie headers of a response (name=value pairs, expired ones dropped). */
export function cookieHeader(res: Response): string {
  return res.headers
    .getSetCookie()
    .filter((c) => !/Max-Age=0/i.test(c))
    .map((c) => c.split(";")[0])
    .join("; ");
}
