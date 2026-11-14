// web/app/verify/[code]/page.tsx — the PUBLIC AA Academy certificate verify page (AA-1105, root ADR 0022 decision 11),
// academy host only (proxy.ts), no sign-in: the holder's display name, the path, the dates, the status and the pinned
// module versions from CIS GET /learn/public/v1/certificates/{code}. The web limits its own visitors before it calls
// CIS (lib/academyVerify.ts: behind the web every visitor reaches CIS from one address). No Referer leaves this page
// (proxy.ts), and it is not indexed.
import type { Metadata } from "next";
import { Badge, K } from "@/app/_kit";
import { CenteredCard } from "@/components/CenteredCard";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { certificateTone } from "@/lib/academy";
import { academyContext } from "@/lib/academyContext";
import { verifyCertificate } from "@/lib/academyVerify";
import { formatDate } from "@/lib/dates";
import { t } from "@/lib/i18n";
import { academyDict, say } from "@/lib/i18nAcademy";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "AA Academy certificate", robots: { index: false, follow: false }, referrer: "no-referrer" };

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <span style={{ fontSize: 11.5, color: K.muted, textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      <span style={{ fontSize: 14, color: K.ink, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

export default async function VerifyCertificatePage({ params }: { params: Promise<{ code: string }> }) {
  const { code } = await params;
  const ctx = await academyContext();
  const dict = academyDict(ctx.lang);
  const outcome = await verifyCertificate(ctx.cfg.cisApiUrl, code, ctx.viewerAddress);
  return (
    <CenteredCard testId="academy-verify" brand={dict["academy.title"]}>
      <div style={{ display: "grid", gap: 14 }}>
        <h1 style={{ margin: 0, fontSize: 18, color: K.ink }}>{dict["academy.verify.title"]}</h1>
        {outcome.kind === "ok" ? (
          <div style={{ display: "grid", gap: 12 }} data-testid="academy-verify-result">
            <Row label={dict["academy.verify.status"]}>
              <Badge tone={certificateTone(outcome.certificate.status)}>{dict[`academy.verify.status.${outcome.certificate.status}`]}</Badge>
            </Row>
            <Row label={dict["academy.verify.holder"]}>{outcome.certificate.holder_display_name ?? dict["academy.verify.noName"]}</Row>
            <Row label={dict["academy.verify.path"]}>{outcome.certificate.path_title}</Row>
            <Row label={dict["academy.verify.issued"]}>{formatDate(outcome.certificate.issued_at, ctx.locale)}</Row>
            <Row label={dict["academy.verify.expires"]}>
              {outcome.certificate.expires_at ? formatDate(outcome.certificate.expires_at, ctx.locale) : dict["academy.certs.noExpiry"]}
            </Row>
            {outcome.certificate.module_versions.length > 0 && (
              <Row label={dict["academy.verify.modules"]}>
                {outcome.certificate.module_versions.map((m) => `${m.module_id} v${m.version}`).join(", ")}
              </Row>
            )}
          </div>
        ) : (
          <p role="status" style={{ margin: 0, fontSize: 13.5, color: K.muted }} data-testid="academy-verify-message">
            {outcome.kind === "not_found"
              ? dict["academy.verify.notFound"]
              : outcome.kind === "rate_limited"
                ? say(dict, "academy.verify.rateLimited", { n: outcome.retryAfterS })
                : dict["academy.verify.unavailable"]}
          </p>
        )}
        <LocaleSwitch locale={ctx.locale} label={t(ctx.lang, "shell.language")} example={t(ctx.lang, "shell.dateExample")} next={`/verify/${encodeURIComponent(code)}`} />
      </div>
    </CenteredCard>
  );
}
