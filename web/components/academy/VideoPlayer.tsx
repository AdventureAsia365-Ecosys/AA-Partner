"use client";
// web/components/academy/VideoPlayer.tsx — AA-1105 (decision D8): the lesson video. Short-lived presigned URLs are asked
// for when the player mounts (GET /learn/v1/lessons/{id}/video; lifetime = setting cis.learn.video_url_ttl_seconds),
// the browser plays the file straight from S3 (range requests for seeking), and the captions come from the academy
// origin (/academy/captions/<lesson>/<lang>, fetched server side: no bucket CORS).
import { useEffect, useState } from "react";
import { K } from "@/app/_kit";
import type { LearnLang, VideoUrls } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import type { AcademyDict } from "@/lib/i18nAcademy";

const LANG_LABEL: Record<LearnLang, string> = { en: "English", vi: "Tiếng Việt" };

export function VideoPlayer({ lessonId, lang, dict }: { lessonId: string; lang: LearnLang; dict: AcademyDict }) {
  const [video, setVideo] = useState<VideoUrls | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    void academySend<VideoUrls>("GET", `lessons/${encodeURIComponent(lessonId)}/video`).then((res) => {
      if (!live) return;
      if (res.ok) setVideo(res.data);
      else if (res.status !== 401) setFailed(true);
    });
    return () => {
      live = false;
    };
  }, [lessonId]);
  if (failed) {
    return (
      <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>
        {dict["academy.lesson.videoError"]}
      </p>
    );
  }
  if (!video) return <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{dict["academy.lesson.videoLoading"]}</p>;
  return (
    <video
      controls
      preload="metadata"
      src={video.url}
      onError={() => setFailed(true)}
      style={{ width: "100%", maxWidth: 880, borderRadius: 10, background: K.ink }}
      data-testid="academy-video"
    >
      {video.captions.map((c) => (
        <track
          key={c.lang}
          kind="captions"
          srcLang={c.lang}
          label={LANG_LABEL[c.lang]}
          src={`/academy/captions/${encodeURIComponent(lessonId)}/${c.lang}`}
          default={c.lang === lang}
        />
      ))}
    </video>
  );
}
