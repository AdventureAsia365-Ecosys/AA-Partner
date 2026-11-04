"use client";
import { CheckCircle2, AlertTriangle, XCircle } from "lucide-react";
import { K, sans, useT } from "@/app/_kit";
import { COMMON_MESSAGES } from "./common.messages";

interface SeoCheck {
  status: "pass" | "warn" | "fail";
  message: string;
}

interface SeoHealthBarProps {
  seoTitle: string | null | undefined;
  seoMeta: string | null | undefined;
  highlights: unknown[];
  summary: string | null | undefined;
  rulesApplied?: Array<{ rule_id: string; message: string }>;
}

export function SeoHealthBar({ seoTitle, seoMeta, highlights, summary, rulesApplied }: SeoHealthBarProps) {
  const t = useT(COMMON_MESSAGES);
  const checks: SeoCheck[] = [
    {
      status: !seoTitle ? "fail" : seoTitle.length > 60 ? "warn" : "pass",
      message: !seoTitle
        ? t("seoTitleMissing")
        : seoTitle.length > 60
        ? t("seoTitleLong", seoTitle.length)
        : t("seoTitleOk", seoTitle.length),
    },
    {
      status: !seoMeta ? "fail" : seoMeta.length > 160 ? "warn" : seoMeta.length < 80 ? "warn" : "pass",
      message: !seoMeta
        ? t("seoMetaMissing")
        : seoMeta.length > 160
        ? t("seoMetaLong", seoMeta.length)
        : seoMeta.length < 80
        ? t("seoMetaShort", seoMeta.length)
        : t("seoMetaOk", seoMeta.length),
    },
    {
      status: highlights.length >= 3 ? "pass" : "fail",
      message: highlights.length >= 3
        ? t("seoHighlightsOk", highlights.length)
        : t("seoHighlightsFew", highlights.length),
    },
    {
      status: summary && summary.length > 50 ? "pass" : "fail",
      message: summary && summary.length > 50 ? t("seoSummaryOk") : t("seoSummaryShort"),
    },
  ];

  if (rulesApplied?.length) {
    rulesApplied.forEach(r => checks.push({ status: "warn", message: r.message }));
  }

  const icon  = { pass: CheckCircle2, warn: AlertTriangle, fail: XCircle } as const;
  const color = { pass: K.success, warn: K.amber, fail: K.danger } as const;
  const allPass = checks.every(c => c.status === "pass");

  return (
    <div style={{ borderRadius: 8, border: `1px solid ${K.line}`, background: K.bg, padding: "12px 14px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted }}>
          {t("seoHealth")}
        </span>
        {allPass && (
          <span style={{ fontSize: 11, color: K.success, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 4 }}><CheckCircle2 size={12} /> {t("seoReady")}</span>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {checks.map((check, i) => (
          <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 7, fontSize: 12 }}>
            {(() => { const Icon = icon[check.status]; return <Icon size={13} color={color[check.status]} style={{ flexShrink: 0, marginTop: 2 }} />; })()}
            <span style={{ color: color[check.status], lineHeight: 1.5, fontFamily: sans }}>
              {check.message}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
