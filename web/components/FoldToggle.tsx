"use client";
// web/components/FoldToggle.tsx — AA-1129 (S283 audit P4 row 14): the "Show all n" / "Show fewer" button under a folded
// list (lib/fold.ts). Kit Button only.
import { Button } from "@/app/_kit";
import { foldToggle, FOLD_FIRST } from "@/lib/fold";
import { fmt, t, type Lang } from "@/lib/i18n";

export function FoldToggle({
  total,
  open,
  onToggle,
  lang,
  first = FOLD_FIRST,
  testId,
}: {
  total: number;
  open: boolean;
  onToggle: () => void;
  lang: Lang;
  first?: number;
  testId?: string;
}) {
  const kind = foldToggle(total, open, first);
  if (!kind) return null;
  return (
    <Button size="sm" variant="ghost" onClick={onToggle} aria-expanded={open} testId={testId}>
      {kind === "all" ? fmt(t(lang, "list.showAll"), { n: total }) : t(lang, "list.showFewer")}
    </Button>
  );
}
