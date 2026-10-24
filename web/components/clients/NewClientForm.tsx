"use client";
// web/components/clients/NewClientForm.tsx — "New client" on A-06 (AA-937). A name is enough (a companion without
// an e-mail is a valid client, card CRM-09 E test); an e-mail or phone already on file answers 409 with that client's
// id (body traveler_id), and the form offers to open it (R6 dedupe) instead of creating a twin.
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, K } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { type Lang, t } from "@/lib/i18n";

const input: React.CSSProperties = {
  font: "inherit",
  fontSize: 13,
  padding: "6px 8px",
  borderRadius: 8,
  border: `1px solid ${K.line2}`,
  background: K.card,
  color: K.ink,
  minWidth: 0,
  flex: "1 1 180px",
};

export function NewClientForm({ lang }: { lang: Lang }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [existing, setExisting] = useState<string | null>(null);

  if (!open) {
    return (
      <div>
        <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
          + {t(lang, "clients.new")}
        </Button>
      </div>
    );
  }

  async function create() {
    setBusy(true);
    setError(null);
    setExisting(null);
    const res = await bffSend<{ traveler_id: string }>("POST", "travelers", {
      display_name: name,
      email: email.trim() || null,
      phone: phone.trim() || null,
    });
    setBusy(false);
    if (res.ok) {
      router.push(`/clients/${res.data.traveler_id}`);
      return;
    }
    if (res.status === 409) {
      setError(t(lang, "clients.duplicate"));
      const id = (res.body as { traveler_id?: unknown } | undefined)?.traveler_id;
      if (typeof id === "string" && /^[0-9a-f-]{36}$/.test(id)) setExisting(id);
      return;
    }
    setError(t(lang, "clients.error.save"));
  }

  return (
    <form
      data-testid="new-client"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) void create();
      }}
      style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: 12 }}
    >
      <input aria-label={t(lang, "clients.newName")} placeholder={t(lang, "clients.newName")} value={name} onChange={(e) => setName(e.target.value)} maxLength={120} style={input} required />
      <input aria-label={t(lang, "clients.newEmail")} placeholder={t(lang, "clients.newEmail")} value={email} onChange={(e) => setEmail(e.target.value)} maxLength={254} type="email" style={input} />
      <input aria-label={t(lang, "clients.newPhone")} placeholder={t(lang, "clients.newPhone")} value={phone} onChange={(e) => setPhone(e.target.value)} maxLength={40} style={input} />
      <Button size="sm" type="submit" variant="primary" disabled={busy || !name.trim()}>
        {t(lang, "clients.create")}
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
        {t(lang, "clients.cancel")}
      </Button>
      {error && (
        <p role="alert" style={{ margin: 0, width: "100%", fontSize: 12.5, color: K.danger }}>
          {error} {existing && <a href={`/clients/${existing}`}>{t(lang, "clients.openExisting")}</a>}
        </p>
      )}
    </form>
  );
}
