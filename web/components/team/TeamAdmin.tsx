"use client";
// web/components/team/TeamAdmin.tsx — A-18 Team & roles for the agency owner (AA-939, card CRM-11; design v3 §4.6):
// the plan line (seats used of the plan's limit), invite by e-mail with a role, pending invitations (masked address,
// expiry, revoke), members (invited address or short id, role, status, open trips; change role, remove), and the
// reassign queue (R8: open trips of removed members, moved to an active member). Every call goes through the BFF
// allow-list; the API checks the permission matrix and keeps the last owner (409 last_owner).
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, EmptyState, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { type Lang, type MessageKey, t } from "@/lib/i18n";
import type { Role } from "@/lib/nav";
import { memberLabel, reassignTargets, seatsLeft, teamError, type ReassignQueue, type TeamPage } from "@/lib/team";

const ROLES: Role[] = ["owner", "agent", "assistant"];

const field: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "7px 9px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  boxSizing: "border-box",
};
const cell: React.CSSProperties = { padding: "8px 8px", borderBottom: `1px solid ${K.line}`, fontSize: 13, verticalAlign: "middle" };
const head: React.CSSProperties = { ...cell, color: K.muted, fontWeight: 600, fontSize: 11.5, textAlign: "left", letterSpacing: "0.04em" };

export function TeamAdmin({ page, queue, lang, locale }: { page: TeamPage; queue: ReassignQueue | null; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("agent");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [targets, setTargets] = useState<Record<string, string>>({});

  async function send(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown, done?: MessageKey) {
    setBusy(true);
    setError(null);
    setNotice(null);
    const res = await bffSend(method, path, body);
    setBusy(false);
    if (!res.ok) {
      const known = teamError(res.detail);
      setError(t(lang, known ? (`team.error.${known}` as MessageKey) : "trip.error"));
      return false;
    }
    if (done) setNotice(t(lang, done));
    router.refresh();
    return true;
  }

  // Server-rendered first: until React attaches the handlers a click does nothing, so the buttons wait for hydration
  // and the root carries data-hydrated="true" (the E2E waits for it, as on the trip page).
  const hydrated = useHydrated();
  const left = seatsLeft(page.plan);
  const fromMembers = Array.from(new Set(queue?.trips.map((tr) => tr.from_member_id) ?? []));
  const byId = new Map(page.members.map((m) => [m.member_id, m]));

  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="team-admin" data-hydrated={hydrated ? "true" : undefined}>
      <Panel title={t(lang, "team.plan")} testId="team-plan">
        <p style={{ margin: 0, fontSize: 13.5, color: K.ink }}>
          {t(lang, `team.plan.${page.plan.plan}` as MessageKey)} ·{" "}
          {t(lang, "team.seats").replace("{used}", String(page.plan.seats_used)).replace("{limit}", String(page.plan.member_limit))}
        </p>
      </Panel>

      <Panel title={t(lang, "team.invite")} testId="team-invite">
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (email.trim() && (await send("POST", "team/invitations", { email: email.trim(), role }, "team.invited"))) setEmail("");
          }}
          style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}
        >
          <label style={{ display: "grid", gap: 4, fontSize: 13, flex: "1 1 240px" }}>
            {t(lang, "team.email")}
            <input
              type="email"
              autoComplete="off"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              maxLength={254}
              style={{ ...field, width: "100%" }}
              data-testid="team-invite-email"
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 13 }}>
            {t(lang, "team.role")}
            <select value={role} onChange={(e) => setRole(e.target.value as Role)} style={field} data-testid="team-invite-role">
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {t(lang, `role.${r}` as MessageKey)}
                </option>
              ))}
            </select>
          </label>
          <Button type="submit" variant="primary" disabled={busy || !hydrated || !email.trim() || left === 0}>
            {t(lang, "team.sendInvite")}
          </Button>
        </form>
        <p style={{ margin: "8px 0 0", fontSize: 12.5, color: K.muted }}>{t(lang, "team.inviteHelp")}</p>
        {left === 0 && <p style={{ margin: "6px 0 0", fontSize: 12.5, color: K.amber }}>{t(lang, "team.error.member_limit_reached")}</p>}
      </Panel>

      {(error || notice) && (
        <p role={error ? "alert" : "status"} style={{ margin: 0, fontSize: 13, color: error ? K.danger : K.ink }} data-testid="team-message">
          {error ?? notice}
        </p>
      )}

      {page.invitations.length > 0 && (
        <Panel title={t(lang, "team.pending")} testId="team-invitations">
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={head}>{t(lang, "team.email")}</th>
                  <th style={head}>{t(lang, "team.role")}</th>
                  <th style={head}>{t(lang, "team.expires")}</th>
                  <th style={head} />
                </tr>
              </thead>
              <tbody>
                {page.invitations.map((i) => (
                  <tr key={i.invitation_id}>
                    <td style={{ ...cell, overflowWrap: "anywhere" }}>{i.email_masked}</td>
                    <td style={cell}>{t(lang, `role.${i.role}` as MessageKey)}</td>
                    <td style={{ ...cell, whiteSpace: "nowrap" }}>
                      {formatDate(i.expires_at, locale)}
                      {i.status === "claimed" && (
                        <span style={{ marginLeft: 6 }}>
                          <Badge tone="info">{t(lang, "team.opened")}</Badge>
                        </span>
                      )}
                    </td>
                    <td style={{ ...cell, textAlign: "right" }}>
                      <Button size="sm" variant="ghost" disabled={busy || !hydrated} onClick={() => void send("DELETE", `team/invitations/${i.invitation_id}`, undefined, "team.revoked")}>
                        {t(lang, "team.revoke")}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      <Panel title={t(lang, "team.members")} testId="team-members">
        {page.members.length === 0 ? (
          <EmptyState title={t(lang, "team.members")} description={t(lang, "team.noMembers")} />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={head}>{t(lang, "team.member")}</th>
                  <th style={head}>{t(lang, "team.role")}</th>
                  <th style={head}>{t(lang, "team.status")}</th>
                  <th style={head}>{t(lang, "team.openTrips")}</th>
                  <th style={head} />
                </tr>
              </thead>
              <tbody>
                {page.members.map((m) => (
                  <tr key={m.member_id} data-testid="team-member-row">
                    <td style={{ ...cell, overflowWrap: "anywhere", maxWidth: 280 }}>
                      {memberLabel(m)}
                      {m.is_me && <span style={{ color: K.muted }}> · {t(lang, "team.you")}</span>}
                    </td>
                    <td style={cell}>
                      {m.status === "disabled" ? (
                        t(lang, `role.${m.role}` as MessageKey)
                      ) : (
                        <select
                          aria-label={t(lang, "team.role")}
                          value={m.role}
                          disabled={busy || !hydrated}
                          onChange={(e) => void send("PATCH", `team/members/${m.member_id}`, { role: e.target.value }, "team.roleChanged")}
                          style={field}
                        >
                          {ROLES.map((r) => (
                            <option key={r} value={r}>
                              {t(lang, `role.${r}` as MessageKey)}
                            </option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td style={cell}>
                      <Badge tone={m.status === "active" ? "success" : m.status === "invited" ? "info" : "neutral"}>
                        {t(lang, `team.status.${m.status}` as MessageKey)}
                      </Badge>
                    </td>
                    <td style={cell}>{m.open_trips}</td>
                    <td style={{ ...cell, textAlign: "right" }}>
                      {m.status !== "disabled" && (
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={busy || !hydrated}
                          onClick={() => {
                            if (window.confirm(t(lang, "team.removeConfirm"))) void send("POST", `team/members/${m.member_id}/remove`, undefined, "team.removed");
                          }}
                        >
                          {t(lang, "team.remove")}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title={t(lang, "team.queue")} testId="team-reassign">
        {!queue || queue.trips.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t(lang, "team.queueEmpty")}</p>
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            {fromMembers.map((from) => {
              const trips = queue.trips.filter((tr) => tr.from_member_id === from);
              const options = reassignTargets(page.members, from);
              const target = targets[from] ?? options[0]?.member_id ?? "";
              const who = byId.get(from);
              return (
                <div key={from} style={{ display: "grid", gap: 6 }}>
                  <strong style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>
                    {who ? memberLabel(who) : `#${from.slice(0, 8)}`} ·{" "}
                    {t(lang, "team.queueCount")
                      .replace("{trips}", String(trips.length))
                      .replace("{clients}", String(queue.clients_by_member[from] ?? 0))}
                  </strong>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                    {trips.map((tr) => (
                      <li key={tr.trip_id}>
                        <a href={`/pipeline/${tr.trip_id}`}>{tr.title}</a> · {t(lang, `stage.${tr.stage}` as MessageKey)}
                      </li>
                    ))}
                  </ul>
                  <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <select
                      aria-label={t(lang, "team.reassignTo")}
                      value={target}
                      onChange={(e) => setTargets({ ...targets, [from]: e.target.value })}
                      style={field}
                    >
                      {options.map((o) => (
                        <option key={o.member_id} value={o.member_id}>
                          {memberLabel(o)}
                        </option>
                      ))}
                    </select>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={busy || !hydrated || !target}
                      onClick={() => void send("POST", "team/reassign", { from_member_id: from, to_member_id: target }, "team.reassigned")}
                    >
                      {t(lang, "team.reassign")}
                    </Button>
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
