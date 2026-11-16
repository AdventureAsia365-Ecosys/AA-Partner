// web/lib/searchContent.ts
// AA-963 (CRM-32) — the person's Content pieces for the global search palette, read through the studio BFF they
// already use (app/api/tenant: the CIS studio token bound to this sign-in, minted on demand). Only when the studio
// token's tenant is the CRM agency the palette searches: a person with several studios never sees another agency's
// pieces here. Any failure (no studio, CIS down, another tenant) is an empty list: the CRM results still show.
// Cache (AA-963 review): one read per palette open (a piece deleted or added since shows correctly without a reload),
// reused while that palette stays open; a failed read is never kept, the next call asks again.

import type { ReviewItem } from "@/app/(studio)/portal/_components/reviewListApi";
import { pieceTitleOrTour } from "@/lib/studio/myContent";
import type { ContentPieceHit } from "./search";

async function readJson<T>(path: string, fetchImpl: typeof fetch): Promise<T | null> {
  try {
    const res = await fetchImpl(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

/** One read of the pieces: `ok` false when a read failed (the empty list is then not a fact worth keeping). */
interface ContentRead {
  ok: boolean;
  pieces: ContentPieceHit[];
}

async function readContentPieces(tenantId: string, lang: "en" | "vi", fetchImpl: typeof fetch): Promise<ContentRead> {
  const me = await readJson<{ tenant_id?: string }>("/api/tenant/me", fetchImpl);
  if (!me) return { ok: false, pieces: [] };
  if (!tenantId || me.tenant_id !== tenantId) return { ok: true, pieces: [] }; // another agency's studio: none here
  const list = await readJson<{ data?: ReviewItem[] }>("/api/tenant/v1/content-writing/reviews", fetchImpl);
  if (!list) return { ok: false, pieces: [] };
  return {
    ok: true,
    pieces: (list.data ?? []).map((r) => ({ pieceId: r.piece_id, title: pieceTitleOrTour(r, lang), tour: r.tour?.name ?? null })),
  };
}

/** The pieces as palette hits, for the agency `tenantId` only. Pure apart from the two reads. */
export async function fetchContentPieces(tenantId: string, lang: "en" | "vi", fetchImpl: typeof fetch = fetch): Promise<ContentPieceHit[]> {
  return (await readContentPieces(tenantId, lang, fetchImpl)).pieces;
}

let cached: { key: string; session: object; read: Promise<ContentRead> } | null = null;

/** :func:`fetchContentPieces` for the agency, read once per palette open (`session`: one object per open, so a piece
 * deleted or added since the last open shows correctly) and reused while that palette stays open; a failed read is
 * dropped at once, so the next call reads again. */
export function loadContentPieces(
  tenantId: string,
  lang: "en" | "vi",
  session: object,
  fetchImpl: typeof fetch = fetch,
): Promise<ContentPieceHit[]> {
  const key = `${tenantId}:${lang}`;
  let entry = cached;
  if (!entry || entry.key !== key || entry.session !== session) {
    const fresh = { key, session, read: readContentPieces(tenantId, lang, fetchImpl) };
    void fresh.read.then((r) => {
      if (!r.ok && cached === fresh) cached = null; // never keep a failure
    });
    cached = fresh;
    entry = fresh;
  }
  return entry.read.then((r) => r.pieces);
}

/** Forget the cached pieces (tests). */
export function resetContentPieces(): void {
  cached = null;
}
