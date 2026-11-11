"use client";
// web/components/partners/PartnerView.tsx — AA-1071 (crm-056): a sales partner's own page on the client host (/c/a,
// after the /c/a/<token> landing set the host-only link cookie). Not signed in: the agency's name and "Send me a code"
// (6 digits to the partner's own e-mail, from the agency's mailbox; the Dev echo shows it); then the code. Signed in:
// the trips the partner referred (short reference, when, travellers, a stage in four words, the commission with its
// status) and the totals per currency; never a traveller's name or contact. The browser talks to /crm/public/v1/partner
// on this origin with the cookies; the page never holds the token or the session.
import { useCallback, useEffect, useState } from "react";
import { Badge, Button, DataTable, Field, Input, K, KpiTile } from "@/app/_kit";
import type { ColumnDef } from "@tanstack/react-table";
import { type DateLocale, formatDate } from "@/lib/dates";
import { fmt, type Lang } from "@/lib/i18n";
import { PARTNER_API, type PartnerView as View, partnerPageErrorKey, STATUS_TONE } from "@/lib/partners";
import { formatAmount } from "@/lib/proposals";

type Dict = Record<string, string>;
type Trip = NonNullable<View["trips"]>[number];

type Loaded = { state: "ready" | "gone" | "error"; view?: View };

async function fetchView(): Promise<Loaded> {
  try {
    const res = await fetch(PARTNER_API, { headers: { Accept: "application/json" }, cache: "no-store" });
    if (res.status === 404) return { state: "gone" };
    if (!res.ok) return { state: "error" };
    return { state: "ready", view: (await res.json()) as View };
  } catch {
    return { state: "error" };
  }
}

const small: React.CSSProperties = { display: "block", fontSize: 12, color: K.muted };

export function PartnerView({ dict, locale }: { dict: Dict; lang: Lang; locale: DateLocale }) {
  const tr = (key: string) => dict[key] ?? key;
  const [view, setView] = useState<View | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "gone" | "error">("loading");
  const [sent, setSent] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const apply = useCallback((r: Loaded) => {
    if (r.view) setView(r.view);
    setState(r.state);
  }, []);
  const load = useCallback(async () => apply(await fetchView()), [apply]);

  useEffect(() => {
    let live = true;
    void fetchView().then((r) => {
      if (live) apply(r);
    });
    return () => {
      live = false;
    };
  }, [apply]);

  async function post(path: string, body?: unknown): Promise<{ status: number; data: Record<string, unknown> }> {
    const res = await fetch(`${PARTNER_API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: res.status, data: (await res.json().catch(() => ({}))) as Record<string, unknown> };
  }

  async function sendCode() {
    setBusy(true);
    setMessage(null);
    const { status, data } = await post("/code");
    setBusy(false);
    if (status !== 202) {
      setMessage(tr(partnerPageErrorKey(String(data.detail ?? ""))));
      return;
    }
    const parts = [data.to_hint ? fmt(tr("partnerPage.codeSent"), { to: String(data.to_hint) }) : null, data.dev_code ? fmt(tr("partnerPage.codeSentDev"), { code: String(data.dev_code) }) : null];
    setSent(parts.filter(Boolean).join(" "));
  }

  async function signIn() {
    setBusy(true);
    setMessage(null);
    const { status, data } = await post("/session", { code: code.trim() });
    setBusy(false);
    if (status === 200) {
      setCode("");
      setSent(null);
      await load();
      return;
    }
    if (data.detail === "code_invalid") setMessage(fmt(tr("partnerPage.codeInvalid"), { n: Number(data.attempts_left ?? 0) }));
    else setMessage(tr(partnerPageErrorKey(String(data.detail ?? ""))));
  }

  async function signOut() {
    await post("/sign-out");
    await load();
  }

  if (state === "loading") return <p style={{ margin: 0, fontSize: 13, color: K.muted }}>…</p>;
  if (state !== "ready" || !view) {
    return (
      <p data-testid="partner-page-unavailable" style={{ margin: 0, fontSize: 14, color: K.body }}>
        {tr(state === "gone" ? "partnerPage.notFound" : "partnerPage.error")}
      </p>
    );
  }
  const money = (amount: string | null | undefined, currency: string) => formatAmount(amount, currency, locale);
  if (!view.signed_in) {
    return (
      <div data-testid="partner-sign-in" style={{ display: "grid", gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink }}>{fmt(tr("partnerPage.title"), { agency: view.agency ?? "" })}</h1>
        <strong>{tr("partnerPage.signInTitle")}</strong>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{tr("partnerPage.signInBody")}</p>
        <div>
          <Button onClick={sendCode} disabled={busy} testId="partner-send-code">
            {tr("partnerPage.sendCode")}
          </Button>
        </div>
        {sent && (
          <p data-testid="partner-code-sent" style={{ margin: 0, fontSize: 13 }}>
            {sent}
          </p>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void signIn();
          }}
          style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}
        >
          <Field label={tr("partnerPage.code")}>
            <Input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={6} data-testid="partner-code" />
          </Field>
          <Button type="submit" disabled={busy || code.trim().length !== 6} testId="partner-sign-in-button">
            {tr("partnerPage.signIn")}
          </Button>
        </form>
        {message && (
          <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
            {message}
          </p>
        )}
      </div>
    );
  }

  const trips = view.trips ?? [];
  const columns: ColumnDef<Trip, unknown>[] = [
    {
      id: "ref",
      header: tr("partnerPage.col.ref"),
      accessorFn: (r) => r.received_on,
      cell: ({ row }) => (
        <span data-testid="partner-trip" style={{ display: "block" }}>
          <strong>{row.original.ref}</strong>
          <span style={small}>{fmt(`${tr("partnerPage.col.received")}: {d}`, { d: formatDate(row.original.received_on, locale) })}</span>
        </span>
      ),
    },
    {
      id: "when",
      header: tr("partnerPage.col.when"),
      enableSorting: false,
      cell: ({ row }) => (
        <span style={{ display: "block" }}>
          {row.original.start_month ?? "—"}
          <span style={small}>
            {tr("partnerPage.col.travellers")}: {row.original.travellers}
          </span>
        </span>
      ),
    },
    {
      id: "stage",
      header: tr("partnerPage.col.stage"),
      enableSorting: false,
      cell: ({ row }) => <Badge tone={row.original.stage === "closed" ? "neutral" : "info"}>{tr(`partnerPage.stage.${row.original.stage}`)}</Badge>,
    },
    {
      id: "commission",
      header: tr("partnerPage.col.commission"),
      enableSorting: false,
      cell: ({ row }) => {
        const c = row.original.commission;
        if (!c) return <span>—</span>;
        return (
          <span style={{ display: "grid", gap: 4, justifyItems: "start" }}>
            <strong>{money(c.amount, c.currency)}</strong>
            <Badge tone={STATUS_TONE[c.status]}>{tr(`partners.pcStatus.${c.status}`)}</Badge>
          </span>
        );
      },
    },
  ];
  return (
    <div data-testid="partner-view" style={{ display: "grid", gap: 14, minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>{fmt(tr("partnerPage.title"), { agency: view.agency ?? "" })}</h1>
        <Button size="sm" variant="ghost" onClick={signOut} testId="partner-sign-out">
          {tr("partnerPage.signOut")}
        </Button>
      </div>
      {view.partner_name && <p style={{ margin: 0, fontSize: 13.5 }}>{fmt(tr("partnerPage.hello"), { name: view.partner_name })}</p>}
      {(view.totals ?? []).length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
          {(view.totals ?? []).flatMap((x) => [
            <KpiTile key={`${x.currency}-open`} size="sm" label={tr("partnerPage.totals.open")} value={money(x.open, x.currency)} />,
            <KpiTile key={`${x.currency}-paid`} size="sm" label={tr("partnerPage.totals.paid")} value={money(x.paid, x.currency)} />,
          ])}
        </div>
      )}
      {trips.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("partnerPage.empty")}</p>
      ) : (
        <DataTable tableId="partner-trips" data={trips} columns={columns} getRowId={(r) => r.ref} paginate={false} tableMinWidth={480} />
      )}
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("partnerPage.privacy")}</p>
    </div>
  );
}
