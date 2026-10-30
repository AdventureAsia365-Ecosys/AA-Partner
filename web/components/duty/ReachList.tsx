"use client";
// web/components/duty/ReachList.tsx — "Reach travellers here" for one region (AA-961, card CRM-30; design v3 R37):
// the region's travellers without names (a short reference, their trips, which contact fields exist). The member
// ticks travellers (the kit DataTable selection) and fields and opens them in one POST /bff/duty/reveal: one audit event per traveller; above
// REASON_THRESHOLD travellers a reason is required (card R), and the API's hourly limit answers 429. The opened values
// stay in this component's state only (never in the URL or storage) and are gone on the next page load. Nothing is
// sent to a traveller from here: the agent contacts them by hand. Health notes open one by one on the client page.
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { Badge, Button, Checkbox, DataTable, Field, K, Select, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import {
  BULK_REASONS,
  MAX_BULK,
  REASON_THRESHOLD,
  needsReason,
  revealErrorKey,
  type BulkField,
  type DutyTraveller,
  type RevealedTraveller,
} from "@/lib/duty";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";

const FIELDS: readonly BulkField[] = ["display_name", "phone", "email", "emergency_contact"];

export function ReachList({
  travellers,
  tripTitles,
  lang,
}: {
  travellers: DutyTraveller[];
  tripTitles: Record<string, string>;
  lang: Lang;
}) {
  const tr = (key: string) => t(lang, key as MessageKey);
  const hydrated = useHydrated();
  const [fields, setFields] = useState<Set<BulkField>>(new Set(["display_name", "phone"]));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<MessageKey | null>(null);
  const [opened, setOpened] = useState<Record<string, RevealedTraveller>>({});

  async function open(ids: string[]) {
    setBusy(true);
    setError(null);
    const body: Record<string, unknown> = { traveler_ids: ids, fields: FIELDS.filter((f) => fields.has(f)) };
    if (reason) body.reason = reason;
    const res = await bffSend<{ travellers: RevealedTraveller[] }>("POST", "duty/reveal", body);
    setBusy(false);
    if (!res.ok) {
      setError(revealErrorKey(res.status, res.detail));
      return;
    }
    setOpened((prev) => {
      const next = { ...prev };
      for (const p of res.data.travellers) next[p.traveler_id] = { ...next[p.traveler_id], ...p };
      return next;
    });
  }

  const columns = useMemo<ColumnDef<DutyTraveller, unknown>[]>(
    () => [
      {
        id: "traveller",
        header: t(lang, "duty.reach.traveller"),
        enableSorting: false,
        cell: ({ row }) => {
          const p = row.original;
          const seen = opened[p.traveler_id];
          return (
            <div data-testid={`duty-traveller-${p.traveler_id}`} style={{ minWidth: 0 }}>
              {seen?.display_name ? (
                <strong data-testid="duty-revealed-name">{seen.display_name}</strong>
              ) : (
                <span style={{ color: K.muted }}>
                  {t(lang, "duty.reach.hidden")} · <code>{p.traveler_id.slice(0, 8)}</code>
                </span>
              )}
              {(["phone", "email", "emergency_contact"] as const).map((f) =>
                seen && seen[f] !== undefined ? (
                  <div key={f} style={{ fontSize: 12.5 }}>
                    <span style={{ color: K.muted }}>{t(lang, `duty.reveal.field.${f}` as MessageKey)}: </span>
                    <code style={{ overflowWrap: "anywhere" }}>{seen[f] ?? "—"}</code>
                  </div>
                ) : null,
              )}
            </div>
          );
        },
      },
      {
        id: "trips",
        header: t(lang, "duty.reach.trips"),
        enableSorting: false,
        cell: ({ row }) =>
          row.original.trip_ids.map((id) => (
            <div key={id}>
              <a href={`/pipeline/${id}`}>{tripTitles[id] ?? id.slice(0, 8)}</a>
            </div>
          )),
      },
      {
        id: "on_file",
        header: t(lang, "duty.reach.has"),
        enableSorting: false,
        cell: ({ row }) => {
          const p = row.original;
          return (
            <div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {p.has_phone && <Badge dot={false}>{t(lang, "duty.reveal.field.phone")}</Badge>}
                {p.has_email && <Badge dot={false}>{t(lang, "duty.reveal.field.email")}</Badge>}
                {p.has_emergency_contact && (
                  <Badge dot={false} tone="info">
                    {t(lang, "duty.reveal.field.emergency_contact")}
                  </Badge>
                )}
              </div>
              {p.has_health_info && (
                <div style={{ fontSize: 12, color: K.muted, marginTop: 4 }}>
                  <a href={`/clients/${p.traveler_id}`}>{t(lang, "duty.reach.health")}</a>
                </div>
              )}
            </div>
          );
        },
      },
    ],
    [lang, opened, tripTitles],
  );

  return (
    <div data-testid="duty-reach" style={{ display: "grid", gap: 10, minWidth: 0 }}>
      <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("duty.reach.body")}</p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
        <Field as="div" label={tr("duty.reveal.fields")}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
            {FIELDS.map((f) => (
              <Checkbox
                key={f}
                label={tr(`duty.reveal.field.${f}`)}
                checked={fields.has(f)}
                disabled={!hydrated}
                onChange={() =>
                  setFields((prev) => {
                    const next = new Set(prev);
                    if (next.has(f)) next.delete(f);
                    else next.add(f);
                    return next;
                  })
                }
              />
            ))}
          </div>
        </Field>
        <Field label={tr("duty.reveal.reason")} hint={fmt(tr("duty.reveal.reasonNote"), { n: REASON_THRESHOLD })}>
          <Select
            size="sm"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={tr("duty.reveal.reason.pick")}
            options={BULK_REASONS.map((r) => ({ value: r, label: tr(`duty.reveal.reason.${r}`) }))}
          />
        </Field>
      </div>
      <DataTable<DutyTraveller>
        data={travellers}
        columns={columns}
        tableId="duty-reach"
        getRowId={(p) => p.traveler_id}
        searchable={false}
        enableSelection
        paginate={false}
        compact
        emptyTitle={tr("duty.empty.title")}
        bulkActions={({ selectedRows }) => {
          const ids = selectedRows.map((p) => p.traveler_id);
          const ready =
            hydrated && !busy && ids.length > 0 && ids.length <= MAX_BULK && fields.size > 0 && (!needsReason(ids.length) || reason !== "");
          return (
            <span data-testid="duty-reveal-open" style={{ display: "contents" }}>
              <Button variant="primary" size="sm" disabled={!ready} onClick={() => open(ids)}>
                {fmt(tr("duty.reveal.open"), { n: ids.length })}
              </Button>
            </span>
          );
        }}
      />
      <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("duty.reveal.logged")}</p>
      {error && (
        <p role="alert" data-testid="duty-reveal-error" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {tr(error)}
        </p>
      )}
    </div>
  );
}
