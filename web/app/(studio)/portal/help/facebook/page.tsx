"use client";
// web/app/(studio)/portal/help/facebook/page.tsx
// AA-831 — the step-by-step guide behind "Connect Facebook Page": a Meta app in Live mode, a User token with the three
// permissions, the long-lived Page token, the Page ID, then Test & Connect in the portal; plus the common problems.
// AA-760 — back to Integrations (that page holds the Page ID + token form this guide ends in; Publish is not a sidebar
// item, so "Back to Publish" named a place the tenant could not find).
// AA-1036 (R3): the guide's text is ./facebookGuide.messages.ts (English and Vietnamese) and the page is built from
// kit parts (PageHeader, Section) instead of the studio's legacy primitives.
import Link from "next/link";
import { ArrowLeft, ExternalLink, ShieldCheck } from "lucide-react";
import { K, PageHeader, Section, mono, sans, useLocalized, useT } from "@/app/_kit";
import { PermissionList } from "../../_components/FacebookConnect";
import { FACEBOOK_GUIDE, GUIDE_CHROME, type GuideBlock, type Run } from "./facebookGuide.messages";

const text: React.CSSProperties = { fontSize: 13.5, color: K.body, lineHeight: 1.65, margin: "0 0 10px" };
const list: React.CSSProperties = { ...text, paddingLeft: 20 };
const bullets: React.CSSProperties = { ...list, listStyleType: "disc" };
const numbers: React.CSSProperties = { ...list, listStyleType: "decimal" };
const codeStyle: React.CSSProperties = {
  fontFamily: mono, fontSize: 12, color: K.ink3, background: K.bg, border: `1px solid ${K.line}`,
  borderRadius: 4, padding: "0 5px", overflowWrap: "anywhere",
};
const sectionTitle: React.CSSProperties = {
  fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em",
  color: K.muted, margin: "8px 0 2px",
};

function Ext({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" style={{
      color: K.accentDeep, fontWeight: 600, textDecoration: "none", overflowWrap: "anywhere",
    }}>
      {children} <ExternalLink size={11} style={{ display: "inline", verticalAlign: "-1px" }} aria-hidden />
    </a>
  );
}

function Runs({ runs }: { runs: Run[] }) {
  return (
    <>
      {runs.map((r, i) => {
        if (typeof r === "string") return <span key={i}>{r}</span>;
        if ("b" in r) return <strong key={i}>{r.b}</strong>;
        if ("c" in r) return <code key={i} style={codeStyle}>{r.c}</code>;
        if ("ext" in r) return <Ext key={i} href={r.href}>{r.ext}</Ext>;
        if ("link" in r) {
          return <Link key={i} href={r.href} style={{ color: K.accentDeep, fontWeight: 600, textDecoration: "none" }}>{r.link}</Link>;
        }
        return <PermissionList key={i} />;
      })}
    </>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 12.5, color: K.accentDeep, background: K.accentTint, border: `1px solid ${K.accentSoft}`,
      borderRadius: 8, padding: "10px 12px", lineHeight: 1.55, marginTop: 4,
    }}>{children}</div>
  );
}

function Block({ block }: { block: GuideBlock }) {
  if ("p" in block) return <p style={text}><Runs runs={block.p} /></p>;
  if ("muted" in block) return <p style={{ ...text, fontSize: 12.5, color: K.muted, marginBottom: 0 }}><Runs runs={block.muted} /></p>;
  if ("note" in block) return <Note><Runs runs={block.note} /></Note>;
  const items = "ol" in block ? block.ol : block.ul;
  const Tag = "ol" in block ? "ol" : "ul";
  return <Tag style={"ol" in block ? numbers : bullets}>{items.map((it, i) => <li key={i}><Runs runs={it} /></li>)}</Tag>;
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <Section bodyStyle={{ padding: "20px 22px 14px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
        <span style={{
          width: 28, height: 28, borderRadius: "50%", background: K.accentTint, color: K.accentDeep,
          border: `1px solid ${K.accentSoft}`, display: "grid", placeItems: "center",
          fontSize: 13, fontWeight: 700, flexShrink: 0, fontFamily: sans,
        }}>{n}</span>
        <h2 style={{ margin: 0, fontSize: 15.5, fontWeight: 600, color: K.ink, fontFamily: sans }}>
          {title}
        </h2>
      </div>
      {children}
    </Section>
  );
}

export default function FacebookGuidePage() {
  const t = useT(GUIDE_CHROME);
  const guide = useLocalized(FACEBOOK_GUIDE);
  return (
    <div style={{ maxWidth: 760 }}>
      <Link href="/portal/account/integrations" style={{
        display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 600,
        color: K.muted, textDecoration: "none", marginBottom: 16, fontFamily: sans,
      }}>
        <ArrowLeft size={13} /> {t("back")}
      </Link>

      <PageHeader title={guide.title} description={guide.sub} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Section title={t("beforeTitle")}>
          <ul style={{ ...bullets, marginBottom: 6 }}>
            {guide.before.map((item, i) => <li key={i}><Runs runs={item} /></li>)}
          </ul>
          <Block block={{ muted: guide.beforeNote }} />
        </Section>

        <h2 style={sectionTitle}>{t("steps")}</h2>

        {guide.steps.map((s, i) => (
          <Step key={i} n={i + 1} title={s.title}>
            {s.blocks.map((b, j) => <Block key={j} block={b} />)}
          </Step>
        ))}

        <Section bodyStyle={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
          <ShieldCheck size={18} color={K.success} style={{ flexShrink: 0, marginTop: 2 }} aria-hidden />
          <p style={{ ...text, margin: 0 }}><Runs runs={guide.tip} /></p>
        </Section>

        <h2 style={sectionTitle}>{t("problems")}</h2>

        {guide.problems.map((p) => (
          <Section key={p.title} bodyStyle={{ padding: "18px 22px 12px" }}>
            <h3 style={{ margin: "0 0 10px", fontSize: 14, fontWeight: 600, color: K.ink, fontFamily: sans }}>
              {p.title}
            </h3>
            <p style={text}><strong style={{ color: K.ink3 }}>{t("why")}</strong> <Runs runs={p.cause} /></p>
            <p style={text}><strong style={{ color: K.ink3 }}>{t("fix")}</strong> <Runs runs={p.fix} /></p>
          </Section>
        ))}
      </div>
    </div>
  );
}
