"use client";
// web/components/campaigns/UnsubscribePage.tsx — the body of C-11 (AA-968): the agency's name, whether the client gets
// its news, and one button: Unsubscribe, or Subscribe again once out. Calls /crm/public/v1/unsubscribe/<token> on the
// page's own origin with no credentials; an unknown or dead link reads "not valid any more". `dict` holds only the
// unsub.* strings (lib/unsubscribeMessages.ts).
import { useEffect, useState } from "react";
import { Button, K, Spinner } from "@/app/_kit";
import type { MessageKey } from "@/lib/i18n";

type State = { agency: string; subscribed: boolean };

export function UnsubscribePage({ token, dict }: { token: string; dict: Partial<Record<MessageKey, string>> }) {
  const tr = (key: string, vars: Record<string, string> = {}) =>
    Object.entries(vars).reduce((s, [k, v]) => s.replace(`{${k}}`, v), dict[key as MessageKey] ?? key);
  const url = `/crm/public/v1/unsubscribe/${encodeURIComponent(token)}`;
  const [state, setState] = useState<State | null>(null);
  const [problem, setProblem] = useState<"notFound" | "error" | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "omit" })
      .then(async (res) => {
        if (!live) return;
        if (res.status === 404) return setProblem("notFound");
        if (!res.ok) return setProblem("error");
        setState((await res.json()) as State);
      })
      .catch(() => live && setProblem("error"));
    return () => {
      live = false;
    };
  }, [url]);

  async function change(path: string) {
    setBusy(true);
    try {
      const res = await fetch(path, { method: "POST", headers: { Accept: "application/json" }, credentials: "omit" });
      if (res.status === 404) setProblem("notFound");
      else if (!res.ok) setProblem("error");
      else setState((await res.json()) as State);
    } catch {
      setProblem("error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-testid="unsubscribe" style={{ display: "grid", gap: 12, background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: 20 }}>
      <h1 style={{ margin: 0, fontSize: 18 }}>{tr("unsub.title")}</h1>
      {problem === "notFound" && <p role="alert" style={{ margin: 0 }}>{tr("unsub.notFound")}</p>}
      {problem === "error" && <p role="alert" style={{ margin: 0, color: K.danger }}>{tr("unsub.error")}</p>}
      {!state && !problem && (
        <span style={{ display: "flex", gap: 8, alignItems: "center", color: K.muted }}>
          <Spinner /> {tr("unsub.loading")}
        </span>
      )}
      {state && problem !== "notFound" && (
        <>
          <p data-testid="unsubscribe-state" data-subscribed={state.subscribed ? "yes" : "no"} style={{ margin: 0 }}>
            {tr(state.subscribed ? "unsub.subscribed" : "unsub.unsubscribed", { agency: state.agency })}
          </p>
          <div>
            {state.subscribed ? (
              <Button variant="primary" disabled={busy} onClick={() => void change(url)}>
                {tr("unsub.button")}
              </Button>
            ) : (
              <Button variant="secondary" disabled={busy} onClick={() => void change(`${url}/resubscribe`)}>
                {tr("unsub.resubscribe")}
              </Button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
