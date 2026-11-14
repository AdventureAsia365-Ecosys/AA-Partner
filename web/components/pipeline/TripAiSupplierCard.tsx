// web/components/pipeline/TripAiSupplierCard.tsx — AA-965 (R10): the trip page's "AI reads a supplier confirmation"
// card, shown only when the agency may use AI (the plan's ai_assist and the AI switch, GET settings/ai) and the trip's
// lines were read. Takes the page's raw API results so the page itself stays a flat list of cards.
import { AiSupplierConfirmPanel } from "@/components/pipeline/AiSupplierConfirmPanel";
import type { AiSettings } from "@/lib/ai";
import type { ApiResult } from "@/lib/crmApi";
import type { Lang } from "@/lib/i18n";
import type { SuppliersView, TripLines } from "@/lib/ledger";

export function TripAiSupplierCard({
  tripId,
  ai,
  lines,
  suppliers,
  lang,
  closed,
}: {
  tripId: string;
  ai: ApiResult<AiSettings>;
  lines: ApiResult<TripLines>;
  suppliers: ApiResult<SuppliersView>;
  lang: Lang;
  closed: boolean;
}) {
  if (!ai.ok || !lines.ok || !ai.data.ai_enabled || ai.data.ai_assist_allowed === false) return null;
  return (
    <AiSupplierConfirmPanel
      tripId={tripId}
      lines={lines.data.lines}
      suppliers={suppliers.ok ? suppliers.data.suppliers : []}
      lang={lang}
      closed={closed}
    />
  );
}
