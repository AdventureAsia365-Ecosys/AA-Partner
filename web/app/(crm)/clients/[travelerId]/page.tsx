// web/app/(crm)/clients/[travelerId]/page.tsx — A-07 Client profile (AA-937, card CRM-09; design v3 WF-3, §4.4).
// The API decrypts the display names (this client, related clients) and the document file names for this page and
// writes one "view" audit event; every identity field, the contact details and the health codes stay masked until a
// member presses Reveal and gives a reason. The profile panel is F2: bands and codes only.
import { notFound } from "next/navigation";
import { ErrorState } from "@/app/_kit";
import { ClientProfileView } from "@/components/clients/ClientProfileView";
import type { ClientProfile } from "@/lib/clients";
import { crmCall } from "@/lib/crmApi";
import { crmContext } from "@/lib/crmContext";
import { t } from "@/lib/i18n";
import type { Member } from "@/lib/pipeline";
import { serverContext } from "@/lib/serverContext";

export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function ClientPage({ params }: { params: Promise<{ travelerId: string }> }) {
  const { travelerId } = await params;
  if (!UUID.test(travelerId)) notFound();
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { session } = await serverContext();
  if (!session) return null;
  const [client, members] = await Promise.all([
    crmCall<ClientProfile>(ctx.cfg, session, "GET", `travelers/${travelerId}`),
    crmCall<{ members: Member[] }>(ctx.cfg, session, "GET", "members"),
  ]);
  if (!client.ok) {
    return <ErrorState message={t(ctx.lang, client.status === 404 ? "clients.notFound" : "clients.error")} />;
  }
  return (
    <ClientProfileView
      view={client.data}
      members={members.ok ? members.data.members : []}
      role={ctx.role}
      lang={ctx.lang}
      locale={ctx.locale}
    />
  );
}
