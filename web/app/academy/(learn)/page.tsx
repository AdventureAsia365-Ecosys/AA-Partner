// web/app/academy/(learn)/page.tsx — the AA Academy catalogue (AA-1105): the learning paths of the learner's tier with
// enrollment, progress and certificate state (GET /learn/v1/paths), each path's modules in order (required or
// elective, draft and planned marked), "Start this path" and, once every required module is complete, the final quiz.
import Link from "next/link";
import { Badge, EmptyState, K, PageHeader, Section } from "@/app/_kit";
import { EnrollButton } from "@/components/academy/EnrollButton";
import { ReadError } from "@/components/academy/ReadError";
import { certificateTone, pathProgress, type PathList, type PathModule, type PathSummary } from "@/lib/academy";
import { academyContext } from "@/lib/academyContext";
import { academyRead } from "@/lib/academyPage";
import { academyDict, say, type AcademyDict } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

function ModuleRow({ m, dict }: { m: PathModule; dict: AcademyDict }) {
  const planned = m.status === "planned";
  const label = m.title ? `${m.id} · ${m.title}` : m.id;
  return (
    <li
      style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "10px 0", borderTop: `1px solid ${K.line}` }}
      data-testid={`academy-path-module-${m.id}`}
    >
      {planned ? (
        <span style={{ fontSize: 13.5, color: K.muted }}>{label}</span>
      ) : (
        <Link href={`/academy/modules/${encodeURIComponent(m.id)}`} style={{ fontSize: 13.5, color: K.accentDeep, fontWeight: 600, textDecoration: "none" }}>
          {label}
        </Link>
      )}
      <span style={{ flex: 1 }} />
      {!m.required && <Badge dot={false}>{dict["academy.path.elective"]}</Badge>}
      {m.status === "draft" && <Badge tone="warning">{dict["academy.draft"]}</Badge>}
      {planned ? (
        <Badge dot={false}>{dict["academy.module.planned"]}</Badge>
      ) : m.complete ? (
        <Badge tone="success">{dict["academy.module.complete"]}</Badge>
      ) : (
        <Badge tone="accent">{dict["academy.module.open"]}</Badge>
      )}
    </li>
  );
}

function PathCard({ path, dict }: { path: PathSummary; dict: AcademyDict }) {
  const progress = pathProgress(path);
  const ready = progress.total > 0 && progress.done === progress.total;
  const certKey = path.certificate_status ? (`academy.cert.${path.certificate_status}` as const) : null;
  const badges = (
    <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
      {path.completed ? (
        <Badge tone="success">{dict["academy.path.completed"]}</Badge>
      ) : path.enrolled ? (
        <Badge tone="info">{dict["academy.path.enrolled"]}</Badge>
      ) : null}
      {certKey && <Badge tone={certificateTone(path.certificate_status)}>{dict[certKey]}</Badge>}
    </span>
  );
  return (
    <Section title={path.title} description={say(dict, "academy.path.progress", progress)} actions={badges} testId={`academy-path-${path.id}`}>
      <div style={{ display: "grid", gap: 12 }}>
        {!path.enrolled && (
          <div>
            <EnrollButton pathId={path.id} dict={dict} />
          </div>
        )}
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {path.modules.map((m) => (
            <ModuleRow key={m.id} m={m} dict={dict} />
          ))}
        </ul>
        {path.planned && path.planned.length > 0 && (
          <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{say(dict, "academy.path.planned", { list: path.planned.join(", ") })}</p>
        )}
        {path.final_quiz &&
          (ready ? (
            <Link
              href={`/academy/quiz/final/${encodeURIComponent(path.id)}`}
              style={{ fontSize: 13.5, fontWeight: 600, color: K.accentDeep }}
              data-testid={`academy-final-quiz-${path.id}`}
            >
              {dict["academy.path.finalQuiz"]}
            </Link>
          ) : (
            <p style={{ margin: 0, fontSize: 12.5, color: K.muted }}>{dict["academy.path.finalQuizLocked"]}</p>
          ))}
      </div>
    </Section>
  );
}

export default async function AcademyCataloguePage() {
  const ctx = await academyContext();
  const dict = academyDict(ctx.lang);
  const list = await academyRead<PathList>(ctx, "paths");
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="academy-catalogue">
      <PageHeader title={dict["academy.catalogue.title"]} description={dict["academy.catalogue.subtitle"]} />
      {!list.ok ? (
        <ReadError lang={ctx.lang} kind={list.kind} />
      ) : list.data.paths.length === 0 ? (
        <EmptyState title={dict["academy.catalogue.empty"]} />
      ) : (
        list.data.paths.map((p) => <PathCard key={p.id} path={p} dict={dict} />)
      )}
    </div>
  );
}
