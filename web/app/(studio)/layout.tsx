// web/app/(studio)/layout.tsx — AA-1036 (R3 slice 1, root ADR 0019 d6): the Content studio's part of the one agency
// app (route group "studio": /portal/*, /portal-access/*, /legal/partner-terms, /legal/privacy). It was the root layout
// of the separate studio app (AA-885): the studio's stylesheet (Tailwind preflight + the studio rules, ./studio.css)
// and the kit react-query provider. The fonts, the theme restore script and the sidebar restore script are the root
// layout's (app/layout.tsx), shared with the CRM. English only, as before (studio i18n is a later R3 lane).
import type { Metadata } from "next";
import "./studio.css";
import { Providers } from "@/app/_kit/core/Providers";

export const metadata: Metadata = {
  title: "Adventure Asia — Partner Portal",
  description: "Adventure Asia B2B partner portal",
};

export default function StudioLayout({ children }: { children: React.ReactNode }) {
  return <Providers>{children}</Providers>;
}
