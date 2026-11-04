"use client";
// web/app/(studio)/portal/create/social/page.tsx — Create › Social Posts (T7 Slate, every Channel but blog).
// AA-759: was /portal/t7-planning ("Social Content"; 308 from the old slug, next.config.ts).
import { PageHeader, useT } from "@/app/_kit";
import { PAGES_MESSAGES } from "../../content/content.messages";
import PlanningTab from "../../_components/PlanningTab";

export default function CreateSocialPage() {
  const t = useT(PAGES_MESSAGES);
  return (
    <>
      <PageHeader title={t("socialTitle")} description={t("socialSub")} />
      <PlanningTab kind="social" />
    </>
  );
}
