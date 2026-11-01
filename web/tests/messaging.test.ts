// AA-958 (CRM-28) web: the BFF allow-list for SMS / WhatsApp, the window, the template helpers, the error and failure
// words (every code the API answers has its EN and VI text).
import { describe, expect, it } from "vitest";
import { BFF_ROUTES, matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  deliveryTone,
  failureKey,
  FAILURE_CODES,
  isTextChannel,
  messagingErrorKey,
  renderTemplate,
  templateVariables,
  variableOk,
  windowHoursLeft,
} from "@/lib/messaging";

describe("BFF allow-list", () => {
  it("lets the messaging routes through and nothing else", () => {
    for (const [method, path] of [
      ["GET", "messaging"],
      ["PUT", "messaging/connection"],
      ["DELETE", "messaging/connection"],
      ["POST", "messaging/test"],
      ["POST", "messaging/templates/refresh"],
      ["GET", "messaging/compose"],
      ["POST", "messaging/messages"],
    ]) {
      const route = matchBff(method, path);
      expect(route, `${method} ${path}`).not.toBeNull();
      expect(route?.tenant).toBe(true);
    }
    for (const [method, path] of [
      ["POST", "messaging"],
      ["GET", "messaging/connection"],
      ["POST", "messaging/templates"],
      ["DELETE", "messaging/messages"],
      ["POST", "hooks/twilio/abc"],
    ]) {
      expect(matchBff(method, path), `${method} ${path}`).toBeNull();
    }
    expect(BFF_ROUTES.filter((r) => r.card === "AA-958")).toHaveLength(6);
  });
});

describe("helpers", () => {
  it("counts the WhatsApp window in whole hours, 0 when closed or never opened", () => {
    const now = Date.parse("2026-11-01T10:00:00Z");
    expect(windowHoursLeft("2026-11-02T07:30:00Z", now)).toBe(22);
    expect(windowHoursLeft("2026-11-01T10:00:01Z", now)).toBe(1);
    expect(windowHoursLeft("2026-11-01T09:00:00Z", now)).toBe(0);
    expect(windowHoursLeft(null, now)).toBe(0);
    expect(windowHoursLeft("not a date", now)).toBe(0);
  });

  it("reads and fills template placeholders", () => {
    const body = "Hi {{1}}, the deposit for {{ 2 }} is due. Thanks, {{1}}";
    expect(templateVariables({ body, variables: [] })).toEqual(["1", "2"]);
    expect(templateVariables({ body, variables: ["x"] })).toEqual(["x"]);
    expect(renderTemplate(body, { "1": " Lan ", "2": "" })).toBe("Hi Lan, the deposit for {{ 2 }} is due. Thanks, Lan");
  });

  it("accepts template values WhatsApp accepts", () => {
    expect(variableOk("Hoi An")).toBe(true);
    expect(variableOk("  ")).toBe(false);
    expect(variableOk("two\nlines")).toBe(false);
    expect(variableOk("tab\there")).toBe(false);
    expect(variableOk("five     spaces")).toBe(false);
    expect(variableOk("x".repeat(1025))).toBe(false);
  });

  it("knows the channels and the delivery tones", () => {
    expect(isTextChannel("sms") && isTextChannel("whatsapp") && !isTextChannel("email")).toBe(true);
    expect([deliveryTone("read"), deliveryTone("failed"), deliveryTone("sent"), deliveryTone(null)]).toEqual([
      "success",
      "danger",
      "info",
      "neutral",
    ]);
  });
});

describe("words", () => {
  const API_ERRORS = [
    "template_required",
    "channel_not_connected",
    "no_client_phone",
    "client_opted_out",
    "template_not_approved",
    "template_variables_missing",
    "invalid_template_variable",
    "messaging_auth_revoked",
    "messaging_hook_not_configured",
  ];

  it("has EN and VI text for every error and failure code", () => {
    for (const code of API_ERRORS) {
      const key = messagingErrorKey(code) as keyof typeof MESSAGES.en;
      expect(key).toBe(`msg.error.${code}`);
      expect(MESSAGES.en[key]).toBeTruthy();
      expect(MESSAGES.vi[key]).toBeTruthy();
    }
    expect(messagingErrorKey("something_new")).toBe("msg.error.other");
    for (const code of FAILURE_CODES) {
      const key = failureKey(code) as keyof typeof MESSAGES.en;
      expect(MESSAGES.en[key], code).toBeTruthy();
      expect(MESSAGES.vi[key], code).toBeTruthy();
    }
    expect(failureKey(null)).toBe("msg.failure.other");
    for (const c of ["auth_revoked", "permission_missing", "vendor_rejected", "rate_limited", "unreachable"]) {
      expect(MESSAGES.vi[`msg.health.${c}` as keyof typeof MESSAGES.en]).toBeTruthy();
    }
    expect(MESSAGES.en["bell.event.received"]).toContain("{trip}");
  });

  it("never names an issue id in the copy", () => {
    for (const [key, text] of Object.entries(MESSAGES.en).concat(Object.entries(MESSAGES.vi))) {
      if (key.startsWith("msg.")) expect(text).not.toMatch(/AA-\d+/);
    }
  });
});
