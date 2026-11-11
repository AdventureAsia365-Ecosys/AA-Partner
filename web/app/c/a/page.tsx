// web/app/c/a/page.tsx — AA-1071 (crm-056): a sales partner's own page on the client host (the trips it referred and
// its commission, after an e-mail code). Public: no agency session, no BFF; the /c/a/<token> landing set the host-only
// link cookie, the browser talks to /crm/public/v1/partner on this origin (components/partners/PartnerView.tsx).
// English unless the visitor saved a choice. Referrer-Policy no-referrer (next.config).
import { CenteredCard } from "@/components/CenteredCard";
import { PartnerView } from "@/components/partners/PartnerView";
import { dictionary } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

// Only the strings the page uses go to the browser.
const PREFIXES = ["partnerPage.", "partners.pcStatus."];

export default async function PartnerPage() {
  const { lang, locale } = await serverContext();
  const dict = Object.fromEntries(Object.entries(dictionary(lang)).filter(([k]) => PREFIXES.some((p) => k.startsWith(p))));
  return (
    <CenteredCard testId="partner-page" wide>
      <div lang={lang}>
        <PartnerView dict={dict} lang={lang} locale={locale} />
      </div>
    </CenteredCard>
  );
}
