"use client";
// web/components/team/RecoveryBanner.tsx — the owner-recovery banner every member sees (AA-939, Q1 6.3: "every member
// sees a banner with [Cancel]"). AA opened a recovery (the owner left or cannot be reached); on the date shown an
// existing member becomes owner unless someone in the agency cancels it here.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { formatDate, type DateLocale } from "@/lib/dates";
import { type Lang, t } from "@/lib/i18n";
import type { Recovery } from "@/lib/team";

export function RecoveryBanner({ recovery, lang, locale }: { recovery: Recovery; lang: Lang; locale: DateLocale }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const when = recovery.eligible_at ? formatDate(recovery.eligible_at, locale, { year: true }) : "";
  return (
    <div
      role="status"
      data-testid="recovery-banner"
      style={{
        display: "flex",
        gap: 12,
        alignItems: "center",
        flexWrap: "wrap",
        justifyContent: "space-between",
        background: K.amberSoft,
        border: `1px solid ${K.amber}`,
        borderRadius: 10,
        padding: "10px 14px",
        marginBottom: 16,
        fontSize: 13.5,
        color: K.ink,
      }}
    >
      <span>
        {t(lang, "recovery.banner").replace("{date}", when)}
        {recovery.is_candidate ? ` ${t(lang, "recovery.youAreCandidate")}` : ""}
      </span>
      <span style={{ display: "flex", gap: 8, alignItems: "center" }} data-testid="recovery-cancel">
        {error && <span style={{ color: K.danger, fontSize: 12.5 }}>{t(lang, "trip.error")}</span>}
        <Button
          size="sm"
          variant="secondary"
          disabled={busy}
          ariaLabel={t(lang, "recovery.cancel")}
          onClick={async () => {
            setBusy(true);
            setError(false);
            const res = await bffSend("POST", `recovery/${recovery.request_id}/cancel`);
            setBusy(false);
            if (res.ok) router.refresh();
            else setError(true);
          }}
        >
          {t(lang, "recovery.cancel")}
        </Button>
      </span>
    </div>
  );
}
