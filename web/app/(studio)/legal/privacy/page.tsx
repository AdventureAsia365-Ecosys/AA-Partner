// web/app/(studio)/legal/privacy/page.tsx
// AA-886 — /privacy: DRAFT privacy notice of the Partner Portal (replaces the AA-885 redirect to
// adventure.asia/privacy-policy, which does not describe portal data). Not indexed while it is a draft.
// AA-1036 (R3): the text is ./privacy.messages.ts (English and Vietnamese), shown in the UI language.
import type { Metadata } from "next";
import LegalPage from "@/app/(studio)/_legal/LegalPage";
import { localized } from "@/app/_kit/core/i18n";
import { serverContext } from "@/lib/serverContext";
import { PRIVACY_DOC } from "./privacy.messages";

export async function generateMetadata(): Promise<Metadata> {
  const { lang } = await serverContext();
  return {
    title: localized(PRIVACY_DOC, lang).metaTitle,
    robots: { index: false, follow: true },
  };
}

export default async function PrivacyPage() {
  const { lang } = await serverContext();
  return <LegalPage doc={localized(PRIVACY_DOC, lang)} lang={lang} />;
}
