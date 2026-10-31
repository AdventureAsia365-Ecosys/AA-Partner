"use client";
// web/components/onboarding/DuplicatesView.tsx — Clients > Duplicates (AA-962, card CRM-31 "merge screen"; design v3
// R6, A-06 Duplicates): pairs of clients sharing a name, e-mail or phone (display names only; which field matched is a
// word, never the value), "Keep this one" on either side, a confirm dialog, the merge (owner). A 409 merge_blocked
// names what the client that would go has (links, portal access, signatures) and offers to keep that one instead.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, EmptyState, K, Modal } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { importErrorKey, mergeBlock, type DuplicateClient, type DuplicatesView as View } from "@/lib/onboarding";

type Pending = { keep: DuplicateClient; other: DuplicateClient };

export function DuplicatesView({ view, owner, lang, locale }: { view: View; owner: boolean; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; swap?: Pending } | null>(null);
  const [done, setDone] = useState(false);

  async function merge(p: Pending) {
    setBusy(true);
    setError(null);
    const res = await bffSend("POST", `travelers/${p.keep.traveler_id}/merge`, { other_traveler_id: p.other.traveler_id });
    setBusy(false);
    setPending(null);
    if (res.ok) {
      setDone(true);
      router.refresh();
      return;
    }
    if (res.detail === "merge_blocked") {
      const { blockers, swap } = mergeBlock(res.body);
      const what = blockers.map((b) => tr(`onb.block.${b}`)).join(", ");
      setError({
        text: fmt(t(lang, swap ? "onb.dup.blocked" : "onb.dup.blockedBoth"), { what }),
        swap: swap ? { keep: p.other, other: p.keep } : undefined,
      });
      return;
    }
    setError({ text: tr(importErrorKey(res.status, res.detail)) });
  }

  if (view.pairs.length === 0) {
    return (
      <Panel testId="onb-dup-empty">
        {done && <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>{t(lang, "onb.dup.merged")}</p>}
        <EmptyState title={t(lang, "onb.dup.none")} description={t(lang, "onb.dup.noneBody")} />
      </Panel>
    );
  }

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="onb-duplicates">
      {done && (
        <p role="status" style={{ margin: 0, fontSize: 13, color: K.muted }}>
          {t(lang, "onb.dup.merged")}
        </p>
      )}
      {error && (
        <div role="alert" style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ color: K.danger, fontSize: 13 }}>{error.text}</span>
          {owner && error.swap && (
            <Button size="sm" variant="secondary" onClick={() => setPending(error.swap ?? null)}>
              {t(lang, "onb.dup.swap")}
            </Button>
          )}
        </div>
      )}
      <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{t(lang, "onb.dup.notSame")}</p>
      {view.pairs.map((pair) => {
        const [a, b] = pair.clients;
        const fields = pair.matched_on.map((m) => tr(`onb.dup.field.${m}`)).join(", ");
        return (
          <Panel key={`${a.traveler_id}-${b.traveler_id}`} testId="onb-dup-pair">
            <div style={{ marginBottom: 10 }}>
              <Badge tone="warning">{fmt(t(lang, "onb.dup.matched"), { fields })}</Badge>
            </div>
            <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
              {[a, b].map((c, i) => {
                const other = i === 0 ? b : a;
                return (
                  <div key={c.traveler_id} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "10px 12px", display: "grid", gap: 4 }}>
                    <a href={`/clients/${c.traveler_id}`} style={{ fontSize: 14, fontWeight: 500, color: K.ink, overflowWrap: "anywhere" }}>
                      {c.display_name}
                    </a>
                    <span style={{ fontSize: 12.5, color: K.muted }}>
                      {c.trips === 1 ? t(lang, "onb.dup.trip1") : fmt(t(lang, "onb.dup.trips"), { n: c.trips })} · {fmt(t(lang, "onb.dup.added"), { date: formatDate(new Date(c.created_at), locale) })}
                    </span>
                    {owner && (
                      <div style={{ marginTop: 6 }}>
                        <Button size="sm" variant="secondary" disabled={busy} onClick={() => setPending({ keep: c, other })}>
                          {t(lang, "onb.dup.keep")}
                        </Button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </Panel>
        );
      })}
      {view.has_more && (
        <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{fmt(t(lang, "onb.dup.more"), { shown: view.pairs.length, total: view.total })}</p>
      )}
      {!owner && <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{t(lang, "onb.dup.ownerOnly")}</p>}
      <Modal
        open={pending !== null}
        onClose={() => setPending(null)}
        title={t(lang, "onb.dup.confirmTitle")}
        testId="onb-dup-confirm"
        footer={
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setPending(null)} disabled={busy}>
              {t(lang, "onb.dup.cancel")}
            </Button>
            <Button variant="primary" onClick={() => pending && merge(pending)} disabled={busy}>
              {t(lang, "onb.dup.merge")}
            </Button>
          </div>
        }
      >
        <p style={{ margin: 0, fontSize: 13.5, color: K.body }}>
          {pending ? fmt(t(lang, "onb.dup.confirmBody"), { keep: pending.keep.display_name }) : null}
        </p>
      </Modal>
    </div>
  );
}
