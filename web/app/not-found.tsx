// web/app/not-found.tsx — AA-935. AA-1105: on the academy host (x-aa-host-kind, set by proxy.ts) the card says
// "AA Academy" and Home is the academy's catalogue. AA-1129 (P5): on the client host (white-label, the agency's
// travellers) the card has no brand mark and no Home link: the client host has no home page ("/" is 404 there), so the
// link looped to another 404.
import Link from "next/link";
import { headers } from "next/headers";
import { CenteredCard } from "@/components/CenteredCard";
import { K } from "@/app/_kit";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export default async function NotFound() {
  const kind = (await headers()).get("x-aa-host-kind");
  const academy = kind === "academy";
  if (kind === "client") {
    const { lang } = await serverContext();
    return (
      <CenteredCard testId="crm-not-found" brand={null}>
        <h1 style={{ margin: "0 0 6px", fontSize: 18, color: K.ink }}>404</h1>
        <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>{t(lang, "client.notFound")}</p>
      </CenteredCard>
    );
  }
  return (
    <CenteredCard testId="crm-not-found" brand={academy ? "AA Academy" : undefined}>
      <h1 style={{ margin: "0 0 6px", fontSize: 18, color: K.ink }}>404</h1>
      <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>
        This page does not exist. <Link href={academy ? "/academy" : "/"}>Home</Link>
      </p>
    </CenteredCard>
  );
}
