// web/lib/offlineItinerary.ts
// AA-954 (card CRM-24b, R16; design v3 C-09 "installable, offline", §2.9 rule 7 = Fix A2, WF-8 "ITINERARY (works
// offline)"). The one thing the traveller portal keeps on the device: the itinerary of the signed-in person (the trip's
// days with their dates, local times and places, the services as travellers see them, and the agency contact), plus
// when it was saved. Documents, Stage 3, signing, flights, the group and payments stay online only.
//
//   - built by COPYING NAMED FIELDS (an allow-list), never by deleting fields from the home: a field the API adds later
//     never reaches the device by accident (card R: a cache inspection finds no document, Stage 3 or F1 field);
//   - kept in Cache Storage (`OFFLINE_DATA_CACHE`), the place a cache inspection looks, one entry for the whole origin;
//   - replaced on every signed-in answer (server wins: the copy is read-only, nothing is ever merged back), dropped at
//     sign-out, when the link is gone (404) or past its expiry, and when a signed-out answer belongs to another link
//     (another agency's or another traveller's link on the same device);
//   - shown only while the network is unreachable, with "last updated".
//
// The service worker (lib/portalServiceWorker.ts) caches the page shell only; it never touches this entry's content.

import type { PortalDay, PortalService } from "./clientPortal";

/** Bump with any change of {@link OfflineItinerary}'s shape: an older entry is then ignored and replaced. */
export const OFFLINE_VERSION = 1;
export const OFFLINE_CACHE_PREFIX = "aa-portal-";
export const OFFLINE_DATA_CACHE = `${OFFLINE_CACHE_PREFIX}data-v${OFFLINE_VERSION}`;
/** The key of the one entry (never fetched from the network: Cache Storage needs a same-origin URL as its key). */
export const OFFLINE_DATA_KEY = "/c/t/offline-itinerary.json";

export interface OfflineContact {
  email: string;
}

export interface OfflineItinerary {
  v: typeof OFFLINE_VERSION;
  ref: string;
  saved_at: string;
  expires_at: string | null;
  agency_name: string;
  contact: OfflineContact | null;
  trip: { title: string; start_date: string | null; end_date: string | null };
  days: PortalDay[];
  services: PortalService[];
}

/** The fields of the portal home this module reads (the rest of the home is never looked at). */
export interface OfflineSource {
  signed_in: boolean;
  agency_name?: unknown;
  offline_ref?: unknown;
  link_expires_at?: unknown;
  agency_contact?: unknown;
  trip?: unknown;
  itinerary?: unknown;
  services?: unknown;
}

const MAX_TEXT = 300;
const MAX_ROWS = 120;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const OFFSET = /^[+-]\d{2}:\d{2}$/;
const ZONE = /^(UTC|[A-Za-z]+(\/[A-Za-z0-9_+-]+){1,2})$/;
const EMAIL = /^[^\s@]{1,64}@[^\s@]{1,190}$/;

function text(v: unknown, max = MAX_TEXT): string | null {
  return typeof v === "string" && v.length > 0 ? v.slice(0, max) : null;
}

function date(v: unknown): string | null {
  return typeof v === "string" && DATE.test(v) ? v : null;
}

function rec(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function rows(v: unknown): Record<string, unknown>[] {
  return Array.isArray(v) ? v.slice(0, MAX_ROWS).map(rec).filter((r): r is Record<string, unknown> => r !== null) : [];
}

function day(r: Record<string, unknown>): PortalDay | null {
  if (typeof r.day_no !== "number" || !Number.isInteger(r.day_no)) return null;
  return {
    day_no: r.day_no,
    before_you_go: r.before_you_go === true,
    date: date(r.date),
    title: text(r.title),
    place: text(r.place),
    tz: typeof r.tz === "string" && ZONE.test(r.tz) ? r.tz : "UTC",
    utc_offset: typeof r.utc_offset === "string" && OFFSET.test(r.utc_offset) ? r.utc_offset : "+00:00",
  };
}

function service(r: Record<string, unknown>): PortalService | null {
  const title = text(r.title);
  if (!title) return null;
  return {
    title,
    from: date(r.from),
    to: date(r.to),
    status: r.status === "confirmed" ? "confirmed" : "planning",
    confirm_class: null, // the class is a booking detail the offline view never shows
    pay_on_arrival: r.pay_on_arrival === true,
  };
}

/**
 * The offline copy of a SIGNED-IN home, or null (a signed-out home, no `offline_ref`). Only the named fields below are
 * copied; everything else of the home (Stage 3, the waiver and envelopes, documents, flights, the passport flag, the
 * group, names, payments) is never read.
 */
export function offlineCopy(home: OfflineSource, now: Date): OfflineItinerary | null {
  if (home.signed_in !== true) return null;
  const ref = text(home.offline_ref, 64);
  if (!ref) return null;
  const trip = rec(home.trip);
  const contact = rec(home.agency_contact);
  const email = contact && typeof contact.email === "string" && EMAIL.test(contact.email) ? contact.email : null;
  const expires = typeof home.link_expires_at === "string" && !Number.isNaN(Date.parse(home.link_expires_at)) ? home.link_expires_at : null;
  return {
    v: OFFLINE_VERSION,
    ref,
    saved_at: now.toISOString(),
    expires_at: expires,
    agency_name: text(home.agency_name, 120) ?? "",
    contact: email ? { email } : null,
    trip: { title: text(trip?.title) ?? "", start_date: date(trip?.start_date), end_date: date(trip?.end_date) },
    days: rows(home.itinerary).map(day).filter((d): d is PortalDay => d !== null),
    services: rows(home.services).map(service).filter((s): s is PortalService => s !== null),
  };
}

/** Whether a stored copy may still be shown: the current shape and its link not yet expired. */
export function usable(copy: OfflineItinerary | null, now: Date): copy is OfflineItinerary {
  if (!copy || copy.v !== OFFLINE_VERSION) return false;
  if (copy.expires_at && Date.parse(copy.expires_at) <= now.getTime()) return false;
  return true;
}

/** What an online answer of the portal home does to the stored copy. */
export type OnlineAnswer = { status: number; home?: OfflineSource };
export type OfflineAction = { kind: "save"; copy: OfflineItinerary } | { kind: "clear" } | { kind: "keep" };

/**
 * The reconnect rule (server wins; nothing is ever written back from the device):
 *   - a signed-in home -> replace the copy with this one (another person of the group after "Switch" included);
 *   - a signed-out home -> keep the copy only if it is this link's (a session that ran out, card: "cleared at sign-out
 *     and when the link expires"), else clear it (another link, so another traveller or agency, on this device);
 *   - 404 (the link expired, was revoked or re-issued, or the trip closed) -> clear;
 *   - no answer (status 0: offline), 429 or 5xx -> keep (show it when offline).
 */
export function reconcile(stored: OfflineItinerary | null, answer: OnlineAnswer, now: Date): OfflineAction {
  if (answer.status === 404) return { kind: "clear" };
  if (answer.status >= 200 && answer.status < 300 && answer.home) {
    const copy = offlineCopy(answer.home, now);
    if (copy) return { kind: "save", copy };
    const ref = text(answer.home.offline_ref, 64);
    if (!stored) return { kind: "keep" };
    return ref !== null && stored.ref === ref && usable(stored, now) ? { kind: "keep" } : { kind: "clear" };
  }
  return { kind: "keep" };
}

/** Parse a stored entry (anything unreadable is treated as nothing stored). */
export function parseStored(raw: string | null | undefined): OfflineItinerary | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as OfflineItinerary;
    return v && v.v === OFFLINE_VERSION && typeof v.ref === "string" && Array.isArray(v.days) ? v : null;
  } catch {
    return null;
  }
}

// ── Cache Storage (browser only; every call is guarded: no Cache Storage, a private window or a full disk) ──────────

type CacheStorageLike = Pick<CacheStorage, "open" | "delete" | "keys" | "match">;

function storage(): CacheStorageLike | null {
  try {
    return typeof caches !== "undefined" ? caches : null;
  } catch {
    return null;
  }
}

export async function readOffline(cs: CacheStorageLike | null = storage()): Promise<OfflineItinerary | null> {
  if (!cs) return null;
  try {
    // CacheStorage.match with a cache name never creates the cache (open would leave an empty one behind).
    const res = await cs.match(OFFLINE_DATA_KEY, { cacheName: OFFLINE_DATA_CACHE });
    return res ? parseStored(await res.text()) : null;
  } catch {
    return null;
  }
}

export async function writeOffline(copy: OfflineItinerary, cs: CacheStorageLike | null = storage()): Promise<void> {
  if (!cs) return;
  try {
    const cache = await cs.open(OFFLINE_DATA_CACHE);
    await cache.put(
      OFFLINE_DATA_KEY,
      new Response(JSON.stringify(copy), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } }),
    );
  } catch {
    // no space or no storage: the page still works online
  }
}

/** Drop every cache of the portal on this origin (the itinerary copy and the page shell): sign-out, a gone link,
 * another link's answer, or "Remove from this device". */
export async function clearOffline(cs: CacheStorageLike | null = storage()): Promise<void> {
  if (!cs) return;
  try {
    const names = await cs.keys();
    await Promise.all(names.filter((n) => n.startsWith(OFFLINE_CACHE_PREFIX)).map((n) => cs.delete(n)));
  } catch {
    // nothing to clear
  }
}

/** Apply {@link reconcile} to the stored copy. Returns the copy that is stored afterwards. */
export async function syncOffline(answer: OnlineAnswer, now: Date, cs: CacheStorageLike | null = storage()): Promise<OfflineItinerary | null> {
  const stored = await readOffline(cs);
  const action = reconcile(stored, answer, now);
  if (action.kind === "save") {
    await writeOffline(action.copy, cs);
    return action.copy;
  }
  if (action.kind === "clear") {
    await clearOffline(cs);
    return null;
  }
  return stored;
}
