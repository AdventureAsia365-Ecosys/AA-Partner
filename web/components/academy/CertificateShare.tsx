"use client";
// web/components/academy/CertificateShare.tsx — AA-1105: "Create a share link" for the learner's own valid certificate
// (POST /learn/v1/certificates/{id}/code). CIS keeps only the code's hash, so the link is shown once; a new link
// replaces the previous one.
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { verifyUrl, type CertificateCode } from "@/lib/academy";
import { academySend } from "@/lib/academyClient";
import type { AcademyDict } from "@/lib/i18nAcademy";

export function CertificateShare({ certificateId, origin, dict }: { certificateId: string; origin: string; dict: AcademyDict }) {
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  async function share() {
    setBusy(true);
    setNote(null);
    const res = await academySend<CertificateCode>("POST", `certificates/${encodeURIComponent(certificateId)}/code`);
    setBusy(false);
    if (res.ok) {
      const url = verifyUrl(origin, res.data.verify_path);
      if (url) setLink(url);
      else setNote(dict["academy.error"]);
    } else if (res.status === 409) setNote(dict["academy.certs.notValid"]);
    else if (res.status !== 401) setNote(dict["academy.error"]);
  }
  return (
    <div style={{ display: "grid", gap: 6, justifyItems: "start" }} data-testid={`academy-cert-share-${certificateId}`}>
      <Button size="sm" onClick={share} disabled={busy}>
        {busy ? dict["academy.certs.sharing"] : dict["academy.certs.share"]}
      </Button>
      <span style={{ fontSize: 12, color: K.muted }}>{dict["academy.certs.shareHelp"]}</span>
      {link && (
        <div style={{ display: "grid", gap: 4 }}>
          <span style={{ fontSize: 13, color: K.body }}>{dict["academy.certs.link"]}</span>
          <a href={link} target="_blank" rel="noopener noreferrer" style={{ fontSize: 12.5, overflowWrap: "anywhere", color: K.accentDeep }} data-testid="academy-cert-link">
            {link}
          </a>
        </div>
      )}
      {note && (
        <span role="alert" style={{ fontSize: 12.5, color: K.amber }}>
          {note}
        </span>
      )}
    </div>
  );
}
