"use client";
// portal/app/portal/account/activity/page.tsx — AA-430 route migration.
// AA-759: was /portal/activity (308 from the old slug, next.config.ts).
// AA-761: the tenant's own activity trail (GET /v1/account/activity), kit DataTable with server paging, type and
// date filters and a CSV export of the filtered set (_components/AccountActivity.tsx). Was the five-row rewrite
// feed of /v1/billing (ActivityLogTab), which the dashboard and the bell still show.
import AccountActivity from "../../_components/AccountActivity";

export default function ActivityPage() {
  return <AccountActivity />;
}
