"use client";
// web/components/intake/PlanningPage.tsx — the client side of C-02 (AA-943): loads the planning form of a signed link
// from the API's public route on this origin, then renders the paged form (PlanningForm) and posts the answers back
// (202). An unknown, expired or withdrawn link answers 404 `link_not_found`, worded the same for every case.
import { useEffect, useState } from "react";
import { K } from "@/app/_kit";
import { PlanningForm, type SubmitResult } from "@/components/intake/PlanningForm";
import { fmt, type MessageKey } from "@/lib/i18n";
import type { Answers, PublicPlanForm } from "@/lib/planningForm";

type Load = { state: "loading" } | { state: "ready"; form: PublicPlanForm } | { state: "failed"; status: number };

export function PlanningPage({ token, dict }: { token: string; dict: Partial<Record<MessageKey, string>> }) {
  const t = (k: MessageKey) => dict[k] ?? k;
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const url = `/crm/public/v1/s/${encodeURIComponent(token)}`;

  useEffect(() => {
    let live = true;
    fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "omit" })
      .then(async (res) => {
        if (!live) return;
        setLoad(res.ok ? { state: "ready", form: (await res.json()) as PublicPlanForm } : { state: "failed", status: res.status });
      })
      .catch(() => live && setLoad({ state: "failed", status: 0 }));
    return () => {
      live = false;
    };
  }, [url]);

  async function submit(body: Answers): Promise<SubmitResult> {
    try {
      const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "omit" });
      if (res.status === 202) return { ok: true };
      const payload = (await res.json().catch(() => ({}))) as { detail?: unknown };
      return { ok: false, status: res.status, detail: typeof payload.detail === "string" ? payload.detail : "error", body: payload };
    } catch {
      return { ok: false, status: 0, detail: "network" };
    }
  }

  if (load.state === "loading") return <p style={{ margin: 0, fontSize: 14, color: K.muted }}>{t("plan.loading")}</p>;
  if (load.state === "failed") {
    return (
      <p data-testid="plan-unavailable" data-status={load.status} style={{ margin: 0, fontSize: 14, color: K.body }}>
        {t(load.status === 404 ? "plan.notFound" : load.status === 429 ? "plan.rateLimited" : "plan.error")}
      </p>
    );
  }
  const { form } = load;
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <h1 style={{ margin: 0, fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>{fmt(t("plan.title"), { agency: form.agency_name })}</h1>
      <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{fmt(t("plan.intro"), { agency: form.agency_name })}</p>
      {form.submitted && (
        <p data-testid="plan-already" style={{ margin: 0, fontSize: 13, color: K.body }}>
          {t("plan.already")}
        </p>
      )}
      <PlanningForm pages={form.pages} tripStyle={form.trip_style} initial={form.prefill} agency={form.agency_name} mode="client" dict={dict} onSubmit={submit} />
    </div>
  );
}
