// web/components/staff/SessionHistory.tsx — S-04, the caller's own sessions in one agency (AA-1019 on GET
// /crm/staff/v1/agencies/{id}/session): kind, ticket, reason, start and end, status; a session that is over says why
// (ended by you / by the owner / expired, S262 finding D1) and when it ended. Server component.
import { Badge, K } from "@/app/_kit";
import { codeLabel } from "@/lib/accessLog";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { t, type Lang } from "@/lib/i18n";
import { endReason, type SessionView } from "@/lib/staffConsole";

/** The status words of one session: "Open", or why it is over. */
export function sessionStatusLabel(lang: Lang, s: SessionView): string {
  const reason = s.status === "active" ? null : endReason(s);
  return reason ? t(lang, `staff.session.endReason.${reason}`) : codeLabel(lang, "staff.session.status", s.status);
}

export function SessionHistory({ sessions, lang, locale }: { sessions: SessionView[]; lang: Lang; locale: DateLocale }) {
  if (sessions.length === 0) return null;
  return (
    <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4, fontSize: 12.5, color: K.body }} data-testid="staff-session-history">
      {sessions.map((s) => (
        <li key={s.session_id}>
          <Badge tone={s.kind === "breakglass" ? "danger" : "warning"} dot={false}>
            {t(lang, s.kind === "breakglass" ? "staff.session.kind.breakglass" : "staff.session.kind.support")}
          </Badge>{" "}
          {s.ticket_ref}
          {s.reason_code ? ` · ${codeLabel(lang, "staff.breakglass.reason", s.reason_code)}` : ""} ·{" "}
          {formatInstant(s.started_at, locale, "UTC")} – {formatInstant(s.ended_at || s.expires_at, locale, "UTC")} ·{" "}
          <span data-status={s.status}>{sessionStatusLabel(lang, s)}</span>
        </li>
      ))}
    </ul>
  );
}
