// web/app/(studio)/legal/data/page.tsx
// AA-970 (design v3 card CRM-37b) — /legal/data, the public page "How we handle your data": rendered from the root DPA
// Annex 3 public summary (its first section; lib/legal/annexMarkdown.ts), in the frame of the studio's draft legal
// pages. Public (lib/hosts.ts), not indexed while the annex is a draft.
import type { Metadata } from "next";
import LegalPage from "@/app/(studio)/_legal/LegalPage";
import { annexDoc, annexVersion } from "@/app/(studio)/_legal/annexDoc";
import { serverContext } from "@/lib/serverContext";

export async function generateMetadata(): Promise<Metadata> {
  const { lang } = await serverContext();
  return { title: annexDoc("data", lang).metaTitle, robots: { index: false, follow: true } };
}

export default async function DataPage() {
  const { lang } = await serverContext();
  return <LegalPage doc={annexDoc("data", lang)} lang={lang} version={annexVersion(lang)} />;
}
