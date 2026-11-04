"use client";
// AA-638 — getting-started checklist on the Dashboard, driven by the tenant's real state (no
// stored "onboarding" flags): brand set up, first tour rewritten, first post written, WordPress
// connected. Hides itself once everything is done; can be dismissed per browser.
import { useEffect, useState } from "react";
import Link from "next/link";
import { Check, ArrowRight, X } from "lucide-react";
import { K, useT } from "@/app/_kit";
import { COMMON_MESSAGES } from "./common.messages";
import { T, Card, CardHead } from "./ui";

type ItemKey = "brand" | "tour" | "post" | "wp";
type Item = { key: ItemKey; href: string; done: boolean };

const TEXT = {
  brand: ["gsBrandTitle", "gsBrandSub", "gsBrandCta"],
  tour: ["gsTourTitle", "gsTourSub", "gsTourCta"],
  post: ["gsPostTitle", "gsPostSub", "gsPostCta"],
  wp: ["gsWpTitle", "gsWpSub", "gsWpCta"],
} as const;

const DISMISS_KEY = "cis_portal_getting_started_dismissed";

type Json = Record<string, unknown> | null;

async function json(url: string): Promise<Json> {
  try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch { return null; }
}

export default function GettingStarted({ catalogCount }: { catalogCount: number }) {
  const t = useT(COMMON_MESSAGES);
  const [items, setItems] = useState<Item[] | null>(null);
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try { return typeof window !== "undefined" && localStorage.getItem(DISMISS_KEY) === "1"; } catch { return false; }
  });

  useEffect(() => {
    Promise.all([
      json("/api/tenant/admin/brand-identity"),
      json("/api/tenant/v1/content-writing/reviews"),
      json("/api/tenant/v1/integrations/wordpress"),
    ]).then(([brand, reviews, wp]) => {
      const brandDone = !!(brand && (String(brand.system_prompt ?? "").trim() || String(brand.core_idea ?? "").trim()));
      const reviewList = reviews?.data;
      const postsDone = Array.isArray(reviewList) && reviewList.length > 0;
      setItems([
        { key: "brand", href: "/portal/brand", done: brandDone },
        { key: "tour", href: "/portal/tours", done: catalogCount > 0 },
        { key: "post", href: "/portal/create/social", done: postsDone },
        { key: "wp", href: "/portal/content/publish", done: !!wp?.connected },
      ]);
    });
  }, [catalogCount]);

  if (!items || dismissed) return null;
  const doneCount = items.filter(i => i.done).length;
  if (doneCount === items.length) return null;
  const next = items.find(i => !i.done);

  function dismiss() {
    setDismissed(true);
    try { localStorage.setItem(DISMISS_KEY, "1"); } catch { /* ignore */ }
  }

  return (
    <Card>
      <CardHead title={t("gsTitle", doneCount, items.length)} action={
        <button onClick={dismiss} aria-label={t("gsHide")} style={{ background: "none", border: "none", cursor: "pointer", color: K.muted2, display: "flex", padding: 0 }}>
          <X size={15} />
        </button>
      } />
      <div style={{ height: 6, borderRadius: 999, background: K.line2, overflow: "hidden", marginBottom: 14 }}>
        <div style={{ width: `${(doneCount / items.length) * 100}%`, height: "100%", background: K.accent, borderRadius: 999, transition: "width .4s ease" }} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {items.map(i => {
          const isNext = i.key === next?.key;
          return (
            <Link key={i.key} href={i.href} style={{
              display: "grid", gridTemplateColumns: "22px minmax(0,1fr) auto", alignItems: "center", gap: 10,
              padding: "9px 10px", margin: "0 -10px", borderRadius: 8, textDecoration: "none",
              background: isNext ? K.accentTint : "transparent",
            }}>
              <span style={{
                width: 20, height: 20, borderRadius: "50%", display: "grid", placeItems: "center",
                background: i.done ? K.success : "transparent", border: i.done ? "none" : `1.5px solid ${isNext ? K.accent : K.line}`,
              }}>
                {i.done && <Check size={12} color={T.onSolid} strokeWidth={3} />}
              </span>
              <span style={{ minWidth: 0 }}>
                <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: i.done ? K.muted : K.ink, textDecoration: i.done ? "line-through" : "none" }}>{t(TEXT[i.key][0])}</span>
                {!i.done && <span style={{ display: "block", fontSize: 11.5, color: K.muted, marginTop: 1 }}>{t(TEXT[i.key][1])}</span>}
              </span>
              {!i.done && isNext && (
                <span style={{ fontSize: 12, fontWeight: 600, color: K.accentDeep, display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
                  {t(TEXT[i.key][2])} <ArrowRight size={13} />
                </span>
              )}
            </Link>
          );
        })}
      </div>
    </Card>
  );
}
