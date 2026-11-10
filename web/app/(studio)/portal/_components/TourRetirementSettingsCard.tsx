"use client";
// web/app/(studio)/portal/_components/TourRetirementSettingsCard.tsx — AA-1039 follow-up (tech lead decision S276)
//
// Account › Settings: "Automatically move posts about a discontinued tour to draft after N days". Off by default:
// when Adventure Asia stops offering a tour, the agency is told (bell + e-mail) and its WordPress pages stay online
// until it acts (one-click "Move to draft" on Publish). Turning this on makes the next retirements move those pages to
// draft after N days (1–30, default 7); turning it off also stops automatic drafts already scheduled.
//
// API: GET / PATCH /api/tenant/v1/account/tour-retirement (CIS api/routers/v1_account_tour_retirement.py). CIS lets
// only the Content studio owner change it (editor, viewer, view-as get 403), so the controls are read-only for the
// others (useStudioRole). The card is not shown when the read fails (503 before the CIS migration, a refused read).
// UI kit: Card + Checkbox + Field + Input + Button; react-query key ["account", "tour-retirement"].

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Checkbox, Field, Input, K, sans, useLang, useT } from "@/app/_kit";
import { Card } from "@/app/_kit/local/studio/StudioCard";
import { useStudioRole } from "@/app/_kit/local/studio/useShellData";
import { RETIRED_PAGES_MESSAGES } from "./retiredPages.messages";
import {
  TOUR_RETIREMENT_SETTINGS_KEY, getTourRetirementSettings, parseDays, saveTourRetirementSettings,
  type TourRetirementSettings,
} from "./retiredPagesApi";
import { plainError } from "./studioError";

export function TourRetirementSettingsForm({ settings, isOwner }: { settings: TourRetirementSettings; isOwner: boolean }) {
  const t = useT(RETIRED_PAGES_MESSAGES);
  const lang = useLang();
  const qc = useQueryClient();
  const [auto, setAuto] = useState(settings.auto_draft);
  const [days, setDays] = useState(String(settings.notice_days));
  const [note, setNote] = useState<{ text: string; ok: boolean } | null>(null);

  const parsed = parseDays(days, settings.min_days, settings.max_days);
  const daysError = auto && parsed === null ? t("daysInvalid", settings.min_days, settings.max_days) : null;
  const changed = auto !== settings.auto_draft || (auto && parsed !== null && parsed !== settings.notice_days);

  const save = useMutation({
    mutationFn: () => saveTourRetirementSettings(auto && parsed !== null
      ? { auto_draft: true, notice_days: parsed } : { auto_draft: auto }),
    onSuccess: (next) => {
      // the form keeps what was typed (it is what was saved); the card's query gets the stored values
      setAuto(next.auto_draft);
      setDays(String(next.notice_days));
      qc.setQueryData(TOUR_RETIREMENT_SETTINGS_KEY, next);
      setNote({ text: t("saved"), ok: true });
    },
    onError: (e) => setNote({ text: plainError(e, t("saveFailed"), lang), ok: false }),
  });

  return (
    <div data-testid="tour-retirement-settings" style={{ display: "flex", flexDirection: "column", gap: 12, fontFamily: sans }}>
      <Checkbox label={t("autoDraftLabel")} checked={auto} disabled={!isOwner || save.isPending}
        onChange={(e) => { setAuto(e.currentTarget.checked); setNote(null); }} data-testid="tour-retirement-auto" />
      <div style={{ fontSize: 11.5, color: K.muted2, marginTop: -6 }}>{t("autoDraftHint")}</div>
      {auto && (
        <Field label={t("daysLabel")} hint={t("daysHint", settings.min_days, settings.max_days)} error={daysError}
          locked={!isOwner} style={{ maxWidth: 220 }}>
          <Input type="number" inputMode="numeric" min={settings.min_days} max={settings.max_days} step={1} value={days}
            disabled={!isOwner || save.isPending} onChange={(e) => { setDays(e.currentTarget.value); setNote(null); }}
            data-testid="tour-retirement-days" />
        </Field>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        {isOwner ? (
          <Button size="sm" variant="primary" disabled={!changed || !!daysError || save.isPending}
            onClick={() => save.mutate()} testId="tour-retirement-save">
            {save.isPending ? t("saving") : t("save")}
          </Button>
        ) : (
          <span style={{ fontSize: 12, color: K.muted }}>{t("ownerOnly")}</span>
        )}
        <span data-testid="tour-retirement-state" style={{ fontSize: 12, color: K.muted }}>
          {settings.auto_draft ? t("stateOn", settings.notice_days) : t("stateOff")}
        </span>
      </div>
      {note && (
        <span role={note.ok ? "status" : "alert"} data-testid="tour-retirement-note"
          style={{ fontSize: 12.5, color: note.ok ? K.success : K.danger }}>{note.text}</span>
      )}
    </div>
  );
}

export function TourRetirementSettingsCard() {
  const t = useT(RETIRED_PAGES_MESSAGES);
  const role = useStudioRole();
  const { data, isError } = useQuery({
    queryKey: TOUR_RETIREMENT_SETTINGS_KEY, queryFn: getTourRetirementSettings, retry: false,
  });
  if (isError || !data) return null;
  return (
    <Card>
      <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted, marginBottom: 10 }}>
        {t("settingsHeading")}
      </div>
      <TourRetirementSettingsForm settings={data} isOwner={role === "owner"} />
    </Card>
  );
}
