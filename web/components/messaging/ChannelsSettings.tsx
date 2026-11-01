"use client";
// web/components/messaging/ChannelsSettings.tsx — Settings > SMS and WhatsApp (AA-958, card CRM-28; errata S247 §2
// "Connections": owner edits, agents and assistants see status only; each card says who pays). The owner connects the
// agency's own Twilio account (Account SID + Auth Token, optional API key, the SMS and WhatsApp senders; the sandbox
// provider on Dev only), copies the two webhook addresses into Twilio, tests the connection (optionally a test message
// to their own mobile: on WhatsApp an approved template, since free text outside their own 24-hour window fails later;
// a reply to it checks the incoming webhook), refreshes the approved WhatsApp templates, or disconnects (the
// credentials are deleted).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, ConfirmModal, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  messagingErrorKey,
  testTemplates,
  TEXT_CHANNELS,
  type MessagingView,
  type TestResult,
  type TextChannel,
} from "@/lib/messaging";

type Form = {
  provider: "twilio" | "sandbox";
  account_sid: string;
  auth_token: string;
  api_key_sid: string;
  api_key_secret: string;
  sms_sender: string;
  whatsapp_sender: string;
};

const STATUS_TONE = { connected: "success", needs_attention: "warning", disconnected: "danger", not_set_up: "neutral" } as const;

export function ChannelsSettings({ view, lang, locale }: { view: MessagingView; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const conn = view.connection;
  const [form, setForm] = useState<Form>({
    provider: conn?.provider ?? view.providers[0] ?? "twilio",
    account_sid: "",
    auth_token: "",
    api_key_sid: "",
    api_key_secret: "",
    sms_sender: conn?.sms_sender ?? "",
    whatsapp_sender: conn?.whatsapp_sender ?? "",
  });
  const [testTo, setTestTo] = useState("");
  const [testChannel, setTestChannel] = useState<TextChannel>(conn?.whatsapp_sender ? "whatsapp" : "sms");
  const approved = testTemplates(view.templates);
  const [testTemplate, setTestTemplate] = useState(approved[0]?.content_sid ?? "");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const set = (name: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    // Read the value now: React clears currentTarget once the handler returns, before the updater runs.
    const value = e.currentTarget.value;
    setForm((f) => ({ ...f, [name]: value }));
  };

  async function call<T>(
    method: "POST" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
    errors: Record<string, string> = {},
  ): Promise<T | null> {
    setBusy(true);
    setNotice(null);
    const res = await bffSend<T>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setNotice({ ok: false, text: tr(errors[res.detail] ?? messagingErrorKey(res.detail)) });
      return null;
    }
    return res.data;
  }

  async function connect() {
    const blank = (v: string) => (v.trim() ? v.trim() : null);
    const done = await call("PUT", "messaging/connection", {
      provider: form.provider,
      account_sid: form.account_sid.trim(),
      auth_token: form.auth_token.trim(),
      api_key_sid: blank(form.api_key_sid),
      api_key_secret: blank(form.api_key_secret),
      sms_sender: blank(form.sms_sender),
      whatsapp_sender: blank(form.whatsapp_sender),
    });
    if (done) {
      setForm((f) => ({ ...f, auth_token: "", api_key_secret: "" }));
      setNotice({ ok: true, text: tr("msg.connected") });
      router.refresh();
    }
  }

  async function test() {
    const body = !testTo.trim()
      ? {}
      : testChannel === "whatsapp" && testTemplate
        ? { channel: testChannel, to: testTo.trim(), content_sid: testTemplate }
        : { channel: testChannel, to: testTo.trim() };
    // A WhatsApp test without an approved template: say what to do (not the client-window wording of the composer).
    const out = await call<TestResult>("POST", "messaging/test", body, { template_required: "msg.testNoTemplate" });
    if (out) {
      const reason = tr(`msg.failure.${out.check}`) === `msg.failure.${out.check}` ? out.check : tr(`msg.failure.${out.check}`);
      setNotice(
        out.result === "ok"
          ? { ok: true, text: tr(out.test_message_sent ? "msg.testSent" : "msg.testOk") }
          : { ok: false, text: fmt(tr("msg.testFailed"), { reason }) },
      );
      router.refresh();
    }
  }

  async function refresh() {
    if (await call("POST", "messaging/templates/refresh")) setNotice({ ok: true, text: tr("msg.refreshQueued") });
  }

  async function disconnect() {
    setConfirm(false);
    if (await call("DELETE", "messaging/connection")) {
      setNotice({ ok: true, text: tr("msg.disconnected") });
      router.refresh();
    }
  }

  const statusLine = (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
      <Badge tone={STATUS_TONE[view.status]}>{tr(`msg.status.${view.status}`)}</Badge>
      {conn?.failure_class && (
        <span style={{ fontSize: 12.5, color: K.danger }} data-testid="msg-failure">
          {tr(`msg.health.${conn.failure_class}`)}
        </span>
      )}
      {TEXT_CHANNELS.map((c) => (
        <span key={c} style={{ fontSize: 12.5, color: view.channels[c] ? K.ink : K.muted }} data-testid={`msg-channel-${c}`}>
          {fmt(tr(view.channels[c] ? "msg.channelOn" : "msg.channelOff"), { channel: tr(`msg.channel.${c}`) })}
        </span>
      ))}
    </div>
  );

  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="msg-settings">
      <Panel title={tr("msg.status")} testId="msg-status">
        <div style={{ display: "grid", gap: 8 }}>
          {statusLine}
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("msg.whoPays")}</p>
          {!view.can_manage && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("msg.ownerOnly")}</p>}
          {conn && (
            <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 14px", margin: 0, fontSize: 13 }}>
              <dt style={{ color: K.muted }}>{tr("msg.account")}</dt>
              <dd style={{ margin: 0 }} data-testid="msg-account">
                {conn.account} · {tr(`msg.provider.${conn.provider}`)}
              </dd>
              {conn.last_ok_at && (
                <>
                  <dt style={{ color: K.muted }}>{tr("msg.lastOk")}</dt>
                  <dd style={{ margin: 0 }}>{formatDate(conn.last_ok_at.slice(0, 10), locale)}</dd>
                </>
              )}
              {conn.last_inbound_at && (
                <>
                  <dt style={{ color: K.muted }}>{tr("msg.lastInbound")}</dt>
                  <dd style={{ margin: 0 }}>{formatDate(conn.last_inbound_at.slice(0, 10), locale)}</dd>
                </>
              )}
              {conn.inbound_check && (
                <>
                  <dt style={{ color: K.muted }}>{tr("msg.inboundCheck")}</dt>
                  <dd style={{ margin: 0 }} data-testid="msg-inbound-check">
                    {tr(`msg.inboundCheck.${conn.inbound_check}`)}
                  </dd>
                </>
              )}
            </dl>
          )}
        </div>
      </Panel>

      {notice && (
        <p role="status" data-testid="msg-notice" style={{ margin: 0, fontSize: 13, color: notice.ok ? K.ink : K.danger }}>
          {notice.text}
        </p>
      )}

      {view.can_manage && conn && (
        <Panel title={tr("msg.webhooks")} testId="msg-webhooks">
          <div style={{ display: "grid", gap: 8 }}>
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("msg.webhooksHelp")}</p>
            <Field label={tr("msg.hookInbound")}>
              <Input readOnly value={conn.hook_url} data-testid="msg-hook-url" onFocus={(e) => e.currentTarget.select()} />
            </Field>
            <Field label={tr("msg.hookStatus")}>
              <Input readOnly value={conn.status_callback_url} onFocus={(e) => e.currentTarget.select()} />
            </Field>
          </div>
        </Panel>
      )}

      {view.can_manage && (
        <Panel title={tr(conn ? "msg.reconnect" : "msg.connect")} testId="msg-connect">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void connect();
            }}
            style={{ display: "grid", gap: 10, maxWidth: 520 }}
          >
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("msg.vendorSteps")}</p>
            {view.providers.length > 1 && (
              <Field label={tr("msg.provider")}>
                <Select
                  value={form.provider}
                  onChange={set("provider")}
                  options={view.providers.map((p) => ({ value: p, label: tr(`msg.provider.${p}`) }))}
                />
              </Field>
            )}
            <Field label={tr("msg.accountSid")} hint={tr("msg.accountSidHint")} required>
              <Input value={form.account_sid} onChange={set("account_sid")} autoComplete="off" maxLength={34} required />
            </Field>
            <Field label={tr("msg.authToken")} hint={tr("msg.authTokenHint")} required>
              <Input type="password" value={form.auth_token} onChange={set("auth_token")} autoComplete="new-password" maxLength={128} required />
            </Field>
            <Field label={tr("msg.apiKeySid")} hint={tr("msg.apiKeyHint")}>
              <Input value={form.api_key_sid} onChange={set("api_key_sid")} autoComplete="off" maxLength={34} />
            </Field>
            <Field label={tr("msg.apiKeySecret")}>
              <Input type="password" value={form.api_key_secret} onChange={set("api_key_secret")} autoComplete="new-password" maxLength={64} />
            </Field>
            <Field label={tr("msg.smsSender")} hint={tr("msg.smsSenderHint")}>
              <Input value={form.sms_sender} onChange={set("sms_sender")} autoComplete="off" maxLength={40} />
            </Field>
            <Field label={tr("msg.whatsappSender")} hint={tr("msg.whatsappSenderHint")}>
              <Input value={form.whatsapp_sender} onChange={set("whatsapp_sender")} autoComplete="off" inputMode="tel" maxLength={40} />
            </Field>
            <div>
              <Button type="submit" variant="primary" disabled={!hydrated || busy} testId="msg-connect-submit">
                {tr(conn ? "msg.reconnect" : "msg.connect")}
              </Button>
            </div>
          </form>
        </Panel>
      )}

      {view.can_manage && conn && (
        <Panel title={tr("msg.test")} testId="msg-test">
          <div style={{ display: "grid", gap: 10, maxWidth: 520 }}>
            <Field label={tr("msg.testTo")}>
              <Input value={testTo} onChange={(e) => setTestTo(e.currentTarget.value)} inputMode="tel" maxLength={40} />
            </Field>
            <Field label={tr("msg.testChannel")}>
              <Select
                value={testChannel}
                onChange={(e) => setTestChannel(e.currentTarget.value as TextChannel)}
                options={TEXT_CHANNELS.map((c) => ({ value: c, label: tr(`msg.channel.${c}`) }))}
              />
            </Field>
            {testChannel === "whatsapp" &&
              (approved.length > 0 ? (
                <Field label={tr("msg.testTemplate")}>
                  <Select
                    value={testTemplate}
                    onChange={(e) => setTestTemplate(e.currentTarget.value)}
                    options={approved.map((x) => ({ value: x.content_sid, label: `${x.name} (${x.language})` }))}
                    data-testid="msg-test-template"
                  />
                </Field>
              ) : (
                <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="msg-test-no-template">
                  {tr("msg.testNoTemplate")}
                </p>
              ))}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              <Button size="sm" disabled={!hydrated || busy} onClick={test} testId="msg-test-button">
                {tr("msg.test")}
              </Button>
              <Button size="sm" variant="danger" disabled={!hydrated || busy} onClick={() => setConfirm(true)}>
                {tr("msg.disconnect")}
              </Button>
            </div>
          </div>
        </Panel>
      )}

      {view.can_manage && conn && (
        <Panel title={tr("msg.templates")} testId="msg-templates">
          <div style={{ display: "grid", gap: 8 }}>
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("msg.templatesHelp")}</p>
            {(view.templates ?? []).length === 0 ? (
              <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("msg.templatesEmpty")}</p>
            ) : (
              <ul style={{ margin: 0, padding: 0, display: "grid", gap: 6 }}>
                {(view.templates ?? []).map((tpl) => (
                  <li
                    key={tpl.content_sid}
                    data-testid={`msg-template-${tpl.content_sid}`}
                    style={{ listStyle: "none", display: "grid", gap: 4, padding: 8, border: `1px solid ${K.line2}`, borderRadius: 8 }}
                  >
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", fontSize: 13 }}>
                      <strong>{tpl.name}</strong>
                      <span style={{ color: K.muted }}>{tpl.language}</span>
                      <Badge tone={tpl.approval === "approved" ? "success" : tpl.approval === "pending" ? "info" : "neutral"}>
                        {tr(`msg.approval.${tpl.approval ?? "unsubmitted"}`)}
                      </Badge>
                    </div>
                    <span style={{ fontSize: 12.5, color: K.muted, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{tpl.body}</span>
                  </li>
                ))}
              </ul>
            )}
            <div>
              <Button size="sm" disabled={!hydrated || busy} onClick={refresh} testId="msg-refresh">
                {tr("msg.refresh")}
              </Button>
            </div>
          </div>
        </Panel>
      )}

      <ConfirmModal
        open={confirm}
        title={tr("msg.disconnect")}
        message={tr("msg.disconnectConfirm")}
        confirmLabel={tr("msg.disconnect")}
        danger
        onConfirm={disconnect}
        onClose={() => setConfirm(false)}
      />
    </div>
  );
}
