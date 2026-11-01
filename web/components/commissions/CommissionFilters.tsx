"use client";
// web/components/commissions/CommissionFilters.tsx — A-12's status filter (card CRM-27). S262 (AA-957 live): the kit
// underline tabs, so the active filter is marked (accent underline, aria-selected) instead of six identical links; a
// tab moves to the filter's own URL (/commissions?status=...), so a reload or a shared link keeps it. S262 round 3: the
// shared FilterTabs (also /payments) keeps the active tab in view on a phone.
import { FilterTabs } from "@/components/FilterTabs";
import { type CommissionFilter, FILTERS } from "@/lib/commissions";
import { t, type Lang, type MessageKey } from "@/lib/i18n";

export function filterHref(f: CommissionFilter): string {
  return f === "open" ? "/commissions" : `/commissions?status=${f}`;
}

export function CommissionFilters({ active, lang }: { active: CommissionFilter; lang: Lang }) {
  return (
    <FilterTabs
      testId="commissions-filters"
      ariaLabel={t(lang, "commissions.filterLabel")}
      active={active}
      tabs={FILTERS.map((f) => ({ key: f, label: t(lang, `commissions.filter.${f}` as MessageKey), href: filterHref(f) }))}
    />
  );
}
