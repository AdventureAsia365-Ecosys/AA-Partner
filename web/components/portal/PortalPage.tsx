"use client";
// web/components/portal/PortalPage.tsx — the traveller portal per person (AA-946 base, card CRM-18; AA-953 card
// CRM-24a; design v3 C-05 code sign-in + "Who are you?" picker (Fix E4), C-06 home (lead vs companion), C-07 Stage 3
// + flights + passport scan, C-08 sign, C-09 trip, C-10 payments view; §2.9 rules 1-4). AA-955 (CRM-25): C-08 is the
// v1 envelope (SignEnvelope); the v0 waiver form stays for an API without the envelope. Everything goes to the API's
// routes under `base` on this origin (/crm/public/v1/traveller: the link cookie the landing route set, and the
// session cookie the API sets on the same path); the page never sees either. Stage 3 is write-only: the page shows
// which fields are on file, never their values. Before the deposit is paid, Stage 3, flights, the scan and signing
// say "opens after your deposit" (the API answers 403 stage3_locked anyway).
// AA-954 (CRM-24b, R16, §2.9 rule 7; `offline` on /c/t): the page registers the service worker that keeps its shell,
// keeps an allow-listed copy of the signed-in person's itinerary on the device (lib/offlineItinerary.ts) and shows it
// with "last updated" when the network cannot be reached; it replaces the copy on every signed-in answer and on
// reconnect (server wins), and drops it at sign-out, for a gone link and for another link's answer. Sign-out clears
// the device first, whatever the server then answers, and a load still in flight at that moment never writes back.
import { useEffect, useRef, useState } from "react";
import { Button, K, LinkButton } from "@/app/_kit";
import type { PortalPersonHome, PortalPick } from "@/lib/clientPortal";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, type MessageKey } from "@/lib/i18n";
import { clientErrorKey, formatAmount, stage3Body, STAGE3_FIELDS, type PortalHome, type Stage3Field } from "@/lib/proposals";
import { clearOffline, readOffline, syncOffline, usable, type OfflineItinerary, type OfflineSource } from "@/lib/offlineItinerary";
import { clearWorkerCaches, precacheShell, registerPortalWorker } from "@/lib/portalPwa";
import { InstallPrompt, OfflineView } from "./PortalOffline";
import { DocumentsSection, FlightsSection, PartySection, PassportScanSection, TripSection } from "./PortalPerson";
import { SignEnvelope } from "./SignEnvelope";

type Home = PortalHome & PortalPersonHome;
type Load =
  | { state: "loading" }
  | { state: "ready"; home: Home }
  | { state: "failed"; status: number }
  | { state: "offline"; copy: OfflineItinerary | null };

const box: React.CSSProperties = {
  font: "inherit",
  fontSize: 14,
  padding: "8px 10px",
  borderRadius: 8,
  border: `1px solid ${K.line}`,
  background: K.card,
  color: K.ink,
  width: "100%",
  boxSizing: "border-box",
};
const section: React.CSSProperties = { display: "grid", gap: 8, paddingTop: 12, borderTop: `1px solid ${K.line}` };
const DATE_FIELDS = new Set(["dob", "passport_expiry"]);

async function call(url: string, method: string, body?: unknown): Promise<{ ok: boolean; status: number; data: Record<string, unknown> }> {
  try {
    const res = await fetch(url, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
    return { ok: res.ok, status: res.status, data: ((await res.json().catch(() => ({}))) ?? {}) as Record<string, unknown> };
  } catch {
    return { ok: false, status: 0, data: { detail: "network" } };
  }
}

/** The offline rule for one answer of GET `base` (lib/offlineItinerary.ts): what to show, after the stored copy was
 * replaced, kept or dropped. `fresh` says no sign-out or "Remove from this device" happened since the request
 * started: when one did, this answer touches nothing on the device, and a write that raced the clear is cleared
 * again (the clear wins). Exported for the tests. */
export async function settle(
  res: { ok: boolean; status: number; data: Record<string, unknown> },
  now: Date,
  fresh: () => boolean = () => true,
): Promise<Load> {
  if (res.status === 0) {
    const stored = await readOffline();
    if (stored && !usable(stored, now)) await clearOffline(); // the link has expired since it was saved
    return { state: "offline", copy: usable(stored, now) ? stored : null };
  }
  if (fresh()) {
    await syncOffline({ status: res.status, home: res.ok ? (res.data as unknown as OfflineSource) : undefined }, now);
    if (!fresh()) await clearOffline();
    else if (res.ok && res.data.signed_in === true) void precacheShell();
  }
  return res.ok ? { state: "ready", home: res.data as unknown as Home } : { state: "failed", status: res.status };
}

export function PortalPage({
  base,
  dict,
  locale,
  offline = false,
}: {
  base: string;
  dict: Partial<Record<MessageKey, string>>;
  locale: DateLocale;
  /** AA-954: the installable page keeps the itinerary offline (the /c/t page; off for any other use). */
  offline?: boolean;
}) {
  const t = (k: string) => dict[k as MessageKey] ?? k;
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [round, setRound] = useState(0);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [picks, setPicks] = useState<PortalPick[] | null>(null);
  const [signCode, setSignCode] = useState("");
  const [signName, setSignName] = useState("");
  const [signAgree, setSignAgree] = useState(false);
  const [signAsked, setSignAsked] = useState(false);
  const [values, setValues] = useState<Partial<Record<Stage3Field, string>>>({});
  const [netDown, setNetDown] = useState(false);
  // AA-954: bumped by every clear of the device; a load that started before a clear never writes the copy back.
  const clears = useRef(0);

  useEffect(() => {
    let live = true;
    const at = clears.current;
    call(base, "GET").then(async (res) => {
      const next: Load = offline
        ? await settle(res, new Date(), () => clears.current === at)
        : res.ok
          ? { state: "ready", home: res.data as unknown as Home }
          : { state: "failed", status: res.status };
      if (live) setLoad(next);
    });
    return () => {
      live = false;
    };
  }, [base, round, offline]);

  // AA-954: the worker that keeps the page shell, and the network state (back online = load again: the server wins).
  useEffect(() => {
    if (!offline) return;
    void registerPortalWorker();
    const up = () => {
      setNetDown(false);
      setRound((r) => r + 1);
    };
    const down = () => setNetDown(true);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [offline]);

  async function forgetDevice(text?: string) {
    clears.current += 1;
    await clearOffline();
    await clearWorkerCaches();
    if (text) setNote({ tone: "success", text });
  }

  function refused(data: Record<string, unknown>, status: number): string {
    if (status === 0) return t("client.error.offline"); // AA-954: no network (every write needs one)
    const detail = typeof data.detail === "string" ? data.detail : status === 429 ? "rate_limited" : status === 413 ? "body_too_large" : "error";
    const text = t(clientErrorKey(detail));
    return typeof data.attempts_left === "number" ? fmt(text, { left: data.attempts_left }) : text;
  }

  async function step(method: string, path: string, body: unknown, done: (data: Record<string, unknown>) => void) {
    setBusy(true);
    setNote(null);
    const res = await call(`${base}${path}`, method, body);
    setBusy(false);
    if (res.ok) done(res.data);
    else setNote({ tone: "danger", text: refused(res.data, res.status) });
  }

  function devHint(data: Record<string, unknown>): string {
    const shown = typeof data.to_hint === "string" ? fmt(t("client.codeSent"), { to: data.to_hint }) : t("client.codeReady");
    return typeof data.dev_code === "string" ? `${shown} (${fmt(t("client.devCode"), { code: data.dev_code })})` : shown;
  }

  function signedIn(data: Record<string, unknown>) {
    if (Array.isArray(data.pick)) {
      setPicks(data.pick as PortalPick[]); // Fix E4: this address opens more than one traveller's page
      return;
    }
    setCode("");
    setPicks(null);
    setRound((r) => r + 1);
  }

  const saved = (text: string) => {
    setNote({ tone: "success", text });
    setRound((r) => r + 1);
  };

  // AA-954 (§2.9 rule 7): nothing of this person stays on the device after sign-out, whatever the server answers
  // (offline, a rate limit, a fault or a revoked link included). Clear first, then again once the POST is done, so a
  // load that was in flight during the POST cannot leave a copy either.
  async function signOut() {
    if (offline) await forgetDevice();
    let out = false;
    await step("POST", "/sign-out", undefined, () => {
      out = true;
    });
    if (offline) await forgetDevice();
    if (out) {
      setPicks(null);
      setRound((r) => r + 1);
    }
  }

  if (load.state === "loading") return <p style={{ margin: 0, fontSize: 14, color: K.muted }}>{t("client.loading")}</p>;
  if (load.state === "offline") {
    return (
      <>
        {note && (
          <p role="status" data-testid="portal-note" style={{ margin: "0 0 12px", fontSize: 13.5, color: K.success }}>
            {note.text}
          </p>
        )}
        <OfflineView
          copy={load.copy}
          t={t}
          locale={locale}
          onRetry={() => {
            setNote(null);
            setRound((r) => r + 1);
          }}
          onForget={() => {
            void forgetDevice(t("client.offline.forgotten")).then(() => setLoad({ state: "offline", copy: null }));
          }}
        />
      </>
    );
  }
  if (load.state === "failed") {
    return (
      <p data-testid="portal-unavailable" data-status={load.status} style={{ margin: 0, fontSize: 14, color: K.body }}>
        {t(load.status === 404 ? "client.error.link_not_found" : load.status === 429 ? "client.error.rate_limited" : "client.error.other")}
      </p>
    );
  }
  const { home } = load;
  const message = note && (
    <p role={note.tone === "danger" ? "alert" : "status"} data-testid="portal-note" style={{ margin: 0, fontSize: 13.5, color: note.tone === "danger" ? K.danger : K.success, overflowWrap: "anywhere" }}>
      {note.text}
    </p>
  );

  if (!home.signed_in) {
    return (
      <div data-testid="portal-sign-in" style={{ display: "grid", gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>{fmt(t("client.portalTitle"), { agency: home.agency_name })}</h1>
        {picks ? (
          <div data-testid="portal-picker" style={{ display: "grid", gap: 8 }}>
            <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{t("client.whoAreYou")}</h2>
            <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t("client.whoAreYouIntro")}</p>
            {picks.map((p) => (
              <Button
                key={p.traveler_id}
                variant="secondary"
                size="lg"
                disabled={busy}
                testId={`portal-pick-${p.role}`}
                onClick={() => step("POST", "/session", { code: code.trim(), traveler_id: p.traveler_id }, signedIn)}
              >
                {`${p.name ?? t(`client.role.${p.role}`)} · ${t(`client.role.${p.role}`)}`}
              </Button>
            ))}
          </div>
        ) : (
          <>
            <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t("client.signInIntro")}</p>
            <Button variant="primary" size="lg" disabled={busy} testId="portal-send-code" onClick={() => step("POST", "/code", { purpose: "sign_in" }, (d) => setHint(devHint(d)))}>
              {t("client.sendCode")}
            </Button>
            {hint && (
              <p data-testid="portal-hint" style={{ margin: 0, fontSize: 13.5, color: K.body }}>
                {hint}
              </p>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void step("POST", "/session", { code: code.trim() }, signedIn);
              }}
              style={{ display: "grid", gap: 8 }}
            >
              <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
                {t("client.code")}
                <input name="code" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} style={box} data-testid="portal-code" />
              </label>
              <Button type="submit" variant="primary" size="lg" disabled={busy || code.trim().length !== 6} testId="portal-verify">
                {t("client.signIn")}
              </Button>
            </form>
          </>
        )}
        {message}
      </div>
    );
  }

  const stage3 = home.stage3!;
  const missing = new Set(stage3.missing);
  const waiver = home.waiver;
  return (
    <div data-testid="portal-home" data-role={home.role} style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "grid", gap: 4 }}>
        <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{fmt(t("client.preparedBy"), { agency: home.agency_name })}</p>
        <h1 style={{ margin: 0, fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>{home.trip?.title}</h1>
        {home.trip?.start_date && (
          <p style={{ margin: 0, fontSize: 13, color: K.body }}>
            {formatDate(home.trip.start_date, locale)}
            {home.trip.end_date ? ` – ${formatDate(home.trip.end_date, locale)}` : ""}
          </p>
        )}
        {home.me && (
          <p style={{ margin: 0, fontSize: 13.5, color: K.body }} data-testid="portal-me" data-traveller={home.me.traveler_id}>
            {fmt(t("client.signedInAs"), { name: home.me.name ?? t(`client.role.${home.me.role}`) })}
            {(home.switch_to ?? []).map((p) => (
              <span key={p.traveler_id}>
                {" · "}
                <LinkButton
                  size="inherit"
                  disabled={busy}
                  testId={`portal-switch-${p.role}`}
                  onClick={() => step("POST", "/switch", { traveler_id: p.traveler_id }, () => setRound((r) => r + 1))}
                >
                  {fmt(t("client.switchTo"), { name: p.name ?? t(`client.role.${p.role}`) })}
                </LinkButton>
              </span>
            ))}
          </p>
        )}
      </div>
      {message}
      {offline && netDown && (
        <p role="status" data-testid="portal-offline-banner" style={{ margin: 0, fontSize: 13.5, color: K.body, padding: "8px 10px", border: `1px solid ${K.line}`, borderRadius: 8 }}>
          {t("client.offline.banner")}
        </p>
      )}
      {offline && <InstallPrompt t={t} />}

      <TripSection days={home.itinerary ?? []} services={home.services ?? []} t={t} locale={locale} />
      {offline && (home.itinerary?.length || home.services?.length) ? (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }} data-testid="portal-offline-kept">
          {t("client.offline.kept")}
        </p>
      ) : null}

      <section data-testid="portal-stage3" data-locked={stage3.locked ? "true" : "false"} data-done={stage3.done ? "true" : "false"} style={section}>
        <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{t("client.myDetails")}</h2>
        {stage3.locked ? (
          <p style={{ margin: 0, fontSize: 14, color: K.body }} data-testid="portal-stage3-locked">
            {t("client.stage3Locked")}
          </p>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const body = stage3Body(values);
              if (!Object.keys(body).length) return;
              void step("PUT", "/stage3", body, () => {
                setValues({});
                saved(t("client.detailsSaved"));
              });
            }}
            style={{ display: "grid", gap: 8 }}
          >
            <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t("client.writeOnly")}</p>
            {STAGE3_FIELDS.map((f) => (
              <label key={f} style={{ display: "grid", gap: 4, fontSize: 14 }}>
                <span>
                  {t(`client.field.${f}`)}{" "}
                  {!missing.has(f) && <span style={{ fontSize: 12, color: K.success }}>{t("client.onFile")}</span>}
                </span>
                <input
                  name={f}
                  type={DATE_FIELDS.has(f) ? "date" : "text"}
                  maxLength={f === "nationality" || f === "passport_country" ? 2 : 300}
                  value={values[f] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [f]: e.target.value }))}
                  style={box}
                  data-testid={`portal-field-${f}`}
                />
              </label>
            ))}
            <Button type="submit" variant="primary" size="lg" disabled={busy} testId="portal-stage3-save">
              {t("client.saveDetails")}
            </Button>
          </form>
        )}
      </section>

      {home.flights !== undefined && <FlightsSection flights={home.flights} locked={stage3.locked} busy={busy} step={step} t={t} locale={locale} onSaved={saved} />}
      <PassportScanSection onFile={Boolean(home.passport_scan_on_file)} locked={stage3.locked} busy={busy} step={step} t={t} onSaved={saved} />

      {home.agreements ? (
        <SignEnvelope
          box={home.agreements}
          locked={stage3.locked}
          busy={busy}
          step={step}
          hint={hint}
          onCode={(d) => setHint(devHint(d))}
          onSigned={() => saved(t("client.signedThanks"))}
          t={t}
          locale={locale}
        />
      ) : (
        waiver && (
          <section data-testid="portal-waiver" data-signed={waiver.signed ? "true" : "false"} style={section}>
            <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{fmt(t("client.waiverTitle"), { version: waiver.version_label })}</h2>
            <p style={{ margin: 0, fontSize: 13.5, color: K.body, whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 260, overflowY: "auto" }}>{waiver.body_text}</p>
            {waiver.signed ? (
              <p style={{ margin: 0, fontSize: 14, color: K.success }} data-testid="portal-signed">
                {fmt(t("client.signedOn"), { date: waiver.signed_at ? formatDate(waiver.signed_at.slice(0, 10), locale) : "" })}
              </p>
            ) : stage3.locked ? (
              <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t("client.stage3Locked")}</p>
            ) : (
              <div style={{ display: "grid", gap: 8 }}>
                <Button
                  variant="secondary"
                  size="lg"
                  disabled={busy}
                  testId="portal-sign-code"
                  onClick={() =>
                    step("POST", "/code", { purpose: "sign" }, (d) => {
                      setSignAsked(true);
                      setHint(devHint(d));
                    })
                  }
                >
                  {t("client.sendSignCode")}
                </Button>
                {signAsked && hint && (
                  <p data-testid="portal-sign-hint" style={{ margin: 0, fontSize: 13.5, color: K.body }}>
                    {hint}
                  </p>
                )}
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void step(
                      "POST",
                      "/sign",
                      { code: signCode.trim(), full_name: signName, agree: signAgree, doc_id: waiver.doc_id, doc_sha256: waiver.body_sha256 },
                      () => saved(t("client.signedThanks")),
                    );
                  }}
                  style={{ display: "grid", gap: 8 }}
                >
                  <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 14 }}>
                    <input type="checkbox" checked={signAgree} onChange={(e) => setSignAgree(e.target.checked)} data-testid="portal-sign-agree" />
                    <span>{t("client.signAgree")}</span>
                  </label>
                  <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
                    {t("client.fullName")}
                    <input autoComplete="name" value={signName} onChange={(e) => setSignName(e.target.value)} style={box} data-testid="portal-sign-name" />
                  </label>
                  <label style={{ display: "grid", gap: 4, fontSize: 14 }}>
                    {t("client.code")}
                    <input inputMode="numeric" maxLength={6} value={signCode} onChange={(e) => setSignCode(e.target.value)} style={box} data-testid="portal-sign-code-input" />
                  </label>
                  <Button type="submit" variant="primary" size="lg" disabled={busy || !signAgree || !signName.trim() || signCode.trim().length !== 6} testId="portal-sign">
                    {t("client.sign")}
                  </Button>
                </form>
              </div>
            )}
          </section>
        )
      )}

      {home.documents !== undefined && (
        <DocumentsSection base={base} documents={home.documents} t={t} locale={locale} onError={(status) => setNote({ tone: "danger", text: refused({}, status) })} />
      )}

      {home.party && (
        <PartySection party={home.party} busy={busy} step={step} t={t} onInvited={(text) => saved(text)} />
      )}

      {home.payments && home.payments.length > 0 && (
        <section data-testid="portal-payments" style={section}>
          <h2 style={{ margin: 0, fontSize: 16, color: K.ink }}>{t("client.paymentsTitle")}</h2>
          {home.payments.map((p) => (
            <p key={p.kind} style={{ margin: 0, fontSize: 13.5, color: K.body }}>
              {fmt(t(`client.scheduleRow.${p.status}`), { what: t(`client.kind.${p.kind}`), amount: formatAmount(p.amount, p.currency, locale), date: formatDate(p.due_on, locale) })}
            </p>
          ))}
        </section>
      )}

      <div>
        <LinkButton
          size="inherit"
          disabled={busy}
          testId="portal-sign-out"
          onClick={() => void signOut()}
        >
          {t("client.signOut")}
        </LinkButton>
      </div>
    </div>
  );
}
