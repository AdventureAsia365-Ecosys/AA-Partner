// web/lib/bell.ts
// AA-950 (card CRM-21b) — the agency bell's shapes (GET /crm/v1/bell, api/app/proposals/events.py) and the wording of
// one bundled line: "bell.event.<event>" with the trip, the version and how many events it bundles.

export interface BellItem {
  trip_id: string;
  trip_title: string;
  proposal_id: string | null;
  version: number | null;
  /** "proposal.viewed", "proposal.option_chosen", "proposal.rehold" ... */
  name: string;
  count: number;
  last_at: string;
  detail: Record<string, unknown>;
}

export interface BellView {
  items: BellItem[];
  unseen: number;
}

export function bellLine(item: BellItem): { key: string; vars: Record<string, string | number> } {
  const event = item.name.includes(".") ? item.name.slice(item.name.indexOf(".") + 1) : item.name;
  return { key: `bell.event.${event}`, vars: { trip: item.trip_title, version: item.version ?? "", count: item.count } };
}
