"use client";
import { T, sans } from "./ui";

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
  const checks: SeoCheck[] = [
    {
      status: !seoTitle ? "fail" : seoTitle.length > 60 ? "warn" : "pass",
      message: !seoTitle
        ? "SEO title missing"
        : seoTitle.length > 60
        ? `Title too long (${seoTitle.length}/60 chars)`
        : `Title OK (${seoTitle.length}/60 chars)`,
    },
    {
      status: !seoMeta ? "fail" : seoMeta.length > 160 ? "warn" : seoMeta.length < 80 ? "warn" : "pass",
      message: !seoMeta
        ? "Meta description missing"
        : seoMeta.length > 160
        ? `Description too long (${seoMeta.length}/160 chars)`
        : seoMeta.length < 80
        ? `Description too short — aim for 80–160 chars (${seoMeta.length})`
        : `Description OK (${seoMeta.length}/160 chars)`,
    },
    {
      status: highlights.length >= 3 ? "pass" : "fail",
      message: highlights.length >= 3
        ? `${highlights.length} highlights ✓`
        : `Need at least 3 highlights (have ${highlights.length})`,
    },
    {
      status: summary && summary.length > 50 ? "pass" : "fail",
      message: summary && summary.length > 50 ? "Summary complete" : "Summary too short or missing",
    },
  ];

  if (rulesApplied?.length) {
    rulesApplied.forEach(r => checks.push({ status: "warn", message: r.message }));
  }

  const icon  = { pass: "🟢", warn: "🟡", fail: "🔴" } as const;
  const color = { pass: T.green, warn: T.amber, fail: T.red } as const;
  const allPass = checks.every(c => c.status === "pass");

  return (
    <div style={{ borderRadius: 8, border: `1px solid ${T.line}`, background: T.bg, padding: "12px 14px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted }}>
          SEO Health
        </span>
        {allPass && (
          <span style={{ fontSize: 11, color: T.green, fontWeight: 600 }}>✅ Content ready to use</span>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {checks.map((check, i) => (
          <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 7, fontSize: 12 }}>
            <span style={{ flexShrink: 0 }}>{icon[check.status]}</span>
            <span style={{ color: color[check.status], lineHeight: 1.5, fontFamily: sans }}>
              {check.message}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
