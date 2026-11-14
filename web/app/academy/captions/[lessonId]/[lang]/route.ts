// GET /academy/captions/<lesson>/<lang> — a lesson's WebVTT captions from the academy origin (AA-1105; S280 review
// "captions over CORS"). The page's <track> points here; this server asks the learning API for fresh presigned URLs
// (GET /learn/v1/lessons/{id}/video), fetches the caption file of the language server side (lib/academyCaptions.ts:
// checked URL, size cap, WebVTT signature) and answers it as text/vtt. A <track> sends no custom header, so this
// handler only refuses a request another site's page sent (Sec-Fetch-Site), then needs the academy session (401).
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import type { VideoUrls } from "@/lib/academy";
import { learnCall } from "@/lib/academyApi";
import { LESSON_ID } from "@/lib/academyBff";
import { captionUrl, fetchCaption } from "@/lib/academyCaptions";
import { academyAccessValid, readAcademySession } from "@/lib/academySession";
import { requestConfig } from "@/lib/config";
import { academyHostAllowed } from "@/lib/hosts";
import { json } from "@/lib/respond";

export const dynamic = "force-dynamic";

const LESSON_RE = new RegExp(`^${LESSON_ID}$`);

export async function GET(request: NextRequest, ctx: { params: Promise<{ lessonId: string; lang: string }> }) {
  const cfg = requestConfig(request);
  if (!academyHostAllowed(request.headers.get("host"), cfg)) return json({ detail: "not_found" }, 404);
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return json({ detail: "cross_site_refused" }, 403);
  const { lessonId, lang } = await ctx.params;
  if (!LESSON_RE.test(lessonId) || (lang !== "en" && lang !== "vi")) return json({ detail: "not_found" }, 404);
  const session = readAcademySession(cfg, request.cookies);
  if (!academyAccessValid(session)) return json({ detail: "session_expired" }, 401);

  const video = await learnCall<VideoUrls>(cfg.cisApiUrl, session.at, "GET", `lessons/${lessonId}/video`);
  if (!video.ok) return json({ detail: video.kind }, video.status === 401 ? 401 : video.status === 404 ? 404 : 502);
  const url = captionUrl(video.data, lang);
  if (!url) return json({ detail: "not_found" }, 404);
  const caption = await fetchCaption(url);
  if (!caption.ok) {
    console.warn(JSON.stringify({ event: "crm_web_academy_caption_failed", reason: caption.reason }));
    return json({ detail: "captions_unavailable" }, 502);
  }
  return new NextResponse(caption.text, {
    status: 200,
    headers: {
      "Content-Type": "text/vtt; charset=utf-8",
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
