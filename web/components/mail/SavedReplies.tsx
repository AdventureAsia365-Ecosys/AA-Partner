"use client";
// web/components/mail/SavedReplies.tsx — Settings > Saved replies (AA-945, card CRM-17 "saved replies"): the agency's
// shared texts (the owner writes them; the API checks) and each member's own, EN or VI. The composer of the Messages
// tab inserts them.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { t, type Lang, type MessageKey } from "@/lib/i18n";
import { mailErrorKey, type SavedReply } from "@/lib/mail";
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

export function SavedReplies({ replies, role, lang }: { replies: SavedReply[]; role: Role; lang: Lang }) {
  const router = useRouter();
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [notice, setNotice] = useState<string | null>(null);

  async function run(method: "POST" | "DELETE", path: string, body?: unknown) {
    const res = await bffSend(method, path, body);
    setNotice(res.ok ? null : tr(mailErrorKey(res.detail)));
    if (res.ok) router.refresh();
  }

  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="saved-replies">
      {notice && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: K.danger }}>
          {notice}
        </p>
      )}
      <Panel title={tr("replies.title")} testId="saved-replies-list">
        {replies.length === 0 ? (
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("replies.empty")}</p>
        ) : (
          <ul style={{ margin: 0, padding: 0, display: "grid", gap: 8 }}>
            {replies.map((r) => (
              <li key={r.saved_reply_id} style={{ listStyle: "none", display: "grid", gap: 4, fontSize: 13 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <strong>{r.title}</strong>
                  <span style={{ color: K.muted }}>
                    {tr(`replies.scope.${r.scope}`)} · {r.locale.toUpperCase()}
                  </span>
                  {(r.scope === "own" || role === "owner") && (
                    <Button size="sm" variant="ghost" disabled={!hydrated} onClick={() => run("DELETE", `saved-replies/${r.saved_reply_id}`)}>
                      {tr("replies.delete")}
                    </Button>
                  )}
                </div>
                <div style={{ whiteSpace: "pre-wrap", color: K.muted }}>{r.body}</div>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title={tr("replies.new")} testId="saved-replies-new">
        <form
          action={(data) =>
            void run("POST", "saved-replies", {
              scope: String(data.get("scope") ?? "own"),
              locale: String(data.get("locale") ?? "en"),
              title: String(data.get("title") ?? "").trim(),
              body: String(data.get("body") ?? ""),
            })
          }
          style={{ display: "grid", gap: 8, maxWidth: 560 }}
        >
          <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
            {tr("replies.name")}
            <input name="title" required maxLength={80} style={field} />
          </label>
          <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
            {tr("replies.text")}
            <textarea name="body" required maxLength={20000} style={{ ...field, minHeight: 100 }} />
          </label>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
              {tr("replies.locale")}
              <select name="locale" defaultValue={lang} style={field}>
                <option value="en">English</option>
                <option value="vi">Tiếng Việt</option>
              </select>
            </label>
            {role === "owner" && (
              <label style={{ display: "grid", gap: 2, fontSize: 12.5 }}>
                &nbsp;
                <select name="scope" defaultValue="own" style={field}>
                  <option value="own">{tr("replies.scope.own")}</option>
                  <option value="agency">{tr("replies.scope.agency")}</option>
                </select>
              </label>
            )}
          </div>
          <div>
            <Button type="submit" size="sm" variant="primary" disabled={!hydrated}>
              {tr("replies.save")}
            </Button>
          </div>
        </form>
      </Panel>
    </div>
  );
}
