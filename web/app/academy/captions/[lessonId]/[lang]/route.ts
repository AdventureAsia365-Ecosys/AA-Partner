// GET /academy/captions/<lesson>/<lang> — a lesson's WebVTT captions from the academy origin (AA-1105; S280 review
// "captions over CORS"). The page's <track> points here; this server asks the learning API for fresh presigned URLs
// (GET /learn/v1/lessons/{id}/video), fetches the caption file of the language server side (lib/academyCaptions.ts:
// checked URL, size cap, WebVTT signature) and answers it as text/vtt. A <track> sends no custom header, so this
// handler only refuses a request another site's page sent (Sec-Fetch-Site), then needs the academy session (401).
import { NextResponse, type NextRequest } from "next/server";
import type { LearnLang, VideoUrls } from "@/lib/academy";
import { learnCall } from "@/lib/academyApi";
import { LESSON_ID } from "@/lib/academyBff";
import { captionUrl, fetchCaption } from "@/lib/academyCaptions";
import { academyAccessValid, readAcademySession } from "@/lib/academySession";
import { requestConfig, type WebConfig } from "@/lib/config";
import { academyHostAllowed } from "@/lib/hosts";
import { json } from "@/lib/respond";

export const dynamic = "force-dynamic";

const LESSON_RE = new RegExp(`^${LESSON_ID}$`);

/** A refusal before any upstream call (host, other site, shape), or null. */
function refusal(request: NextRequest, cfg: WebConfig, lessonId: string, lang: string): NextResponse | null {
  if (!academyHostAllowed(request.headers.get("host"), cfg)) return json({ detail: "not_found" }, 404);
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return json({ detail: "cross_site_refused" }, 403);
  return LESSON_RE.test(lessonId) && (lang === "en" || lang === "vi") ? null : json({ detail: "not_found" }, 404);
}

function vtt(text: string): NextResponse {
  return new NextResponse(text, {
    status: 200,
    headers: { "Content-Type": "text/vtt; charset=utf-8", "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" },
  });
}

/** The caption file of one language of a lesson, through fresh presigned URLs from the learning API. */
async function caption(cfg: WebConfig, token: string, lessonId: string, lang: LearnLang): Promise<NextResponse> {
  const video = await learnCall<VideoUrls>(cfg.cisApiUrl, token, "GET", `lessons/${lessonId}/video`);
  if (!video.ok) return json({ detail: video.kind }, video.status === 401 || video.status === 404 ? video.status : 502);
  const url = captionUrl(video.data, lang);
  if (!url) return json({ detail: "not_found" }, 404);
  const fetched = await fetchCaption(url);
  if (fetched.ok) return vtt(fetched.text);
  console.warn(JSON.stringify({ event: "crm_web_academy_caption_failed", reason: fetched.reason }));
  return json({ detail: "captions_unavailable" }, 502);
}

export async function GET(request: NextRequest, ctx: { params: Promise<{ lessonId: string; lang: string }> }) {
  const cfg = requestConfig(request);
  const { lessonId, lang } = await ctx.params;
  const refused = refusal(request, cfg, lessonId, lang);
  if (refused) return refused;
  const session = readAcademySession(cfg, request.cookies);
  if (!academyAccessValid(session)) return json({ detail: "session_expired" }, 401);
  return caption(cfg, session.at, lessonId, lang as LearnLang);
}
