// web/app/(studio)/legal/partner-terms/page.tsx
// AA-886 — /terms (now /legal/partner-terms): DRAFT partner terms of the Partner Portal. Not indexed while it is a
// draft. AA-1036 (R3): the text is ./terms.messages.ts (English and Vietnamese), shown in the UI language.
import type { Metadata } from "next";
import LegalPage from "@/app/(studio)/_legal/LegalPage";
import { localized } from "@/app/_kit/core/i18n";
import { serverContext } from "@/lib/serverContext";
import { TERMS_DOC } from "./terms.messages";

export async function generateMetadata(): Promise<Metadata> {
  const { lang } = await serverContext();
  return {
    title: localized(TERMS_DOC, lang).metaTitle,
    robots: { index: false, follow: true },
  };
}

export default async function TermsPage() {
  const { lang } = await serverContext();
  return <LegalPage doc={localized(TERMS_DOC, lang)} lang={lang} />;
}
