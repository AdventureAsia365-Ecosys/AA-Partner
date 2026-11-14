"use client";
// web/components/academy/LabCheck.tsx — AA-1105: "Check my lab" (POST /learn/v1/modules/{id}/lab/check). Until the lab
// provisioner exists the API answers 409 lab_closed, shown as "Labs open later".
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import type { LabResult } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import type { AcademyDict } from "@/lib/i18nAcademy";

export function LabCheck({ moduleId, dict }: { moduleId: string; dict: AcademyDict }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  async function check() {
    setBusy(true);
    setNote(null);
    const res = await academySend<LabResult>("POST", `modules/${encodeURIComponent(moduleId)}/lab/check`);
    setBusy(false);
    if (res.ok) {
      setNote(res.data.status === "passed" ? dict["academy.lab.passed"] : dict["academy.lab.failed"]);
      router.refresh();
    } else if (res.problem.error === "lab_closed") setNote(dict["academy.lab.closed"]);
    else if (res.status !== 401) setNote(dict["academy.error"]);
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 10, flexWrap: "wrap" }} data-testid="academy-lab-check">
      <Button size="sm" onClick={check} disabled={busy}>
        {dict["academy.lab.check"]}
      </Button>
      {note && (
        <span role="status" style={{ fontSize: 12.5, color: K.muted }}>
          {note}
        </span>
      )}
    </span>
  );
}
