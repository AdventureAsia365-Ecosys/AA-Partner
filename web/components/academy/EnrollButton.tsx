"use client";
// web/components/academy/EnrollButton.tsx — AA-1105: "Start this path" (POST /learn/v1/paths/{id}/enroll, idempotent:
// 201 enrolled, 200 already). On success the catalogue is read again.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { academySend } from "@/lib/academyClient";
import type { AcademyDict } from "@/lib/i18nAcademy";

export function EnrollButton({ pathId, dict }: { pathId: string; dict: AcademyDict }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  async function enroll() {
    setBusy(true);
    setFailed(false);
    const res = await academySend("POST", `paths/${encodeURIComponent(pathId)}/enroll`);
    setBusy(false);
    if (res.ok) router.refresh();
    else if (res.status !== 401) setFailed(true);
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap" }} data-testid={`academy-enroll-${pathId}`}>
      <Button variant="primary" size="sm" onClick={enroll} disabled={busy}>
        {busy ? dict["academy.path.enrolling"] : dict["academy.path.enroll"]}
      </Button>
      {failed && (
        <span role="alert" style={{ fontSize: 12.5, color: K.danger }}>
          {dict["academy.error"]}
        </span>
      )}
    </span>
  );
}
