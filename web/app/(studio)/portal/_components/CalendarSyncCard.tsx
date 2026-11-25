"use client";
// web/app/(studio)/portal/_components/CalendarSyncCard.tsx — AA-1011 part 3: Account > Integrations > Calendar. The
// member connects their own Google or Microsoft 365 calendar (OAuth through AA's client; the consent comes back
// through /connect/mail/callback, which sends a calendar state here as ?calendar_code&calendar_state), and the CRM
// pushes their departures and tasks one way. S294: every call goes through the studio's own route handler
// (/api/studio/calendar, lib/studio/calendar.ts) with the studio's tenant, never the CRM's /bff (a studio session picks
// no CRM agency, so /bff answered 409 on page load); a person with no CRM role in this agency sees why and no connect
// button (the read answers 200 with available: false). Disconnect asks first; the API then removes the pushed events and the
// token (no revoke at the provider: the OAuth client is the mailbox's, the dialog says how to remove access). A provider AA has no client for (Microsoft until the Entra values exist) shows "Not available yet".
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Badge, Button, ConfirmModal, Field, K, Select, useFormat, useT } from "@/app/_kit";
import {
  CALENDAR_PROVIDERS,
  CALENDAR_RETURN,
  HEALTH_TONE,
  calendarReturn,
  connectionHealth,
  connectionOf,
  errorKey,
  type CalendarConnection,
  type CalendarConnectionsView,
  type CalendarProvider,
  type CalendarScope,
} from "@/lib/calendarSync";
import { studioCalendarUrl } from "@/lib/studio/calendar";
import { SectionSkeleton } from "./SectionSkeleton";
import { CALENDAR_SYNC_MESSAGES } from "./calendarSync.messages";

type Key = keyof typeof CALENDAR_SYNC_MESSAGES.en;
type Message = { ok: boolean; key: Key };
const HEALTH_KEY = {
  syncing: "health_syncing",
  needs_reconnect: "health_needs_reconnect",
  disconnecting: "health_disconnecting",
  waiting: "health_waiting",
} as const satisfies Record<string, Key>;

function errKey(detail: string | null | undefined): Key {
  return `err_${errorKey(detail)}` as Key;
}

type Sent<T> = { ok: true; data: T } | { ok: false; detail: string; redirecting?: true };

/** A JSON call to the studio's calendar handler. A 401 (the partner session ran out) reloads through /auth/refresh,
 *  like the CRM's bffSend; the caller then shows no error. */
async function studioSend<T>(method: "GET" | "POST", rest: string, body?: unknown): Promise<Sent<T>> {
  let res: Response;
  try {
    res = await fetch(studioCalendarUrl(rest), {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    return { ok: false, detail: "network" };
  }
  if (res.status === 401) {
    window.location.assign(`/auth/refresh?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    return { ok: false, detail: "session_expired", redirecting: true };
  }
  const json = (await res.json().catch(() => null)) as unknown;
  if (!res.ok) {
    const detail = (json as { detail?: unknown } | null)?.detail;
    return { ok: false, detail: typeof detail === "string" ? detail : "error" };
  }
  return { ok: true, data: json as T };
}

export function useCalendarConnections() {
  const q = useQuery({
    queryKey: ["integration", "calendar"],
    queryFn: async () => {
      const res = await studioSend<CalendarConnectionsView>("GET", "");
      return res.ok ? res.data : null;
    },
  });
  return { view: q.data ?? null, loading: q.isLoading, refresh: q.refetch };
}

export function CalendarSyncCard() {
  const t = useT(CALENDAR_SYNC_MESSAGES);
  const { formatDateTime } = useFormat();
  const router = useRouter();
  const { view, loading, refresh } = useCalendarConnections();
  const [message, setMessage] = useState<Message | null>(null);
  const [busy, setBusy] = useState(false);
  const [scope, setScope] = useState<CalendarScope>("mine");
  const [confirm, setConfirm] = useState<CalendarConnection | null>(null);
  const handled = useRef(false);

  async function call<T>(path: string, body?: unknown): Promise<T | null> {
    setBusy(true);
    setMessage(null);
    const res = await studioSend<T>("POST", path, body);
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setMessage({ ok: false, key: errKey(res.detail) });
      return null;
    }
    return res.data;
  }

  // The OAuth return: complete once, then drop the code from the address bar.
  useEffect(() => {
    if (handled.current) return;
    handled.current = true;
    const back = calendarReturn(new URLSearchParams(window.location.search));
    if (!back) return;
    void (async () => {
      if ("error" in back) {
        await Promise.resolve();
        setMessage({ ok: false, key: back.error === "denied" ? "err_denied" : "err_generic" });
      } else if (await call("oauth/complete", back)) {
        setMessage({ ok: true, key: "connected" });
        void refresh();
      }
      router.replace(CALENDAR_RETURN);
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function connect(provider: CalendarProvider) {
    const data = await call<{ authorize_url?: string }>("oauth/start", { provider, scope });
    if (data?.authorize_url) window.location.assign(data.authorize_url);
  }

  async function disconnect(c: CalendarConnection) {
    const done = await call(`${c.calendar_connection_id}/disconnect`);
    setConfirm(null);
    if (done) {
      setMessage({ ok: true, key: "disconnected" });
      void refresh();
    }
  }

  if (loading && !view) return <SectionSkeleton label={t("loading")} />;
  // No CRM role in the studio's agency (or no CRM there): nothing to connect; say why. A finished OAuth return still
  // shows its message.
  if (view?.available === false) {
    return (
      <div style={{ display: "grid", gap: 10, minWidth: 0 }} data-testid="calendar-sync">
        <p style={{ margin: 0, fontSize: 13, color: K.body }} data-testid="calendar-no-crm">{t("noCrm")}</p>
        {message && (
          <p role="status" data-testid="calendar-message"
            style={{ margin: 0, fontSize: 13, color: message.ok ? K.ink : K.danger }}>{t(message.key)}</p>
        )}
      </div>
    );
  }

  const row = (label: string, value: React.ReactNode, testId?: string) => (
    <>
      <dt style={{ color: K.muted }}>{label}</dt>
      <dd style={{ margin: 0, overflowWrap: "anywhere" }} data-testid={testId}>{value}</dd>
    </>
  );

  function providerBlock(provider: CalendarProvider) {
    const c = connectionOf(view, provider);
    const available = !!view?.providers[provider];
    if (!c) {
      return (
        <div key={provider} style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}
          data-testid={`calendar-${provider}`}>
          <strong style={{ fontSize: 13, minWidth: 180 }}>{t(provider)}</strong>
          {available ? (
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void connect(provider)}
              testId={`calendar-${provider}-connect`}>{t("connect")}</Button>
          ) : (
            <span style={{ fontSize: 12, color: K.muted }} data-testid={`calendar-${provider}-unavailable`}>
              {t("notAvailable")}
            </span>
          )}
        </div>
      );
    }
    const health = connectionHealth(c);
    return (
      <div key={provider} style={{ display: "grid", gap: 8 }} data-testid={`calendar-${provider}`}>
        <strong style={{ fontSize: 13 }}>{t(provider)}</strong>
        <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 14px", margin: 0, fontSize: 13 }}>
          {row(t("connectedAs"), c.address, `calendar-${provider}-address`)}
          {row(t("status"), <Badge tone={HEALTH_TONE[health]}>{t(HEALTH_KEY[health])}</Badge>, `calendar-${provider}-status`)}
          {row(t("scope"), t(c.scope === "agency" ? "scopeAgency" : "scopeMine"))}
          {row(t("lastPush"), c.last_ok_at ? formatDateTime(c.last_ok_at) : t("never"))}
          {row(t("events"), String(c.event_count))}
        </dl>
        {c.status === "active" && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {health === "needs_reconnect" && available && (
              <Button size="sm" variant="primary" disabled={busy} onClick={() => void connect(provider)}>
                {t("reconnect")}
              </Button>
            )}
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => setConfirm(c)}
              testId={`calendar-${provider}-disconnect`}>{t("disconnect")}</Button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 14, minWidth: 0 }} data-testid="calendar-sync">
      <p style={{ margin: 0, fontSize: 13, color: K.body }}>{t("intro")}</p>
      <Field label={t("scope")}>
        <Select value={scope} onChange={(e) => setScope(e.target.value as CalendarScope)} disabled={busy}
          options={[{ value: "mine", label: t("scopeMine") }, { value: "agency", label: t("scopeAgency") }]} />
      </Field>
      {CALENDAR_PROVIDERS.map(providerBlock)}
      {message && (
        <p role="status" data-testid="calendar-message"
          style={{ margin: 0, fontSize: 13, color: message.ok ? K.ink : K.danger }}>{t(message.key)}</p>
      )}
      <ConfirmModal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        onConfirm={() => confirm && void disconnect(confirm)}
        title={t("confirmTitle")}
        body={t("confirmBody")}
        confirmLabel={t("confirmLabel")}
        destructive
        busy={busy}
        testId="calendar-disconnect-confirm"
      />
    </div>
  );
}
