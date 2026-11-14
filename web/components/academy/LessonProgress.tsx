"use client";
// web/components/academy/LessonProgress.tsx — AA-1105: the lesson's learning events (POST /learn/v1/progress, idempotent
// on the client-made statement_id). Opening the page records `opened` once (only when the lesson was not started:
// a re-read changes nothing), and "Mark this lesson done" records `completed`; the answer says when the module's
// lessons are all done (then the quiz is next).
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button, K } from "@/app/_kit";
import type { LearnLang, LessonStatus } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import type { AcademyDict } from "@/lib/i18nAcademy";

interface Accepted {
  statement_id: string;
  lesson_id: string;
  status: "opened" | "completed";
  module_complete: boolean;
}

function statementId(): string {
  return crypto.randomUUID();
}

export function LessonProgress({
  lessonId,
  lang,
  status,
  moduleId,
  dict,
}: {
  lessonId: string;
  lang: LearnLang;
  status: LessonStatus;
  moduleId: string;
  dict: AcademyDict;
}) {
  const router = useRouter();
  const sent = useRef(false);
  const [state, setState] = useState<LessonStatus>(status);
  const [busy, setBusy] = useState(false);
  const [moduleDone, setModuleDone] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (sent.current || status !== "not_started") return;
    sent.current = true;
    void academySend<Accepted>("POST", "progress", { body: { statement_id: statementId(), verb: "opened", lesson_id: lessonId, lang } }).then((res) => {
      if (res.ok) setState((s) => (s === "not_started" ? "opened" : s));
    });
  }, [lessonId, lang, status]);

  async function complete() {
    setBusy(true);
    setFailed(false);
    const res = await academySend<Accepted>("POST", "progress", { body: { statement_id: statementId(), verb: "completed", lesson_id: lessonId, lang } });
    setBusy(false);
    if (res.ok) {
      setState("completed");
      setModuleDone(res.data.module_complete);
      router.refresh();
    } else if (res.status !== 401) setFailed(true);
  }

  return (
    <div style={{ display: "grid", gap: 8, justifyItems: "start" }} data-testid="academy-lesson-progress">
      {state === "completed" ? (
        <span role="status" style={{ fontSize: 13.5, color: K.success, fontWeight: 600 }} data-testid="academy-lesson-done">
          {moduleDone ? dict["academy.lesson.moduleDone"] : dict["academy.lesson.done"]}
        </span>
      ) : (
        <span data-testid="academy-lesson-complete">
          <Button variant="primary" onClick={complete} disabled={busy}>
            {busy ? dict["academy.lesson.completing"] : dict["academy.lesson.complete"]}
          </Button>
        </span>
      )}
      {moduleDone && (
        <a href={`/academy/quiz/module/${encodeURIComponent(moduleId)}`} style={{ fontSize: 13.5, color: K.accentDeep, fontWeight: 600 }}>
          {dict["academy.quiz.start"]}
        </a>
      )}
      {failed && (
        <span role="alert" style={{ fontSize: 12.5, color: K.danger }}>
          {dict["academy.error"]}
        </span>
      )}
    </div>
  );
}
