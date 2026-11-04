// web/app/(studio)/layout.tsx — AA-1036 (R3 slice 1, root ADR 0019 d6): the Content studio's part of the one agency
// app (route group "studio": /portal/*, /portal-access/*, /legal/partner-terms, /legal/privacy). It was the root layout
// of the separate studio app (AA-885): the studio's stylesheet (Tailwind preflight + the studio rules, ./studio.css)
// and the kit react-query provider. The fonts, the theme restore script and the sidebar restore script are the root
// layout's (app/layout.tsx), shared with the CRM.
// AA-1036 (R3, studio EN/VI): the UI language is the app's one setting (cookie aa_crm_locale, the CRM's switch; absent
// = English), resolved here per request and handed to the kit i18n provider (app/_kit/local/studio/StudioLang.tsx).
import type { Metadata } from "next";
import "./studio.css";
import { Providers } from "@/app/_kit/core/Providers";
import { StudioLang } from "@/app/_kit/local/studio/StudioLang";
import { translator } from "@/app/_kit";
import { serverContext } from "@/lib/serverContext";
import { STUDIO_MESSAGES } from "@/app/_kit/local/studio/studio.messages";

export async function generateMetadata(): Promise<Metadata> {
  const { lang } = await serverContext();
  const t = translator(STUDIO_MESSAGES, lang);
  return { title: t("metaTitle"), description: t("metaDescription") };
}

export default async function StudioLayout({ children }: { children: React.ReactNode }) {
  const { lang, locale } = await serverContext();
  return (
    <StudioLang lang={lang} locale={locale}>
      <Providers>{children}</Providers>
    </StudioLang>
  );
}
