"use client";
// web/components/tags/TagBar.tsx — the tags of one trip or one client (card CRM-29b, AA-960; design v3 R3, R29, R36,
// A-03 / A-07): the agency's tags with a remove button, the app's system tags with their reasons ("Trekking: from the AA
// tour T-100", "Ready to travel again: came home 14 months ago"), and "Add a tag": pick one of the agency's tags or
// make a new one (any member; the owner renames and archives them in Settings > Tags). Putting a tag on can start the
// agency's tag automations; on an archived client none starts (card R) and the bar says so. Loaded in the browser after
// the page (GET {trips|travelers}/{id}/tags, GET /tags?applies_to=).
import { X } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge, Button, IconButton, Input, K, Select, useHydrated } from "@/app/_kit";
import { Panel } from "@/components/PageTitle";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { reasonText, tagErrorKey, tagLabel, type Subject, type Tag, type TagLink, type TagVocabulary } from "@/lib/tags";

const NEW = "__new__";

export function TagBar({ subject, id, lang }: { subject: Subject; id: string; lang: Lang }) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const base = `${subject === "trip" ? "trips" : "travelers"}/${id}/tags`;
  const [links, setLinks] = useState<TagLink[] | null>(null);
  const [vocab, setVocab] = useState<Tag[]>([]);
  const [pick, setPick] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let stopped = false;
    void (async () => {
      const [mine, all] = await Promise.all([
        bffSend<{ tags: TagLink[] }>("GET", base),
        bffSend<TagVocabulary>("GET", `tags?applies_to=${subject}`),
      ]);
      if (stopped) return;
      setLinks(mine.ok ? mine.data.tags : []);
      if (all.ok) setVocab(all.data.tags.filter((x) => x.kind === "agency" && !x.archived));
    })();
    return () => {
      stopped = true;
    };
  }, [base, subject, tick]);

  function fail(detail: string, redirecting?: boolean) {
    if (!redirecting) setNotice({ tone: "danger", text: tr(tagErrorKey(detail)) });
  }

  async function put(tagId: string) {
    const res = await bffSend<{ added: boolean; workflows_started: number; workflows_suppressed: boolean }>("PUT", `${base}/${tagId}`);
    if (!res.ok) return fail(res.detail, res.redirecting);
    if (res.data.workflows_suppressed) setNotice({ tone: "success", text: tr("tag.workflowsSuppressed") });
    else if (res.data.workflows_started) setNotice({ tone: "success", text: fmt(tr("tag.workflowsStarted"), { n: res.data.workflows_started }) });
    else setNotice(null);
  }

  async function add() {
    setBusy(true);
    if (pick === NEW) {
      const made = await bffSend<Tag>("POST", "tags", { applies_to: subject, name });
      if (!made.ok) {
        // The name exists: use that tag.
        const existing = (made.body as { tag_id?: string } | undefined)?.tag_id;
        if (made.detail === "tag_exists" && existing) await put(existing);
        else fail(made.detail, made.redirecting);
      } else {
        await put(made.data.tag_id);
      }
    } else if (pick) {
      await put(pick);
    }
    setBusy(false);
    setPick("");
    setName("");
    setTick((n) => n + 1);
  }

  async function remove(link: TagLink) {
    setBusy(true);
    const res = await bffSend("DELETE", `${base}/${link.tag_id}`);
    setBusy(false);
    if (!res.ok) return fail(res.detail, res.redirecting);
    setNotice(null);
    setTick((n) => n + 1);
  }

  const onIt = new Set((links ?? []).map((l) => l.tag_id));
  const agency = (links ?? []).filter((l) => l.kind === "agency");
  const system = (links ?? []).filter((l) => l.kind === "system");
  const options = [
    ...vocab.filter((x) => !onIt.has(x.tag_id)).map((x) => ({ value: x.tag_id, label: x.name })),
    { value: NEW, label: `+ ${tr("tag.new")}` },
  ];

  return (
    <Panel title={tr("tag.title")} testId={`${subject}-tags`}>
      <div style={{ display: "grid", gap: 10 }}>
        {notice && (
          <p role="status" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
            {notice.text}
          </p>
        )}
        {links !== null && links.length === 0 && <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{tr("tag.none")}</p>}
        {agency.length > 0 && (
          <ul aria-label={tr("tag.title")} style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexWrap: "wrap", gap: 6 }}>
            {agency.map((l) => (
              <li key={l.tag_link_id} data-testid="tag-chip" data-code={l.code} style={{ display: "inline-flex", alignItems: "center", gap: 2 }}>
                <Badge tone={l.tone} dot={false}>
                  {l.name}
                </Badge>
                <IconButton size="xs" variant="plain" tone="danger" label={fmt(tr("tag.remove"), { tag: l.name })} disabled={busy || !hydrated} onClick={() => void remove(l)}>
                  <X size={12} aria-hidden />
                </IconButton>
              </li>
            ))}
          </ul>
        )}
        {system.length > 0 && (
          <div style={{ display: "grid", gap: 6 }} data-testid="system-tags">
            <span style={{ fontSize: 12, color: K.muted }} title={tr("tag.systemHint")}>
              {tr("tag.system")}
            </span>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
              {system.map((l) => (
                <li key={l.tag_link_id} data-testid="system-tag" data-code={l.code} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                  <Badge tone={l.tone} dot={false}>
                    {tagLabel(lang, l)}
                  </Badge>
                  {l.reasons.length > 0 && (
                    <span style={{ fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }}>
                      {l.reasons.map((r) => reasonText(lang, r)).join(" · ")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }} data-testid="tag-add">
          <span style={{ minWidth: 200 }}>
            <Select size="sm" aria-label={tr("tag.add")} value={pick} placeholder={tr("tag.addPick")} onChange={(e) => setPick(e.target.value)} options={options} />
          </span>
          {pick === NEW && (
            <span style={{ minWidth: 200, flex: "1 1 200px" }}>
              <Input size="sm" aria-label={tr("tag.newName")} placeholder={tr("tag.newName")} maxLength={60} value={name} onChange={(e) => setName(e.target.value)} />
            </span>
          )}
          <Button size="sm" variant="secondary" disabled={!pick || (pick === NEW && !name.trim()) || busy || !hydrated} onClick={() => void add()}>
            {pick === NEW ? tr("tag.create") : tr("tag.add")}
          </Button>
        </div>
        {pick === NEW && <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{tr("tag.newHint")}</p>}
      </div>
    </Panel>
  );
}
