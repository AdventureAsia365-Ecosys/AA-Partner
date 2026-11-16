// web/app/(studio)/legal/subprocessors/page.tsx
// AA-970 (design v3 card CRM-37b) — /legal/subprocessors, the public sub-processor list: rendered from the root DPA
// Annex 3 public summary (its second section, one card per sub-processor; lib/legal/annexMarkdown.ts), in the frame of
// the studio's draft legal pages. Public (lib/hosts.ts), not indexed while the annex is a draft.
import type { Metadata } from "next";
import LegalPage from "@/app/(studio)/_legal/LegalPage";
import { annexDoc, annexVersion } from "@/app/(studio)/_legal/annexDoc";
import { serverContext } from "@/lib/serverContext";

export async function generateMetadata(): Promise<Metadata> {
  const { lang } = await serverContext();
  return { title: annexDoc("subprocessors", lang).metaTitle, robots: { index: false, follow: true } };
}

export default async function SubprocessorsPage() {
  const { lang } = await serverContext();
  return <LegalPage doc={annexDoc("subprocessors", lang)} lang={lang} version={annexVersion(lang)} />;
}
