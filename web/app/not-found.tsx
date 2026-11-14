// web/app/not-found.tsx — AA-935. AA-1105: on the academy host (x-aa-host-kind, set by proxy.ts) the card says
// "AA Academy" and Home is the academy's catalogue.
import Link from "next/link";
import { headers } from "next/headers";
import { CenteredCard } from "@/components/CenteredCard";
import { K } from "@/app/_kit";

export default async function NotFound() {
  const academy = (await headers()).get("x-aa-host-kind") === "academy";
  return (
    <CenteredCard testId="crm-not-found" brand={academy ? "AA Academy" : undefined}>
      <h1 style={{ margin: "0 0 6px", fontSize: 18, color: K.ink }}>404</h1>
      <p style={{ margin: 0, fontSize: 13.5, color: K.muted }}>
        This page does not exist. <Link href={academy ? "/academy" : "/"}>Home</Link>
      </p>
    </CenteredCard>
  );
}
