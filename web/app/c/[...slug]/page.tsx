// web/app/c/[...slug]/page.tsx — the client host's pages (travellers and prospects, design v3 §2.4: inquiry form,
// planning form, proposal, portal). AA-935 reserves the path and the host routing; the pages arrive with AA-936
// (C-01 inquiry form) and later cards. No session, no agency data, no BFF here.
import { CenteredCard } from "@/components/CenteredCard";
import { K } from "@/app/_kit";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

export default async function ClientPage() {
  const { lang } = await serverContext();
  return (
    <CenteredCard testId="crm-client-page">
      <h1 style={{ margin: "0 0 6px", fontSize: 18, color: K.ink }}>{t(lang, "client.title")}</h1>
      <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{t(lang, "client.body")}</p>
    </CenteredCard>
  );
}
