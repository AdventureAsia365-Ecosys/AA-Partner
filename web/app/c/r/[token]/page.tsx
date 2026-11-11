// web/app/c/r/[token]/page.tsx — AA-1071 (crm-056): a sales partner's inquiry link on the client host. The agency's
// inquiry form the owner chose for this partner (GET /crm/public/v1/r/<token>, the public form's answer); the browser
// posts to /crm/public/v1/r/<token>/submissions, which marks the inquiry (then the trip) as the partner's. Public: no
// session, no agency data beyond the form. English unless the form sets another language or the visitor saved one.
import { notFound } from "next/navigation";
import { K } from "@/app/_kit";
import { CenteredCard } from "@/components/CenteredCard";
import { InquiryForm } from "@/components/intake/InquiryForm";
import { dictionary, fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import type { PublicForm } from "@/lib/intakeForms";
import { CLIENT_TOKEN } from "@/lib/proposals";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const FORM_PREFIXES = ["intake.", "dest.", "party.", "budget.", "style.", "channel.", "heard."];

function formMessages(lang: Lang): Record<MessageKey, string> {
  const all = dictionary(lang);
  return Object.fromEntries(Object.entries(all).filter(([k]) => FORM_PREFIXES.some((p) => k.startsWith(p)))) as Record<MessageKey, string>;
}

async function loadPartnerForm(apiOrigin: string, token: string): Promise<{ ok: true; form: PublicForm } | { ok: false; status: number }> {
  try {
    const res = await fetch(`${apiOrigin}/crm/public/v1/r/${token}`, { headers: { Accept: "application/json" }, cache: "no-store", signal: AbortSignal.timeout(8_000) });
    return res.ok ? { ok: true, form: (await res.json()) as PublicForm } : { ok: false, status: res.status };
  } catch {
    return { ok: false, status: 0 };
  }
}

export default async function PartnerIntakePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  if (!CLIENT_TOKEN.test(token)) notFound();
  const base = await serverContext();
  const found = base.cfg.crmApiOrigin ? await loadPartnerForm(base.cfg.crmApiOrigin, token) : { ok: false as const, status: 0 };
  if (!found.ok) {
    return (
      <CenteredCard testId="partner-intake-unavailable">
        <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t(base.lang, found.status === 404 ? "partnerPage.notFound" : "intake.error")}</p>
      </CenteredCard>
    );
  }
  const { lang } = await serverContext(found.form.locale);
  return (
    <CenteredCard testId="partner-intake-page" wide>
      <div lang={lang}>
        <h1 style={{ margin: "0 0 4px", fontSize: 20, color: K.ink, overflowWrap: "anywhere" }}>
          {fmt(t(lang, "partnerPage.intakeTitle"), { agency: found.form.agency_name })}
        </h1>
        <p style={{ margin: "0 0 16px", fontSize: 13.5, color: K.muted }}>{t(lang, "partnerPage.intakeIntro")}</p>
        <InquiryForm publicKey={token} form={found.form} lang={lang} dict={formMessages(lang)} submitPath={`/crm/public/v1/r/${token}/submissions`} />
      </div>
    </CenteredCard>
  );
}
