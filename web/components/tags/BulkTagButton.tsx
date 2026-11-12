"use client";
// web/components/tags/BulkTagButton.tsx — tag several trips or clients at once (card CRM-29b, AA-960; design v3 R3,
// WF-1 column menu "tag", card E "bulk apply previews the count"): pick a tag and add or remove, Preview (the dry run:
// how many change, how many already have it, held records skipped, archived clients tagged without their automations),
// then Apply with the previewed count (the API refuses another count: 409 count_changed, the selection moved). Owner and
// agent only (the API checks it too). The selection is a filter of the page it sits on (a pipeline stage, the clients
// directory's filters) or explicit ids.
import { useState } from "react";
import { Button, K, Modal, SegmentedControl, Select, useHydrated } from "@/app/_kit";
import { bffSend } from "@/lib/bffClient";
import { fmt, t, type Lang, type MessageKey } from "@/lib/i18n";
import { bulkSummary, tagErrorKey, type BulkResult, type BulkSelection, type Subject, type Tag, type TagVocabulary } from "@/lib/tags";

export function BulkTagButton({ subject, selection, scope, label, lang, testId = "bulk-tag" }: {
  subject: Subject;
  selection: BulkSelection;
  /** Words for what the selection is ("Proposal stage", "Active clients"). */
  scope: string;
  label?: string;
  lang: Lang;
  testId?: string;
}) {
  const hydrated = useHydrated();
  const tr = (key: string) => t(lang, key as MessageKey);
  const [open, setOpen] = useState(false);
  const [tags, setTags] = useState<Tag[]>([]);
  const [tagId, setTagId] = useState("");
  const [action, setAction] = useState<"add" | "remove">("add");
  const [preview, setPreview] = useState<BulkResult | null>(null);
  const [notice, setNotice] = useState<{ tone: "danger" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function openDialog() {
    setOpen(true);
    setPreview(null);
    setNotice(null);
    const res = await bffSend<TagVocabulary>("GET", `tags?applies_to=${subject}`);
    if (res.ok) setTags(res.data.tags.filter((x) => x.kind === "agency" && !x.archived));
  }

  async function run(dryRun: boolean) {
    if (!tagId) return;
    setBusy(true);
    const body = { action, ids: selection.ids ?? [], filter: selection.filter ?? null, dry_run: dryRun, expected: dryRun ? null : preview?.count };
    const res = await bffSend<BulkResult>("POST", `tags/${tagId}/bulk`, body, { renew: true });
    setBusy(false);
    if (!res.ok) {
      if (!res.redirecting) setNotice({ tone: "danger", text: tr(tagErrorKey(res.detail)) });
      if (res.detail === "count_changed") setPreview(null);
      return;
    }
    if (dryRun) {
      setPreview(res.data);
      setNotice(null);
      return;
    }
    setPreview(null);
    setNotice({ tone: "success", text: fmt(tr("tag.bulk.done"), { n: res.data.count }) });
  }

  const tagName = tags.find((x) => x.tag_id === tagId)?.name ?? "";
  return (
    <span data-testid={testId}>
      <Button size="sm" variant="secondary" disabled={!hydrated} onClick={() => void openDialog()}>
        {label ?? tr("tag.bulk.button")}
      </Button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={tr("tag.bulk.title")}
        testId={`${testId}-dialog`}
        footer={
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
            <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
              {tr("tag.bulk.cancel")}
            </Button>
            <Button size="sm" variant="secondary" disabled={!tagId || busy} onClick={() => void run(true)}>
              {tr("tag.bulk.preview")}
            </Button>
            <Button size="sm" variant="primary" disabled={!preview || preview.count === 0 || busy} onClick={() => void run(false)}>
              {fmt(tr("tag.bulk.apply"), { n: preview?.count ?? 0 })}
            </Button>
          </div>
        }
      >
        <div style={{ display: "grid", gap: 12 }}>
          <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{fmt(tr("tag.bulk.scope"), { scope })}</p>
          <SegmentedControl
            label={tr("tag.bulk.action")}
            value={action}
            onChange={(v) => {
              setAction(v);
              setPreview(null);
            }}
            options={[
              { value: "add", label: tr("tag.bulk.add") },
              { value: "remove", label: tr("tag.bulk.removeTag") },
            ]}
          />
          <Select
            size="sm"
            aria-label={tr("tag.filter")}
            value={tagId}
            placeholder={tr("tag.addPick")}
            onChange={(e) => {
              setTagId(e.target.value);
              setPreview(null);
            }}
            options={tags.map((x) => ({ value: x.tag_id, label: x.name }))}
          />
          {preview && (
            <div role="status" data-testid={`${testId}-preview`} data-count={preview.count} style={{ display: "grid", gap: 4, fontSize: 13 }}>
              {preview.count === 0 ? <span>{tr("tag.bulk.nothing")}</span> : null}
              {bulkSummary(lang, preview, tagName).map((line, i) => (
                <span key={i} style={{ color: i === 0 ? K.ink : K.muted }}>
                  {line}
                </span>
              ))}
            </div>
          )}
          {notice && (
            <p role="status" style={{ margin: 0, fontSize: 13, color: notice.tone === "danger" ? K.danger : K.success }}>
              {notice.text}
            </p>
          )}
        </div>
      </Modal>
    </span>
  );
}
