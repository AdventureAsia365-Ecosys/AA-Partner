// web/app/c/f/[publicKey]/page.tsx — C-01, the inquiry form on the client host (AA-936, card CRM-08; design v3 §2.4).
// Public: no session, no agency data beyond the form itself. The definition comes from the API's public route
// (lib/intakeForms.ts, 60 s memo shared with the proxy's CSP); the page words every field in EN/VI (the visitor's
// cookie / Accept-Language, else the agency's default locale) and the browser posts to /crm/public/v1 on this origin.
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { CenteredCard } from "@/components/CenteredCard";
import { InquiryForm } from "@/components/intake/InquiryForm";
import { K } from "@/app/_kit";
import { loadForm, viewerKey } from "@/lib/intakeForms";
import { dictionary, fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

// Only the strings the form uses go to the browser (not the agency app's dictionary).
const FORM_PREFIXES = ["intake.", "dest.", "party.", "budget.", "style.", "channel.", "heard."];

function formMessages(lang: Lang): Record<MessageKey, string> {
  const all = dictionary(lang);
  return Object.fromEntries(
    Object.entries(all).filter(([k]) => FORM_PREFIXES.some((p) => k.startsWith(p))),
  ) as Record<MessageKey, string>;
}

export default async function InquiryPage({ params }: { params: Promise<{ publicKey: string }> }) {
  const { publicKey } = await params;
  if (!/^[A-Za-z0-9_-]{20,64}$/.test(publicKey)) notFound();
  const base = await serverContext();
  const viewer = viewerKey((await headers()).get("cloudfront-viewer-address"));
  const found = base.cfg.crmApiOrigin
    ? await loadForm(base.cfg.crmApiOrigin, publicKey, { viewer })
    : { ok: false as const, status: 0 };
  if (!found.ok) {
    return (
      <CenteredCard testId="intake-unavailable">
        <p style={{ margin: 0, fontSize: 14, color: K.body }}>
          {t(base.lang, found.status === 404 ? "intake.notFound" : "intake.error")}
        </p>
      </CenteredCard>
    );
  }
  const { lang } = await serverContext(found.form.default_locale);
  return (
    <CenteredCard testId="intake-page" wide>
      <h1 style={{ margin: "0 0 4px", fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>
        {fmt(t(lang, "intake.title"), { agency: found.form.agency_name })}
      </h1>
      <p style={{ margin: "0 0 16px", fontSize: 13.5, color: K.muted }}>{t(lang, "intake.intro")}</p>
      <InquiryForm publicKey={publicKey} form={found.form} lang={lang} dict={formMessages(lang)} />
    </CenteredCard>
  );
}
