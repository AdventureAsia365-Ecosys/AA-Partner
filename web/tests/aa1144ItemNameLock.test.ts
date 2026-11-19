// AA-1144 (S285 decisions D8, D9): the quote item note (lib/builder.ts noteBody, the refusal codes) and the studio's
// fact_locked refusal of a tour copy edit (studioError.ts factLockedError).
import { describe, expect, it } from "vitest";
import { builderErrorKey, noteBody } from "@/lib/builder";
import { t } from "@/lib/i18n";
import { factLockedError, plainError, StudioError } from "@/app/(studio)/portal/_components/studioError";

describe("the agency's note line under a quote item", () => {
  it("sends one clean line, an empty line removes the note, a cancel or no change sends nothing", () => {
    expect(noteBody("  Sea view \n on request ", null)).toEqual({ display_note: "Sea view on request" });
    expect(noteBody("", "Sea view")).toEqual({ display_note: "" });
    expect(noteBody(null, "Sea view")).toBeNull();
    expect(noteBody("Sea  view", "Sea view")).toBeNull();
    expect(noteBody("", null)).toBeNull();
  });

  it("words the refusals in both languages", () => {
    for (const code of ["aa_title_read_only", "display_note_too_long", "invalid_display_note"]) {
      const key = builderErrorKey(code);
      expect(key).not.toBe("builder.err.generic");
      expect(t("en", key)).not.toBe(t("vi", key));
    }
    expect(t("en", builderErrorKey("aa_title_read_only"))).toMatch(/keeps its name/);
    for (const key of ["builder.option.note", "builder.option.addNote", "builder.option.editNote", "builder.option.notePrompt"] as const) {
      expect(t("en", key)).toBeTruthy();
      expect(t("vi", key)).toBeTruthy();
    }
  });
});

describe("a tour copy edit that changes a tour fact", () => {
  const body = { detail: { code: "fact_locked", message: "x", locked: [{ field: "summary", fact: "figure", value: "3 days" }] } };

  it("becomes the factLocked error, worded in the UI language", () => {
    const e = factLockedError(422, body);
    expect(e).toBeInstanceOf(StudioError);
    expect(e?.code).toBe("factLocked");
    expect(plainError(e, "fallback", "en")).toMatch(/changes a tour fact/);
    expect(plainError(e, "fallback", "vi")).toMatch(/dữ kiện của tour/);
  });

  it("leaves any other answer alone", () => {
    expect(factLockedError(422, { detail: "bad" })).toBeNull();
    expect(factLockedError(500, null)).toBeNull();
    expect(factLockedError(422, { detail: { code: "other" } })).toBeNull();
  });
});
