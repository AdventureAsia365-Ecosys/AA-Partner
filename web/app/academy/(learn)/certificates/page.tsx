// web/app/academy/(learn)/certificates/page.tsx — AA-1105: the learner's path certificates (GET /learn/v1/certificates):
// status, dates, the module versions they pin, and a share link for a valid one (the public verify page /verify/<code>).
import { Badge, EmptyState, K, PageHeader, Section } from "@/app/_kit";
import { CertificateShare } from "@/components/academy/CertificateShare";
import { ReadError } from "@/components/academy/ReadError";
import { certificateTone, type Certificate } from "@/lib/academy";
import { academyContext } from "@/lib/academyContext";
import { academyRead } from "@/lib/academyPage";
import { formatDate } from "@/lib/dates";
import { academyDict, say } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

export default async function AcademyCertificatesPage() {
  const ctx = await academyContext();
  const dict = academyDict(ctx.lang);
  const list = await academyRead<{ certificates: Certificate[] }>(ctx, "certificates");
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 880 }} data-testid="academy-certificates">
      <PageHeader title={dict["academy.certs.title"]} description={dict["academy.certs.subtitle"]} />
      {!list.ok ? (
        <ReadError lang={ctx.lang} kind={list.kind} />
      ) : list.data.certificates.length === 0 ? (
        <EmptyState title={dict["academy.certs.empty"]} />
      ) : (
        list.data.certificates.map((c) => (
          <Section
            key={c.certificate_id}
            title={c.path_title}
            actions={<Badge tone={certificateTone(c.status)}>{dict[`academy.verify.status.${c.status}`]}</Badge>}
            testId={`academy-cert-${c.path_id}`}
          >
            <div style={{ display: "grid", gap: 8 }}>
              <span style={{ fontSize: 13, color: K.body }}>
                {say(dict, "academy.certs.issued", { date: formatDate(c.issued_at, ctx.locale) })} ·{" "}
                {c.expires_at ? say(dict, "academy.certs.expires", { date: formatDate(c.expires_at, ctx.locale) }) : dict["academy.certs.noExpiry"]}
              </span>
              {c.module_versions.length > 0 && (
                <span style={{ fontSize: 12.5, color: K.muted }}>
                  {dict["academy.certs.modules"]}: {c.module_versions.map((m) => `${m.module_id} v${m.version}`).join(", ")}
                </span>
              )}
              {(c.status === "valid" || c.status === "renewal_due") && (
                <CertificateShare certificateId={c.certificate_id} origin={ctx.cfg.publicOrigin} dict={dict} />
              )}
            </div>
          </Section>
        ))
      )}
    </div>
  );
}
