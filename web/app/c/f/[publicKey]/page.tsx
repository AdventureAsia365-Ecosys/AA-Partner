// web/app/c/f/[publicKey]/page.tsx — C-01, the inquiry form on the client host (AA-936, card CRM-08; design v3 §2.4).
// Public: no session, no agency data beyond the form itself. The definition comes from the API's public route
// (lib/intakeForms.ts, 60 s memo shared with the proxy's CSP); the page words every field in English unless the form
// itself sets another language (`locale` in its definition) or the visitor saved a choice (cookie) — the agency's
// default_locale does not switch it (AA-936, owner rule S249). The browser posts to /crm/public/v1 on this origin.
// AA-1129 (P5, S283 audit): white-label — the agency's name is the brand mark and the tab title (never the partner
// app's name); an unknown key answers a real 404 (not-found.tsx beside this page, same card); the visitor can switch
// EN / VI with ?lang= (the client host has no /prefs route, and the choice applies to this visit only).
import type { Metadata } from "next";
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { CenteredCard } from "@/components/CenteredCard";
import { InquiryForm } from "@/components/intake/InquiryForm";
import { K } from "@/app/_kit";
import { loadForm, viewerKey, type FormLookup } from "@/lib/intakeForms";
import { dictionary, explicitLocale, fmt, langOf, t, type Lang, type MessageKey } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

// Only the strings the form uses go to the browser (not the agency app's dictionary).
const FORM_PREFIXES = ["intake.", "dest.", "party.", "budget.", "style.", "channel.", "heard."];
const KEY = /^[A-Za-z0-9_-]{20,64}$/;
const LANGS: { lang: Lang; label: string }[] = [
  { lang: "en", label: "English" },
  { lang: "vi", label: "Tiếng Việt" },
];

function formMessages(lang: Lang): Record<MessageKey, string> {
  const all = dictionary(lang);
  return Object.fromEntries(
    Object.entries(all).filter(([k]) => FORM_PREFIXES.some((p) => k.startsWith(p))),
  ) as Record<MessageKey, string>;
}

async function lookup(publicKey: string): Promise<FormLookup> {
  const base = await serverContext();
  const viewer = viewerKey((await headers()).get("cloudfront-viewer-address"));
  return base.cfg.crmApiOrigin ? loadForm(base.cfg.crmApiOrigin, publicKey, { viewer }) : { ok: false, status: 0 };
}

/** The page language: the visitor's ?lang= choice for this visit, else the cookie, else the form's own language. */
async function pageLang(formLocale: string | undefined, chosen: string | undefined): Promise<Lang> {
  const pick = explicitLocale(chosen);
  if (pick) return langOf(pick);
  return (await serverContext(formLocale)).lang;
}

export async function generateMetadata({
  params,
  searchParams,
}: {
  params: Promise<{ publicKey: string }>;
  searchParams: Promise<{ lang?: string }>;
}): Promise<Metadata> {
  const { publicKey } = await params;
  if (!KEY.test(publicKey)) return {};
  const found = await lookup(publicKey);
  if (!found.ok) return {};
  const lang = await pageLang(found.form.locale, (await searchParams).lang);
  return { title: fmt(t(lang, "intake.title"), { agency: found.form.agency_name }) };
}

export default async function InquiryPage({
  params,
  searchParams,
}: {
  params: Promise<{ publicKey: string }>;
  searchParams: Promise<{ lang?: string }>;
}) {
  const { publicKey } = await params;
  if (!KEY.test(publicKey)) notFound();
  const found = await lookup(publicKey);
  if (!found.ok) {
    if (found.status === 404) notFound();
    const { lang } = await serverContext();
    return (
      <CenteredCard testId="intake-unavailable" brand={null}>
        <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t(lang, "intake.error")}</p>
      </CenteredCard>
    );
  }
  const lang = await pageLang(found.form.locale, (await searchParams).lang);
  return (
    <CenteredCard testId="intake-page" wide brand={found.form.agency_name}>
      {/* The root <html lang> follows the visitor's cookie only; this block states the form's own language. */}
      <div lang={lang} data-testid="intake-page-body">
        <nav aria-label={t(lang, "intake.language")} data-testid="intake-lang" style={{ display: "flex", justifyContent: "flex-end", gap: 10, fontSize: 12.5, marginBottom: 6 }}>
          {LANGS.map((l) => (
            <a
              key={l.lang}
              href={`?lang=${l.lang}`}
              lang={l.lang}
              hrefLang={l.lang}
              aria-current={l.lang === lang ? "true" : undefined}
              style={{ color: l.lang === lang ? K.ink : K.accentDeep, fontWeight: l.lang === lang ? 600 : 400, textDecoration: l.lang === lang ? "none" : "underline" }}
            >
              {l.label}
            </a>
          ))}
        </nav>
        <h1 style={{ margin: "0 0 4px", fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>
          {fmt(t(lang, "intake.title"), { agency: found.form.agency_name })}
        </h1>
        <p style={{ margin: "0 0 16px", fontSize: 13.5, color: K.muted }}>{t(lang, "intake.intro")}</p>
        <InquiryForm publicKey={publicKey} form={found.form} lang={lang} dict={formMessages(lang)} />
      </div>
    </CenteredCard>
  );
}
