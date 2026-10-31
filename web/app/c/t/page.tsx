// web/app/c/t/page.tsx — the traveller portal per person (AA-946 base, AA-953 card CRM-24a; design v3 C-05 .. C-10) on
// the client host. Public: no agency session, no BFF. The traveller's own link was exchanged for the host-only portal
// cookie by /c/t/<token> (§2.9 rule 1); the browser talks to /crm/public/v1/traveller on this origin with that cookie,
// and the API keeps the portal session in an httpOnly cookie on the same path, so this page never holds either.
// English unless the visitor saved a choice (owner rule S249). Referrer-Policy no-referrer (next.config).
import type { Metadata, Viewport } from "next";
import { CenteredCard } from "@/components/CenteredCard";
import { PortalPage } from "@/components/portal/PortalPage";
import { clientMessages } from "@/lib/clientMessages";
import { PORTAL_API } from "@/lib/clientPortal";
import { t } from "@/lib/i18n";
import { APPLE_TOUCH_ICON, manifestHref, THEME_COLOR } from "@/lib/portalManifest";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

// AA-954 (card CRM-24b, R16): installable. The manifest (start URL and scope /c/t) in the visitor's language, the iOS
// home-screen icon and title; the service worker is registered by the page (components/portal/PortalPage.tsx).
export async function generateMetadata(): Promise<Metadata> {
  const { lang } = await serverContext();
  return {
    manifest: manifestHref(lang),
    appleWebApp: { capable: true, title: t(lang, "client.manifest.name"), statusBarStyle: "default" },
    icons: { apple: APPLE_TOUCH_ICON },
  };
}

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: THEME_COLOR };

export default async function TravellerPortalPage() {
  const { lang, locale } = await serverContext();
  return (
    <CenteredCard testId="client-portal-page" wide>
      <div lang={lang}>
        <PortalPage base={PORTAL_API} dict={clientMessages(lang)} locale={locale} offline />
      </div>
    </CenteredCard>
  );
}
