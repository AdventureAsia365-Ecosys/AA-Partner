// web/app/(studio)/_legal/LegalPage.tsx
// AA-886 — shell of the portal's legal pages (/privacy, /terms). Public pages (proxy.ts matches only
// /portal/* and /tenant-login), light theme like /tenant-login, and a draft banner that is ALWAYS shown
// while counsel has not approved the text. Portal-owned (not a vendored kit file).
// Ink + underline, not gold: BRAND.accentDeep is 4.47:1 on white and 4.05:1 on the gold tint (below WCAG AA).
// AA-1036 (R3): the page renders a structured document (legalDoc.ts) in the UI language (the app's one setting, read
// per request); the frame's words are legal.messages.ts. The Vietnamese document is a translation for reading and says
// so; the English one applies.
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import { BRAND, FONT_DISPLAY, FONT_SANS, LOGO_SRC, SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import { translator, type Lang } from "@/app/_kit/core/i18n";
import { PRIVACY_PATH, TERMS_PATH } from "@/lib/studio/legal";
import type { Block, Inline, LegalDoc, ListItem } from "./legalDoc";
import { LEGAL_MESSAGES } from "./legal.messages";

const linkStyle: CSSProperties = { color: BRAND.ink, fontWeight: 600, textDecoration: "underline", textUnderlineOffset: 2 };

/** A link in the body text; external links open in a new tab. */
export function A({ href, children }: { href: string; children: ReactNode }) {
  const external = href.startsWith("http");
  return (
    <a href={href} style={linkStyle} {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}>
      {children}
    </a>
  );
}

function Runs({ runs }: { runs: Inline[] }) {
  return (
    <>
      {runs.map((r, i) =>
        typeof r === "string" ? <span key={i}>{r}</span>
          : "b" in r ? <strong key={i}>{r.b}</strong>
            : <A key={i} href={r.href}>{r.a}</A>)}
    </>
  );
}

function Item({ item }: { item: ListItem }) {
  if (Array.isArray(item)) return <li><Runs runs={item} /></li>;
  return (
    <li>
      <Runs runs={item.text} />
      <ul>{item.ul.map((x, i) => <Item key={i} item={x} />)}</ul>
    </li>
  );
}

function BlockView({ block }: { block: Block }) {
  if ("p" in block) return <p><Runs runs={block.p} /></p>;
  return <ul>{block.ul.map((x, i) => <Item key={i} item={x} />)}</ul>;
}

function DraftBanner({ lang }: { lang: Lang }) {
  const t = translator(LEGAL_MESSAGES, lang);
  return (
    <div
      role="note"
      data-testid="legal-draft-banner"
      style={{
        background: BRAND.accentTint,
        border: `1px solid ${BRAND.accentBorder}`,
        borderRadius: 12,
        padding: "12px 16px",
        fontSize: 13,
        lineHeight: 1.6,
        color: BRAND.body,
      }}
    >
      <p style={{ margin: 0, fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase", color: BRAND.ink }}>
        {t("draftLabel")}
      </p>
      <p style={{ margin: "4px 0 0" }}>
        {t("draftBody")} <A href={`mailto:${SUPPORT_EMAIL}`}>{SUPPORT_EMAIL}</A>.
      </p>
      {t("translationNote") && (
        <p data-testid="legal-translation-note" style={{ margin: "4px 0 0" }}>{t("translationNote")}</p>
      )}
    </div>
  );
}

export function LegalSection({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} style={{ paddingTop: 24 }}>
      <h2 id={`${id}-title`} style={{ fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 17, color: BRAND.ink, margin: "0 0 8px" }}>
        {title}
      </h2>
      <div className="aa-legal-body" style={{ fontSize: 14, lineHeight: 1.65, color: BRAND.body }}>
        {children}
      </div>
    </section>
  );
}

export default function LegalPage({ doc, lang }: { doc: LegalDoc; lang: Lang }) {
  const t = translator(LEGAL_MESSAGES, lang);
  return (
    <div lang={lang} data-theme="light" style={{ minHeight: "100vh", background: "var(--bg-primary)", fontFamily: FONT_SANS, padding: "32px 16px", boxSizing: "border-box" }}>
      {/* Paragraph and list rhythm for the body text (inline styles cannot target descendants). */}
      <style>{`.aa-legal-body p{margin:0 0 10px}.aa-legal-body ul{list-style:disc;margin:0 0 10px;padding-left:20px}.aa-legal-body li{margin:0 0 6px}.aa-legal-body li ul{list-style:circle;margin:6px 0 0}`}</style>
      <main style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 16, padding: "28px 24px", width: "100%", maxWidth: 760, margin: "0 auto", boxSizing: "border-box" }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={LOGO_SRC} alt={t("logoAlt")} width={52} height={33} style={{ width: 52, height: "auto", display: "block", marginBottom: 16 }} />
        <h1 style={{ fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 24, color: BRAND.ink, margin: "0 0 4px" }}>{doc.title}</h1>
        <p style={{ fontSize: 12, color: BRAND.muted, margin: "0 0 16px" }}>
          {t("versionLine", t("version"), t("updated"))}
        </p>
        <DraftBanner lang={lang} />
        <div style={{ fontSize: 14, lineHeight: 1.65, color: BRAND.body, marginTop: 16 }}>
          <p style={{ margin: 0 }}><Runs runs={doc.intro} /></p>
        </div>
        {doc.sections.map((s) => (
          <LegalSection key={s.id} id={s.id} title={s.title}>
            {s.blocks.map((b, i) => <BlockView key={i} block={b} />)}
          </LegalSection>
        ))}
        <nav aria-label={t("nav")} style={{ display: "flex", flexWrap: "wrap", gap: 16, fontSize: 12, marginTop: 28, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
          <a href={PRIVACY_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>{t("privacy")}</a>
          <a href={TERMS_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>{t("terms")}</a>
          <Link href="/portal/dashboard" style={{ color: BRAND.muted, textDecoration: "none" }}>{t("login")}</Link>
        </nav>
      </main>
    </div>
  );
}
