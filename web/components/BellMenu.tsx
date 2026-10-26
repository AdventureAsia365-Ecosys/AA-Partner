"use client";
// web/components/BellMenu.tsx — the agency bell in the top bar (AA-950, card CRM-21b; design v3 A-00 "Bell": proposal
// viewed or option chosen, re-hold placed (Fix A3), the client's change request, a hold that failed, the R4 nudge).
// GET /bff/bell lists the signed-in member's unseen events, bundled per (proposal, event) with a count; "Mark all as
// seen" posts /bff/bell/seen. Loaded once when the shell mounts and again on open; no polling (no push in v1).
import { Bell } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { K } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { bellLine, type BellView } from "@/lib/bell";
import { fmt, type MessageKey } from "@/lib/i18n";

export function BellMenu({ dict, buttonStyle }: { dict: Record<MessageKey, string>; buttonStyle: React.CSSProperties }) {
  const [view, setView] = useState<BellView | null>(null);
  const [open, setOpen] = useState(false);

  async function load() {
    const res = await bffSend<BellView>("GET", "bell");
    if (res.ok) setView(res.data);
  }

  useEffect(() => {
    void load();
  }, []);

  async function seen() {
    const res = await bffSend<{ seen: number }>("POST", "bell/seen", {});
    if (res.ok) setView({ items: [], unseen: 0 });
  }

  const unseen = view?.unseen ?? 0;
  return (
    <div data-testid="crm-bell" data-unseen={unseen} style={{ position: "relative" }}>
      <button
        type="button"
        title={dict["bell.title"]}
        aria-label={unseen ? `${dict["bell.title"]} (${unseen})` : dict["bell.title"]}
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
          if (!open) void load();
        }}
        style={{ ...buttonStyle, display: "inline-flex", position: "relative" }}
      >
        <Bell size={16} />
        {unseen > 0 && (
          <span
            aria-hidden
            style={{ position: "absolute", top: -4, right: -4, minWidth: 16, height: 16, borderRadius: 8, padding: "0 4px", background: K.danger, color: "#fff", fontSize: 10, lineHeight: "16px", textAlign: "center" }}
          >
            {unseen > 99 ? "99+" : unseen}
          </span>
        )}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={dict["bell.title"]}
          data-testid="crm-bell-list"
          style={{ position: "absolute", right: 0, top: 40, zIndex: 30, width: "min(340px, calc(100vw - 32px))", maxHeight: 420, overflowY: "auto", display: "grid", gap: 6, padding: 10, borderRadius: 10, border: `1px solid ${K.line}`, background: K.card, boxShadow: "0 8px 24px rgba(0,0,0,0.12)" }}
        >
          <strong style={{ fontSize: 13, color: K.ink }}>{dict["bell.title"]}</strong>
          {(view?.items.length ?? 0) === 0 && <span style={{ fontSize: 12.5, color: K.muted }}>{dict["bell.empty"]}</span>}
          {view?.items.map((item, i) => {
            const line = bellLine(item);
            return (
              <Link
                key={i}
                href={`/pipeline/${item.trip_id}`}
                onClick={() => setOpen(false)}
                style={{ fontSize: 12.5, color: K.body, textDecoration: "none", overflowWrap: "anywhere", padding: "4px 0", borderTop: i ? `1px solid ${K.line}` : "none" }}
              >
                {fmt(dict[line.key as MessageKey] ?? line.key, line.vars)}
              </Link>
            );
          })}
          {(view?.items.length ?? 0) > 0 && (
            <button type="button" data-testid="crm-bell-seen" onClick={() => void seen()} style={{ font: "inherit", fontSize: 12.5, border: "none", background: "transparent", color: K.accent, cursor: "pointer", justifySelf: "start", padding: 0 }}>
              {dict["bell.markSeen"]}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
