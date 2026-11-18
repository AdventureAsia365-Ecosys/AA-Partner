"use client";
// web/app/(studio)/portal/_components/AllowanceHeld.tsx
// AA-1126 L1 — a tour version whose job waits because the plan's AI writing allowance for the month is used up (CIS
// job_stop_code ai_quota_exhausted, catalogApi.isAllowanceHeld). The real reason, an "Upgrade plan" link to the plan
// page (lib/apps.ts PLAN_PATH) and a Retry that stays off: the writing resumes on its own when the allowance resets or
// after an upgrade (the CIS hourly tick re-queues it), so a Retry could only stop again.
// Kit: Button (disabled Retry), K tokens; next/link for the page link (a real link, as the kit's LinkButton note says).

import Link from "next/link";
import { useCallback, useState } from "react";
import { X } from "lucide-react";
import { Button, K, useLang, useT } from "@/app/_kit";
import { PLAN_PATH } from "@/lib/apps";
import { COMMON_MESSAGES } from "./common.messages";
import { isAllowanceError, type StudioError } from "./studioError";

/** "1 December 2026" (EN) / "1/12/2026" (VI) from an ISO date; "" when there is none. */
export function resetsOnLabel(iso: string | null | undefined, lang: string): string {
  if (!iso) return "";
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return "";
  return lang === "vi"
    ? `${d.getUTCDate()}/${d.getUTCMonth() + 1}/${d.getUTCFullYear()}`
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
}

/** The row actions of a held version: Upgrade plan + a Retry that is off, with the reason as its title. */
export function AllowanceHeldActions({ resetsAt }: { resetsAt?: string | null }) {
  const t = useT(COMMON_MESSAGES);
  const lang = useLang();
  const note = t("allowanceUsedNote", resetsOnLabel(resetsAt, lang));
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }} data-testid="allowance-held">
      <Link href={PLAN_PATH} style={{ fontSize: 13, fontWeight: 600, color: K.accentDeep }} title={note}>
        {t("upgradePlan")}
      </Link>
      <Button variant="secondary" size="sm" disabled title={note}>
        {t("allowanceRetry")}
      </Button>
    </span>
  );
}

/** The sentence under a held version (the drawer / panel). */
export function AllowanceHeldNote({ resetsAt, testId }: { resetsAt?: string | null; testId?: string }) {
  const t = useT(COMMON_MESSAGES);
  const lang = useLang();
  return (
    <p style={{ margin: 0, fontSize: 13, color: K.ink2 }} role="status" data-testid={testId}>
      {t("allowanceUsedNote", resetsOnLabel(resetsAt, lang))}{" "}
      <Link href={PLAN_PATH} style={{ fontWeight: 600, color: K.accentDeep }}>
        {t("upgradePlan")}
      </Link>
    </p>
  );
}

/** A request refused with 402 ai_quota_exhausted (studioError.allowanceError): the reason and the Upgrade link where
 *  the action was, instead of a "failed" error. `onClose` hides it (a page banner); omitted, it stays (inline). */
export function AllowanceBanner({ resetsAt, onClose, testId = "allowance-banner" }: {
  resetsAt?: string | null; onClose?: () => void; testId?: string;
}) {
  const t = useT(COMMON_MESSAGES);
  return (
    <div data-testid={testId} style={{
      display: "flex", alignItems: "flex-start", gap: 10, padding: "9px 12px", borderRadius: 8,
      background: K.amberSoft, border: `1px solid ${K.line}`,
    }}>
      <div style={{ flex: 1, minWidth: 0 }}><AllowanceHeldNote resetsAt={resetsAt} /></div>
      {onClose && (
        <Button variant="ghost" size="sm" onClick={onClose} aria-label={t("dismiss")} title={t("dismiss")}>
          <X size={12} />
        </Button>
      )}
    </div>
  );
}

/** A page's last request refused for a used-up AI allowance: `take(e)` keeps it and answers true (the caller then
 *  shows no "failed" toast), `clear()` hides the banner. */
export function useAllowanceRefusal() {
  const [refusal, setRefusal] = useState<StudioError | null>(null);
  const take = useCallback((e: unknown): boolean => {
    if (!isAllowanceError(e)) return false;
    setRefusal(e);
    return true;
  }, []);
  const clear = useCallback(() => setRefusal(null), []);
  return { refusal, take, clear };
}

/** The page banner of useAllowanceRefusal (nothing while there is no refusal). */
export function AllowanceRefusalBanner({ refusal, onClose }: { refusal: StudioError | null; onClose: () => void }) {
  if (!refusal) return null;
  return <AllowanceBanner resetsAt={refusal.resetsAt} onClose={onClose} testId="allowance-refused" />;
}
