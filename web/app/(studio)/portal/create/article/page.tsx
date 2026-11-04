"use client";
// web/app/(studio)/portal/create/article/page.tsx — Create › Articles (AA-759): the same Slate as Social
// Posts, on the blog Channel only (search-led: measured demand and questions to answer).
import { PageHeader, useT } from "@/app/_kit";
import { PAGES_MESSAGES } from "../../content/content.messages";
import PlanningTab from "../../_components/PlanningTab";

export default function CreateArticlePage() {
  const t = useT(PAGES_MESSAGES);
  return (
    <>
      <PageHeader title={t("articlesTitle")} description={t("articlesSub")} />
      <PlanningTab kind="article" />
    </>
  );
}
