"use client";
// web/components/staff/BreakglassStart.tsx — S-04 emergency access, L3 break-glass (AA-1019 on the AA-971 API), group
// crm-ops: the agency picked by NAME (GET breakglass/agencies, never a typed id), a reason code, a ticket and 15 to 60
// minutes, then a confirmation that says what happens (the owners are e-mailed, AA ops alerted, names stay hidden).
// No owner permission is needed, so the confirmation is the speed bump. An agency where the caller already holds a
// live session just opens.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, ConfirmModal, Field, Input, K, Select, useHydrated } from "@/app/_kit";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import {
  BREAKGLASS_MINUTES_MAX,
  BREAKGLASS_MINUTES_MIN,
  BREAKGLASS_REASONS,
  minutesValid,
  refusalKey,
  ticketValid,
  type BreakglassAgency,
  type BreakglassReason,
  type SessionView,
} from "@/lib/staffConsole";
import { staffSend } from "@/lib/staffClient";

export function BreakglassStart({ agencies, lang, locale }: { agencies: BreakglassAgency[]; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [tenantId, setTenantId] = useState("");
  const [reason, setReason] = useState<BreakglassReason | "">("");
  const [ticket, setTicket] = useState("");
  const [minutes, setMinutes] = useState(String(BREAKGLASS_MINUTES_MAX));
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const agency = agencies.find((a) => a.tenant_id === tenantId) ?? null;
  const n = Number(minutes);

  if (agencies.length === 0) {
    return <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("staff.breakglass.none")}</p>;
  }

  function check(): string | null {
    if (!agency) return tr("staff.breakglass.pickAgency");
    if (!reason) return tr("staff.breakglass.reason");
    if (!ticketValid(ticket)) return tr("staff.support.ticketInvalid");
    if (!minutesValid(n)) return tr("staff.breakglass.minutes");
    return null;
  }

  async function start() {
    setBusy(true);
    setError(null);
    const res = await staffSend<SessionView>("POST", "breakglass/sessions", {
      body: { tenant_id: tenantId, ticket_ref: ticket.trim(), reason_code: reason, minutes: n },
    });
    setBusy(false);
    if (!res.ok) {
      setAsking(false);
      setError(tr(refusalKey(res.detail)));
      return;
    }
    router.push(`/staff/support/${tenantId}`);
  }

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="staff-breakglass" data-hydrated={hydrated ? "true" : undefined}>
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{tr("staff.breakglass.help")}</p>
      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", alignItems: "end" }}>
        <Field label={tr("staff.breakglass.agency")} required>
          <Select
            value={tenantId}
            onChange={(e) => setTenantId(e.currentTarget.value)}
            placeholder={tr("staff.breakglass.pickAgency")}
            options={agencies.map((a) => ({
              value: a.tenant_id,
              label: `${a.agency_name ?? a.tenant_id.slice(0, 8)}${a.agency_status === "locked" ? ` (${tr("staff.ops.locked")})` : ""}`,
            }))}
            style={{ maxWidth: "100%", minWidth: 0 }}
          />
        </Field>
        <Field label={tr("staff.breakglass.reason")} required>
          <Select
            value={reason}
            onChange={(e) => setReason(e.currentTarget.value as BreakglassReason)}
            placeholder={tr("staff.breakglass.reason")}
            options={BREAKGLASS_REASONS.map((r) => ({ value: r, label: tr(`staff.breakglass.reason.${r}`) }))}
            style={{ maxWidth: "100%", minWidth: 0 }}
          />
        </Field>
        <Field label={tr("staff.support.ticket")} hint={tr("staff.support.ticketHelp")} required>
          <Input value={ticket} onChange={(e) => setTicket(e.currentTarget.value)} maxLength={27} placeholder="INC-7" />
        </Field>
        <Field label={tr("staff.breakglass.minutes")} required>
          <Input
            type="number"
            inputMode="numeric"
            min={BREAKGLASS_MINUTES_MIN}
            max={BREAKGLASS_MINUTES_MAX}
            step={5}
            value={minutes}
            onChange={(e) => setMinutes(e.currentTarget.value)}
          />
        </Field>
      </div>
      {agency?.my_session_expires_at ? (
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
          <span>{fmt(tr("staff.breakglass.mineUntil"), { time: formatInstant(agency.my_session_expires_at, locale, "UTC") })}</span>
          <Link href={`/staff/support/${agency.tenant_id}`}>{tr("staff.support.open")} →</Link>
        </div>
      ) : (
        <div>
          <Button
            variant="danger"
            size="sm"
            disabled={!hydrated || busy}
            testId="staff-breakglass-start"
            onClick={() => {
              const problem = check();
              setError(problem);
              if (!problem) setAsking(true);
            }}
          >
            {tr("staff.breakglass.start")}
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" data-testid="staff-breakglass-error" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {error}
        </p>
      )}
      <ConfirmModal
        open={asking}
        onClose={() => setAsking(false)}
        onConfirm={start}
        title={tr("staff.breakglass.confirmTitle")}
        body={fmt(tr("staff.breakglass.confirmBody"), { agency: agency?.agency_name ?? "", n })}
        confirmLabel={tr("staff.breakglass.confirm")}
        destructive
        busy={busy}
        testId="staff-breakglass-confirm"
      />
    </div>
  );
}
