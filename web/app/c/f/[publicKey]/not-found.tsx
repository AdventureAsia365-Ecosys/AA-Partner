// web/app/c/f/[publicKey]/not-found.tsx — AA-1129 (P5, S283 audit): an unknown or malformed form key answers HTTP 404
// (was a 200 card, so monitors could not tell a dead form link from a live one). Same card as before, no brand mark
// (the client host is white-label and the agency is unknown here).
import { CenteredCard } from "@/components/CenteredCard";
import { K } from "@/app/_kit";
import { t } from "@/lib/i18n";
import { serverContext } from "@/lib/serverContext";

export default async function FormNotFound() {
  const { lang } = await serverContext();
  return (
    <CenteredCard testId="intake-unavailable" brand={null}>
      <p style={{ margin: 0, fontSize: 14, color: K.body }}>{t(lang, "intake.notFound")}</p>
    </CenteredCard>
  );
}
