"use client";
// web/components/support/SupportBanner.tsx — the support banner every member sees (AA-971, card CRM-13b; design v3 §2.3
// "Support banner: amber, cannot be dismissed, [End now]"): amber while the owner's AA support window is open or an AA
// support person works in the agency; red while Adventure Asia holds emergency (break-glass) access. The owner ends
// support access here; an emergency session is not the agency's to end (it closes by itself within the hour).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { formatInstant, type DateLocale } from "@/lib/dates";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { reasonKey, supportErrorKey, type BannerState } from "@/lib/supportAccess";

export function SupportBanner({
  state,
  canManage,
  lang,
  locale,
  timeZone = "UTC",
}: {
  state: NonNullable<BannerState>;
  canManage: boolean;
  lang: Lang;
  locale: DateLocale;
  /** The agency's IANA zone (GET /support-access `timezone`; S262 round 3: the banner printed UTC). */
  timeZone?: string;
}) {
  const router = useRouter();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emergency = state.kind === "breakglass";
  const date = formatInstant(state.until, locale, timeZone);
  const text =
    state.kind === "breakglass"
      ? fmt(tr("support.banner.breakglass"), { date, reason: tr(reasonKey(state.reason)) })
      : fmt(tr(state.kind === "support_session" ? "support.banner.session" : "support.banner.grant"), { date });
  return (
    <div
      role="status"
      data-testid={emergency ? "support-banner-emergency" : "support-banner"}
      style={{
        display: "flex",
        gap: 12,
        alignItems: "center",
        flexWrap: "wrap",
        justifyContent: "space-between",
        background: emergency ? K.dangerSoft : K.amberSoft,
        border: `1px solid ${emergency ? K.danger : K.amber}`,
        borderRadius: 10,
        padding: "10px 14px",
        marginBottom: 16,
        fontSize: 13.5,
        color: K.ink,
      }}
    >
      <span>
        {text}{" "}
        <Link href="/settings/access-log" style={{ fontSize: 12.5 }}>
          {tr("support.accessLog")}
        </Link>
      </span>
      {!emergency && canManage && (
        <span style={{ display: "flex", gap: 8, alignItems: "center" }} data-testid="support-end-now">
          {error && <span style={{ color: K.danger, fontSize: 12.5 }}>{error}</span>}
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            ariaLabel={tr("support.endNow")}
            onClick={async () => {
              setBusy(true);
              setError(null);
              const res = await bffSend("POST", "support-access/revoke");
              setBusy(false);
              if (res.ok) router.refresh();
              else setError(tr(supportErrorKey(res.detail)));
            }}
          >
            {tr("support.endNow")}
          </Button>
        </span>
      )}
    </div>
  );
}
