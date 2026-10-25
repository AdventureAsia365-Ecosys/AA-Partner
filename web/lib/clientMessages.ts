// web/lib/clientMessages.ts
// AA-946 — the strings the CRM-18 client pages use (the proposal page /c/p/<token> and the traveller portal
// /c/t/<token>): only these go to the client page's browser, not the agency app's whole dictionary.

import { dictionary, type Lang, type MessageKey } from "./i18n";

export const CLIENT_PREFIXES = ["client.", "common."] as const;

export function clientMessages(lang: Lang): Partial<Record<MessageKey, string>> {
  const all = dictionary(lang);
  return Object.fromEntries(Object.entries(all).filter(([k]) => CLIENT_PREFIXES.some((p) => k.startsWith(p))));
}
