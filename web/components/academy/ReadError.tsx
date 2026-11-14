// web/components/academy/ReadError.tsx — AA-1105: what an academy page shows when its read of the learning API was
// refused or failed (the kit ErrorState, the reason in plain words). Server component.
import { ErrorState } from "@/app/_kit";
import type { AcademyErrorKind } from "@/lib/academy";
import { refusalKey } from "@/lib/academyPage";
import { t, type Lang } from "@/lib/i18n";

export function ReadError({ lang, kind }: { lang: Lang; kind: AcademyErrorKind | "refused" }) {
  return (
    <div data-testid="academy-read-error">
      <ErrorState message={t(lang, refusalKey(kind))} />
    </div>
  );
}
