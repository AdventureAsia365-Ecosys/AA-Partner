// web/lib/academyCaptions.ts
// AA-1105 — lesson captions served from the academy origin. The learning API returns presigned S3 GET URLs for the
// video and its .vtt captions (GET /learn/v1/lessons/{id}/video, lifetime = setting cis.learn.video_url_ttl_seconds).
// The video plays straight from S3 (a <video src> needs no CORS), but a <track> is fetched with CORS, and the bronze
// bucket allows no academy origin (no bucket CORS change, S280 review). So this server fetches the caption file
// (a server fetch needs no CORS) and the page's <track> points at /academy/captions/<lesson>/<lang>.
//
// The caption URL comes from CIS; it is still checked before this server fetches it (https, an S3 host), and the
// answer must be a WebVTT file of bounded size. Server-only.

import type { LearnLang, VideoUrls } from "./academy";

const CAPTION_TIMEOUT_MS = 10_000; // technical constant: one S3 GET
export const MAX_CAPTION_BYTES = 512 * 1024; // technical constant: a caption file of a short lesson is a few KiB

/** Whether a caption URL may be fetched by this server: https on an Amazon S3 host, no credentials in the URL. */
export function captionUrlAllowed(raw: string): boolean {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port) return false;
  const host = url.hostname.toLowerCase();
  return host.endsWith(".amazonaws.com") && /(^|\.)s3[.-]/.test(host);
}

/** The caption URL of one language in a video answer, or null. */
export function captionUrl(video: Pick<VideoUrls, "captions">, lang: LearnLang): string | null {
  return video.captions.find((c) => c.lang === lang)?.url ?? null;
}

export type CaptionFetch = { ok: true; text: string } | { ok: false; reason: "refused_url" | "upstream" | "too_large" | "not_vtt" };

/** Fetch one caption file (checked URL, size cap, WebVTT signature). */
export async function fetchCaption(raw: string, fetchImpl: typeof fetch = fetch): Promise<CaptionFetch> {
  if (!captionUrlAllowed(raw)) return { ok: false, reason: "refused_url" };
  let res: Response;
  try {
    res = await fetchImpl(raw, { method: "GET", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(CAPTION_TIMEOUT_MS) });
  } catch {
    return { ok: false, reason: "upstream" };
  }
  if (!res.ok) return { ok: false, reason: "upstream" };
  const declared = Number(res.headers.get("content-length") ?? "0");
  if (declared > MAX_CAPTION_BYTES) return { ok: false, reason: "too_large" };
  const buf = await res.arrayBuffer().catch(() => null);
  if (!buf) return { ok: false, reason: "upstream" };
  if (buf.byteLength > MAX_CAPTION_BYTES) return { ok: false, reason: "too_large" };
  const text = new TextDecoder("utf-8").decode(buf).replace(/^﻿/, "");
  if (!/^WEBVTT(?:[ \t]|\r?\n|$)/.test(text)) return { ok: false, reason: "not_vtt" };
  return { ok: true, text };
}
