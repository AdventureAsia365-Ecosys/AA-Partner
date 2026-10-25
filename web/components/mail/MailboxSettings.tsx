"use client";
// web/components/mail/MailboxSettings.tsx — Settings > Mailbox (AA-945; errata S247 §2 "Mailbox" connection): the
// member's own mailbox and (owner) the agency's: Gmail / Microsoft 365 by OAuth, SMTP with an app password; health,
// Test, Disconnect, who pays; the forward-in address with the Gmail forwarding code and the Microsoft 365 BCC help;
// the sender table. After the provider's consent, /connect/mail/callback brings code + state back here and this
// view posts them to the API (same-origin BFF write).
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  mailErrorKey,
  SMTP_PORTS,
  type IngestAddress,
  type Mailbox,
  type MailboxView,
  type SenderRow,
} from "@/lib/mail";
import type { Role } from "@/lib/nav";

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
};

type Scope = "own" | "agency";

export function MailboxSettings({
  view,
  ingest,
  senders,
  role,
  lang,
  locale,
  oauth,
}: {
  view: MailboxView;
  ingest: IngestAddress | null;
  senders: SenderRow[];
  role: Role;
  lang: Lang;
  locale: DateLocale;
  oauth: { code: string; state: string } | { error: string } | null;
}) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [smtpFor, setSmtpFor] = useState<Scope | null>(null);
  const completed = useRef(false);

  async function call(method: "POST" | "PUT" | "DELETE", path: string, body?: unknown, ok?: string): Promise<unknown> {
    setBusy(true);
    setMessage(null);
    const res = await bffSend<Record<string, unknown>>(method, path, body);
    setBusy(false);
    if (!res.ok) {
      setMessage({ ok: false, text: tr(mailErrorKey(res.detail)) });
      return null;
    }
    if (ok) setMessage({ ok: true, text: tr(ok) });
    return res.data;
  }

  useEffect(() => {
    if (!oauth || completed.current) return;
    completed.current = true;
    if ("error" in oauth) {
      setMessage({ ok: false, text: tr(`mail.oauthError.${oauth.error}`) });
      return;
    }
    void call("POST", "mailbox/oauth/complete", oauth, "mail.connected").then(() => {
      router.replace("/settings/mailbox");
      router.refresh();
    });
  }, [oauth]);

  async function connect(provider: "google" | "microsoft", scope: Scope) {
    const data = (await call("POST", "mailbox/oauth/start", { provider, scope })) as { authorize_url?: string } | null;
    if (data?.authorize_url) window.location.assign(data.authorize_url);
  }

  async function test(scope: Scope) {
    const data = (await call("POST", "mailbox/test", { scope })) as { result?: string } | null;
    if (data) setMessage({ ok: data.result === "ok", text: tr(data.result === "ok" ? "mail.testOk" : "mail.testFailed") });
    router.refresh();
  }

  async function disconnect(scope: Scope) {
    if (await call("DELETE", `mailbox?scope=${scope}`, undefined, "mail.disconnected")) router.refresh();
  }

  function card(scope: Scope, box: Mailbox | undefined) {
    const allowed = scope === "own" || role === "owner";
    return (
      <Panel title={tr(scope === "own" ? "mail.own" : "mail.agency")} testId={`mailbox-${scope}`}>
        {!allowed ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("mail.agencyOwnerOnly")}</p>
        ) : (
          <div style={{ display: "grid", gap: 10 }}>
            {box ? (
              <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 14px", margin: 0, fontSize: 13 }}>
                <dt style={{ color: K.muted }}>{tr(`mail.kind.${box.kind}`)}</dt>
                <dd style={{ margin: 0, overflowWrap: "anywhere" }} data-testid={`mailbox-${scope}-address`}>
                  {box.address}
                </dd>
                <dt style={{ color: K.muted }}>{tr("mail.status")}</dt>
                <dd
                  style={{ margin: 0, color: box.status === "connected" ? K.ink : K.danger }}
                  data-testid={`mailbox-${scope}-status`}
                >
                  {tr(`mail.status.${box.status}`)}
                  {box.failure_class ? ` — ${tr(`mail.failure.${box.failure_class}`)}` : ""}
                </dd>
                {box.last_ok_at && (
                  <>
                    <dt style={{ color: K.muted }}>{tr("mail.lastOk")}</dt>
                    <dd style={{ margin: 0 }}>{formatDate(box.last_ok_at.slice(0, 10), locale)}</dd>
                  </>
                )}
              </dl>
            ) : (
              <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("mail.none")}</p>
            )}
            {box && box.held_count > 0 && (
              <p style={{ margin: 0, fontSize: 13, color: K.danger }} data-testid={`mailbox-${scope}-held`}>
                {fmt(tr("mail.held"), { n: box.held_count })}
              </p>
            )}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              <Button size="sm" variant="primary" disabled={!hydrated || busy || !view.providers.google} onClick={() => connect("google", scope)}>
                {tr("mail.connectGmail")}
              </Button>
              <Button size="sm" disabled={!hydrated || busy || !view.providers.microsoft} onClick={() => connect("microsoft", scope)}>
                {tr("mail.connectMicrosoft")}
              </Button>
              <Button size="sm" disabled={!hydrated || busy} onClick={() => setSmtpFor(smtpFor === scope ? null : scope)}>
                {tr("mail.connectSmtp")}
              </Button>
              {box && box.status !== "disconnected" && (
                <>
                  <Button size="sm" disabled={!hydrated || busy} onClick={() => test(scope)}>
                    {tr("mail.test")}
                  </Button>
                  <Button size="sm" variant="danger" disabled={!hydrated || busy} onClick={() => disconnect(scope)}>
                    {tr("mail.disconnect")}
                  </Button>
                </>
              )}
            </div>
            {(!view.providers.google || !view.providers.microsoft) && (
              <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("mail.providerOff")}</p>
            )}
            {smtpFor === scope && (
              <form
                data-testid={`mailbox-${scope}-smtp`}
                action={async (data) => {
                  const ok = await call(
                    "PUT",
                    "mailbox/smtp",
                    {
                      scope,
                      host: String(data.get("host") ?? "").trim(),
                      port: Number(data.get("port") ?? 0),
                      security: String(data.get("security") ?? "starttls"),
                      username: String(data.get("username") ?? ""),
                      password: String(data.get("password") ?? ""),
                      address: String(data.get("address") ?? "").trim(),
                    },
                    "mail.connected",
                  );
                  if (ok) {
                    setSmtpFor(null);
                    router.refresh();
                  }
                }}
                style={{ display: "grid", gap: 8, maxWidth: 460 }}
              >
                <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("mail.smtp.help")}</p>
                {(["host", "username", "address"] as const).map((name) => (
                  <label key={name} style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                    {tr(`mail.smtp.${name}`)}
                    <input name={name} required style={field} autoComplete="off" />
                  </label>
                ))}
                <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                  {tr("mail.smtp.password")}
                  <input name="password" type="password" required style={field} autoComplete="new-password" />
                </label>
                <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                  {tr("mail.smtp.port")}
                  <select name="port" defaultValue={587} style={field}>
                    {SMTP_PORTS.map((port) => (
                      <option key={port} value={port}>
                        {port}
                      </option>
                    ))}
                  </select>
                </label>
                <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                  {tr("mail.smtp.security")}
                  <select name="security" defaultValue="starttls" style={field}>
                    <option value="starttls">{tr("mail.smtp.starttls")}</option>
                    <option value="tls">{tr("mail.smtp.tls")}</option>
                  </select>
                </label>
                <div>
                  <Button type="submit" size="sm" variant="primary" disabled={!hydrated || busy}>
                    {tr("mail.save")}
                  </Button>
                </div>
              </form>
            )}
          </div>
        )}
      </Panel>
    );
  }

  const own = view.mailboxes.find((m) => m.scope === "own");
  const agency = view.mailboxes.find((m) => m.scope === "agency");
  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="mailbox-settings">
      {message && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: message.ok ? K.ink : K.danger }} data-testid="mailbox-message">
          {message.text}
        </p>
      )}
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="mailbox-billing">
        {tr("mail.billing")}
      </p>
      {card("own", own)}
      {card("agency", agency)}
      <Panel title={tr("mail.forward.title")} testId="mailbox-forward">
        {ingest?.address ? (
          <div style={{ display: "grid", gap: 8, fontSize: 13 }}>
            <p style={{ margin: 0, color: K.muted }}>{tr("mail.forward.help")}</p>
            <code style={{ fontSize: 13.5, overflowWrap: "anywhere" }} data-testid="mailbox-forward-address">
              {ingest.address}
            </code>
            <p style={{ margin: 0 }}>
              <strong>{tr("mail.forward.code")}:</strong>{" "}
              <span data-testid="mailbox-forward-code">{ingest.forwarding_code ?? tr("mail.forward.noCode")}</span>
            </p>
            <p style={{ margin: 0, color: K.muted, fontSize: 12.5 }}>{tr("mail.forward.m365")}</p>
          </div>
        ) : (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("mail.forward.notConfigured")}</p>
        )}
      </Panel>
      <Panel title={tr("mail.senders.title")} testId="mailbox-senders">
        <p style={{ margin: "0 0 8px", fontSize: 12.5, color: K.muted }}>{tr("mail.senders.help")}</p>
        <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
          <tbody>
            {senders.map((s) => (
              <tr key={s.path} data-testid={`sender-${s.path}`}>
                <td style={{ padding: "4px 8px 4px 0" }}>{tr(`mail.senders.path.${s.path}`)}</td>
                <td style={{ padding: "4px 8px", color: K.muted }}>{tr(`mail.senders.to.${s.recipient}`)}</td>
                <td style={{ padding: "4px 0" }}>{tr(`mail.senders.via.${s.via}`)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
