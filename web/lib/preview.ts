// web/lib/preview.ts
// AA-970 (design v3 card CRM-37b, R42 "What's new + preview switch") — the agency app's preview features. An agency is
// "on preview" when its own value of the settings key crm.release.preview is on (crm-077, off by default; an AA
// administrator switches one agency on at CIS Platform > Settings, CRM tab). The API says so in /me (`preview`); the
// web shows a preview feature only when previewOn() is true, so an agency on preview sees the flagged UI and every
// other agency does not. The API refuses nothing on this flag: it is a UI channel, not a permission.
//
// R (card): privacy or price logic never sits behind a preview flag. A change to who sees personal data, to retention
// or erasure, to consent, or to a price, a payment, a margin or a commission ships to every agency at once.
// api/scripts/preview_flag_lint.py fails CI when (1) a flag below names a forbidden area, (2) a file that calls
// previewOn() lives in a privacy or money area of the app, or (3) such a file imports a privacy or money module.
// Keep the gated part in a component file of its own that holds no such logic.
//
// To add a preview feature: one entry here (area, the date it joined the preview, its label in both languages), gate
// its UI with previewOn(ctx.me, "<flag>"), and add a What's new entry with `preview: "<flag>"` (lib/whatsNew.ts). When it
// ships to everyone, remove the entry and the previewOn() call, and move the What's new entry to the released list.

import type { Lang } from "./i18n";

/** The parts of the app a preview may touch. Privacy, data rules, prices, payments and money are not on the list. */
export type PreviewArea =
  | "help"
  | "home"
  | "pipeline"
  | "clients-list"
  | "inbox"
  | "tasks"
  | "calendar"
  | "travelling"
  | "departures"
  | "reports"
  | "library"
  | "automations"
  | "suppliers"
  | "services"
  | "studio";

export interface PreviewFeature {
  area: PreviewArea;
  /** The day the feature joined the preview (YYYY-MM-DD). */
  since: string;
  label: Record<Lang, string>;
}

/** Every preview feature of the agency app (lint: api/scripts/preview_flag_lint.py reads the `area:` of each). */
export const PREVIEW_FEATURES = {
  "help.preview-panel": {
    area: "help",
    since: "2026-11-16",
    label: {
      en: "This panel: the list of features your agency can try before every agency gets them",
      vi: "Bảng này: danh sách tính năng đại lý của bạn được dùng thử trước khi mọi đại lý cùng có",
    },
  },
} as const satisfies Record<string, PreviewFeature>;

export type PreviewFlag = keyof typeof PREVIEW_FEATURES;

export function isPreviewFlag(value: unknown): value is PreviewFlag {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(PREVIEW_FEATURES, value);
}

/** Whether this agency sees the preview feature `flag`: only when /me says the agency is on preview. */
export function previewOn(me: { preview?: boolean } | null | undefined, flag: PreviewFlag): boolean {
  return me?.preview === true && isPreviewFlag(flag);
}

/** The preview features an agency on preview can try, oldest first (empty for every other agency). */
export function previewFeatures(me: { preview?: boolean } | null | undefined): { flag: PreviewFlag; feature: PreviewFeature }[] {
  if (me?.preview !== true) return [];
  return (Object.keys(PREVIEW_FEATURES) as PreviewFlag[])
    .map((flag) => ({ flag, feature: PREVIEW_FEATURES[flag] as PreviewFeature }))
    .sort((a, b) => a.feature.since.localeCompare(b.feature.since));
}
