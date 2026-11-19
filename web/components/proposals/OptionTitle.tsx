"use client";
// web/components/proposals/OptionTitle.tsx — AA-1144 (S285 decision D9): the name of a builder option and the agency's
// one note line under it. An Adventure Asia item's name is plain text (AA books exactly that service; the API refuses
// a rename with aa_title_read_only); "Add note" / "Edit note" asks for one line (an empty line removes it), saved
// through the builder's own write (seq, conflicts). The tier and the AA nights follow the name as before.
import { Button, K } from "@/app/_kit";
import { fmt } from "@/lib/i18n";
import { noteBody, type BuilderOption } from "@/lib/builder";

export function OptionTitle({
  option: o,
  editable,
  tr,
  onNote,
}: {
  option: BuilderOption;
  /** A draft the member may edit, hydrated and not busy. */
  editable: boolean;
  tr: (key: string) => string;
  /** The PATCH body of the option ({display_note}); resolves true when saved. */
  onNote: (body: { display_note: string }) => Promise<boolean>;
}) {
  function editNote() {
    const body = noteBody(window.prompt(tr("builder.option.notePrompt"), o.display_note ?? ""), o.display_note);
    if (body) void onNote(body);
  }

  return (
    <span style={{ flex: "1 1 160px", color: K.ink, overflowWrap: "anywhere", minWidth: 0 }} data-testid="builder-option-title">
      {o.title}
      {o.source === "aa" && <span style={{ color: K.muted }}> · AA</span>}
      {o.aa?.nights ? <span style={{ color: K.muted }}> · {fmt(tr("builder.option.nights"), { n: o.aa.nights })}</span> : null}
      {o.tier && <span style={{ color: K.muted }}> · {tr(`builder.tier.${o.tier}`)}</span>}
      {o.display_note && (
        <span style={{ display: "block", color: K.muted, fontSize: 12 }} data-testid="builder-option-note">
          {tr("builder.option.note")}: {o.display_note}
        </span>
      )}
      {editable && (
        <span style={{ display: "block" }}>
          <Button size="sm" variant="ghost" onClick={editNote}>
            {tr(o.display_note ? "builder.option.editNote" : "builder.option.addNote")}
          </Button>
        </span>
      )}
    </span>
  );
}
