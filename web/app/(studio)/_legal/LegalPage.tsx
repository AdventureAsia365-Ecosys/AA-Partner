// web/app/(studio)/_legal/LegalPage.tsx
// AA-886 — shell of the portal's legal pages (/privacy, /terms). Public pages (proxy.ts matches only
// /portal/* and /tenant-login), light theme like /tenant-login, and a draft banner that is ALWAYS shown
// while counsel has not approved the text. Portal-owned (not a vendored kit file).
// Ink + underline, not gold: BRAND.accentDeep is 4.47:1 on white and 4.05:1 on the gold tint (below WCAG AA).
// AA-1036 (R3): the page renders a structured document (legalDoc.ts) in the UI language (the app's one setting, read
// per request); the frame's words are legal.messages.ts. The Vietnamese document is a translation for reading and says
// so; the English one applies.
// AA-970: the public data and sub-processor pages (rendered from the root DPA Annex 3) use this frame too, with the
// annex's own version line, row cards for the sub-processor list and both pages in the footer links.
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import { BRAND, FONT_DISPLAY, FONT_SANS, LOGO_SRC, SUPPORT_EMAIL } from "@/app/_kit/core/brand";
import { translator, type Lang } from "@/app/_kit/core/i18n";
import { PageFrame } from "@/app/_kit/core/PageFrame";
import { DATA_PATH, PRIVACY_PATH, SUBPROCESSORS_PATH, TERMS_PATH } from "@/lib/studio/legal";
import type { Block, Inline, LegalDoc, ListItem, RowCard } from "./legalDoc";
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

function Card({ card }: { card: RowCard }) {
  return (
    <div data-testid="legal-row-card" style={{ border: "1px solid var(--border)", borderRadius: 12, padding: "12px 14px", margin: "0 0 10px" }}>
      <h3 style={{ fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 15, color: BRAND.ink, margin: "0 0 6px" }}><Runs runs={card.title} /></h3>
      <dl style={{ margin: 0, display: "grid", gap: 6 }}>
        {card.fields.map((f, i) => (
          <div key={i}>
            <dt style={{ fontSize: 12, fontWeight: 600, color: BRAND.muted }}>{f.label}</dt>
            <dd style={{ margin: 0 }}><Runs runs={f.value} /></dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function BlockView({ block }: { block: Block }) {
  if ("p" in block) return <p><Runs runs={block.p} /></p>;
  if ("rows" in block) return <div>{block.rows.map((c, i) => <Card key={i} card={c} />)}</div>;
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
      <div data-legal-body="" style={{ fontSize: 14, lineHeight: 1.65, color: BRAND.body }}>
        {children}
      </div>
    </section>
  );
}

export default function LegalPage({
  doc,
  lang,
  version,
}: {
  doc: LegalDoc;
  lang: Lang;
  /** AA-970: the version line of a page rendered from another document (the annex's version and review date). */
  version?: { label: string; version: string; updated: string };
}) {
  const t = translator(LEGAL_MESSAGES, lang);
  return (
    // The page frame is the kit PageFrame (the one <main>, AA-1008 S6f), restyled as the light page ground; the card is
    // the article inside it.
    <PageFrame style={{ minHeight: "100vh", background: "var(--bg-primary)", fontFamily: FONT_SANS, padding: "32px 16px", boxSizing: "border-box", overflowY: "visible" }}>
      <div lang={lang} data-theme="light">
      {/* Paragraph and list rhythm for the body text (inline styles cannot target descendants). */}
      <style>{`[data-legal-body] p{margin:0 0 10px}[data-legal-body] ul{list-style:disc;margin:0 0 10px;padding-left:20px}[data-legal-body] li{margin:0 0 6px}[data-legal-body] li ul{list-style:circle;margin:6px 0 0}`}</style>
      <article style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 16, padding: "28px 24px", width: "100%", maxWidth: 760, margin: "0 auto", boxSizing: "border-box" }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={LOGO_SRC} alt={t("logoAlt")} width={52} height={33} style={{ width: 52, height: "auto", display: "block", marginBottom: 16 }} />
        <h1 style={{ fontFamily: FONT_DISPLAY, fontWeight: 600, fontSize: 24, color: BRAND.ink, margin: "0 0 4px" }}>{doc.title}</h1>
        <p style={{ fontSize: 12, color: BRAND.muted, margin: "0 0 16px" }}>
          {version ? `${version.label} · ${version.version} · ${version.updated}` : t("versionLine", t("version"), t("updated"))}
        </p>
        <DraftBanner lang={lang} />
        <div style={{ fontSize: 14, lineHeight: 1.65, color: BRAND.body, marginTop: 16 }}>
          <p style={{ margin: 0 }}><Runs runs={doc.intro} /></p>
        </div>
        {doc.sections.map((s) =>
          s.title ? (
            <LegalSection key={s.id} id={s.id} title={s.title}>
              {s.blocks.map((b, i) => <BlockView key={i} block={b} />)}
            </LegalSection>
          ) : (
            // AA-970: an untitled section (the page title names it already) has no heading of its own.
            <section key={s.id} id={s.id} style={{ paddingTop: 16 }}>
              <div data-legal-body="" style={{ fontSize: 14, lineHeight: 1.65, color: BRAND.body }}>
                {s.blocks.map((b, i) => <BlockView key={i} block={b} />)}
              </div>
            </section>
          ),
        )}
        <nav aria-label={t("nav")} style={{ display: "flex", flexWrap: "wrap", gap: 16, fontSize: 12, marginTop: 28, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
          <a href={PRIVACY_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>{t("privacy")}</a>
          <a href={TERMS_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>{t("terms")}</a>
          <a href={DATA_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>{t("data")}</a>
          <a href={SUBPROCESSORS_PATH} style={{ color: BRAND.muted, textDecoration: "none" }}>{t("subprocessors")}</a>
          {/* S282: no prefetch. /portal/dashboard redirects a signed-out visitor to the sign-in host; a prefetch of it on a
              short page (footer in view) made the browser resolve that host on page load (Visual, legal-data 1440). */}
          <Link href="/portal/dashboard" prefetch={false} style={{ color: BRAND.muted, textDecoration: "none" }}>{t("login")}</Link>
        </nav>
      </article>
      </div>
    </PageFrame>
  );
}
