// web/lib/searchContent.ts
// AA-963 (CRM-32) — the person's Content pieces for the global search palette, read once per page through the
// studio BFF they already use (app/api/tenant: the CIS studio token bound to this sign-in, minted on demand). Only
// when the studio token's tenant is the CRM agency the palette searches: a person with several studios never sees
// another agency's pieces here. Any failure (no studio, CIS down, another tenant) is an empty list: the CRM results
// still show.

import type { ReviewItem } from "@/app/(studio)/portal/_components/reviewListApi";
import { pieceTitleOrTour } from "@/lib/studio/myContent";
import type { ContentPieceHit } from "./search";

let cached: { key: string; pieces: Promise<ContentPieceHit[]> } | null = null;

async function readJson<T>(path: string, fetchImpl: typeof fetch): Promise<T | null> {
  try {
    const res = await fetchImpl(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

/** The pieces as palette hits, for the agency `tenantId` only. Pure apart from the two reads. */
export async function fetchContentPieces(tenantId: string, lang: "en" | "vi", fetchImpl: typeof fetch = fetch): Promise<ContentPieceHit[]> {
  const me = await readJson<{ tenant_id?: string }>("/api/tenant/me", fetchImpl);
  if (!me || !tenantId || me.tenant_id !== tenantId) return [];
  const list = await readJson<{ data?: ReviewItem[] }>("/api/tenant/v1/content-writing/reviews", fetchImpl);
  return (list?.data ?? []).map((r) => ({ pieceId: r.piece_id, title: pieceTitleOrTour(r, lang), tour: r.tour?.name ?? null }));
}

/** :func:`fetchContentPieces`, once per page and agency (the palette opens many times). */
export function loadContentPieces(tenantId: string, lang: "en" | "vi"): Promise<ContentPieceHit[]> {
  const key = `${tenantId}:${lang}`;
  if (!cached || cached.key !== key) cached = { key, pieces: fetchContentPieces(tenantId, lang) };
  return cached.pieces;
}
