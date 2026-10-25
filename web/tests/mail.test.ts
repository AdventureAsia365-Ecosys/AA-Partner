// AA-945 (CRM-17) web: the BFF allow-list for mail, the empty-sandbox iframe for HTML mail, the debounced draft
// autosave, the OAuth callback (no side effect, code + state handed to Settings > Mailbox; errors not echoed) and the
// disconnected-mailbox banner input.
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GET as mailCallback } from "@/app/connect/mail/callback/route";
import { BFF_ROUTES, matchBff } from "@/lib/bff";
import { MESSAGES } from "@/lib/i18n";
import {
  callbackParams,
  createAutosave,
  disconnected,
  htmlFrame,
  MAIL_FRAME_CSP,
  mailErrorKey,
  SMTP_PORTS,
  newDraftId,
  type MailboxView,
} from "@/lib/mail";
import { testConfig } from "./helpers";

const ID = "0f0e0d0c-0b0a-4908-8706-050403020100";

describe("mail BFF allow-list", () => {
  it("lists every mail route the web calls, and nothing broader", () => {
    const allowed: Array<[string, string]> = [
      ["GET", "mailbox"],
      ["DELETE", "mailbox"],
      ["POST", "mailbox/oauth/start"],
      ["POST", "mailbox/oauth/complete"],
      ["POST", "mailbox/test"],
      ["PUT", "mailbox/smtp"],
      ["GET", "mailbox/ingest-address"],
      ["GET", "mail/senders"],
      ["GET", `mail/imports/${ID}`],
      ["GET", "conversations"],
      ["GET", `conversations/${ID}/messages`],
      ["POST", `conversations/${ID}/link`],
      ["PUT", `messages/drafts/${ID}`],
      ["DELETE", `messages/drafts/${ID}`],
      ["POST", "messages"],
      ["POST", `messages/${ID}/undo`],
      ["POST", `messages/${ID}/edit`],
      ["GET", "saved-replies"],
      ["POST", "saved-replies"],
      ["PATCH", `saved-replies/${ID}`],
      ["DELETE", `saved-replies/${ID}`],
    ];
    for (const [method, path] of allowed) {
      const route = matchBff(method, path);
      expect(route, `${method} ${path}`).not.toBeNull();
      expect(route?.tenant).toBe(true);
    }
    for (const [method, path] of [
      ["POST", "mail/imports"], // raw upload: API only in v1
      ["GET", "mailbox/smtp"],
      ["DELETE", "messages"],
      ["POST", `messages/${ID}/send`],
      ["GET", "conversations/not-a-uuid/messages"],
      ["PUT", "mailbox/oauth/complete"],
    ]) {
      expect(matchBff(method, path), `${method} ${path}`).toBeNull();
    }
    expect(BFF_ROUTES.filter((r) => r.card === "AA-945")).toHaveLength(18);
  });
});

describe("HTML mail", () => {
  it("renders only in an empty sandbox: no script, no same origin, no forms, no popups", () => {
    const frame = htmlFrame("<script>alert(1)</script><p>Hi</p>");
    expect(frame.sandbox).toBe("");
    expect(frame.referrerPolicy).toBe("no-referrer");
    expect(frame.srcDoc).toContain("<p>Hi</p>");
  });

  it("starts the document with a CSP that blocks every remote load (no read receipt, no IP to the sender)", () => {
    const frame = htmlFrame('<img src="https://tracker.example/p.gif"><link rel="stylesheet" href="https://x.example/a.css">');
    expect(frame.srcDoc.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true);
    expect(MAIL_FRAME_CSP).toContain("default-src 'none'");
    expect(MAIL_FRAME_CSP).toContain("img-src data: cid:");
    expect(MAIL_FRAME_CSP).not.toMatch(/https?:|\*/);
  });
});

describe("draft autosave", () => {
  beforeEach(() => vi.useRealTimers());

  it("saves once after the pause, at once on flush, never after cancel", async () => {
    vi.useFakeTimers();
    const save = vi.fn(async () => undefined);
    const auto = createAutosave(save, 3000);
    auto.schedule();
    auto.schedule();
    vi.advanceTimersByTime(2999);
    expect(save).not.toHaveBeenCalled();
    expect(auto.pending).toBe(true);
    vi.advanceTimersByTime(1);
    expect(save).toHaveBeenCalledTimes(1);
    auto.schedule();
    await auto.flush();
    expect(save).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(5000);
    expect(save).toHaveBeenCalledTimes(2);
    auto.schedule();
    auto.cancel();
    vi.advanceTimersByTime(5000);
    expect(save).toHaveBeenCalledTimes(2);
    expect(auto.pending).toBe(false);
    vi.useRealTimers();
  });

  it("uses the injected id source for a new draft", () => {
    expect(newDraftId(() => ID)).toBe(ID);
  });
});

describe("OAuth callback", () => {
  function request(query: string) {
    const headers = new Headers({ host: "partner.example.test" });
    return new NextRequest(`https://partner.example.test/connect/mail/callback${query}`, { headers });
  }

  it("hands code and state to Settings > Mailbox and maps provider errors without echoing them", async () => {
    testConfig();
    const ok = await mailCallback(request("?code=abc&state=def"));
    expect(ok.status).toBe(307);
    expect(ok.headers.get("location")).toBe("https://partner.example.test/settings/mailbox?code=abc&state=def");
    const denied = await mailCallback(request("?error=access_denied&error_description=%3Cscript%3E"));
    expect(denied.headers.get("location")).toBe("https://partner.example.test/settings/mailbox?oauth_error=denied");
    const other = await mailCallback(request("?error=server_error"));
    expect(other.headers.get("location")).toContain("oauth_error=provider");
    const missing = await mailCallback(request("?code=abc"));
    expect(missing.headers.get("location")).toContain("oauth_error=invalid");
  });

  it("parses the query strictly", () => {
    expect(callbackParams(new URLSearchParams(`code=${"x".repeat(5000)}&state=s`))).toEqual({ error: "invalid" });
    expect(callbackParams(new URLSearchParams("code=c&state=s"))).toEqual({ code: "c", state: "s" });
  });
});

describe("mail helpers", () => {
  it("words known errors and falls back for the rest, in both languages", () => {
    expect(mailErrorKey("mailbox_not_connected")).toBe("mail.error.mailbox_not_connected");
    expect(mailErrorKey("something_new")).toBe("mail.error.other");
    // The SMTP codes the API sends (prefixed once: smtp_unreachable, never smtp_smtp_unreachable).
    for (const code of ["smtp_unreachable", "smtp_temporary", "smtp_provider_rejected", "smtp_invalid_port", "smtp_auth_revoked"]) {
      expect(mailErrorKey(code)).toBe(`mail.error.${code}`);
      for (const lang of ["en", "vi"] as const) {
        expect((MESSAGES[lang] as Record<string, string>)[`mail.error.${code}`], `${lang} ${code}`).toBeTruthy();
      }
    }
    expect(mailErrorKey("smtp_smtp_unreachable")).toBe("mail.error.other");
    expect([...SMTP_PORTS].sort()).toEqual([25, 2525, 465, 587].sort());
    for (const lang of ["en", "vi"] as const) {
      const dict = MESSAGES[lang] as Record<string, string>;
      expect(dict["mail.error.other"]).toBeTruthy();
      expect(dict["messages.status.held_reconnect"]).toBeTruthy();
    }
  });

  it("finds the disconnected mailboxes for the banner", () => {
    const view: MailboxView = {
      mailboxes: [
        { channel_account_id: "a", scope: "own", kind: "gmail", address: "a@b.test", status: "disconnected", failure_class: "auth_revoked", last_checked_at: null, last_ok_at: null, last_test_at: null, last_test_result: null, held_count: 2 },
        { channel_account_id: "b", scope: "agency", kind: "smtp", address: "c@d.test", status: "connected", failure_class: null, last_checked_at: null, last_ok_at: null, last_test_at: null, last_test_result: null, held_count: 0 },
      ],
      providers: { google: true, microsoft: false, smtp: true },
      billing_note: "billed_by_your_mail_provider",
    };
    expect(disconnected(view).map((m) => m.channel_account_id)).toEqual(["a"]);
    expect(disconnected(null)).toEqual([]);
  });
});
