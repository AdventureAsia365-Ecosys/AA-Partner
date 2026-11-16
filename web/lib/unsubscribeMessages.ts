// web/lib/unsubscribeMessages.ts
// AA-968 — the strings of the public unsubscribe page (C-11, components/campaigns/UnsubscribePage.tsx): only these go
// to the client's browser, not the agency app's whole dictionary (the lib/planMessages.ts pattern).

import { dictionary, type Lang, type MessageKey } from "./i18n";
import { UNSUB_PREFIX } from "./i18nCampaigns";

export function unsubscribeMessages(lang: Lang): Partial<Record<MessageKey, string>> {
  const all = dictionary(lang);
  return Object.fromEntries(Object.entries(all).filter(([k]) => k.startsWith(UNSUB_PREFIX)));
}
