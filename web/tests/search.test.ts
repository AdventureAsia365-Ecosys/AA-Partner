// AA-963 (CRM-32, R35) — the global search in the agency app: the accent fold of the Content pieces filter (E: "Đà Lạt"
// = "Da Lat"), the config read that fails closed, where each result opens, the refusals, the BFF routes (the term never
// in a URL), the studio pieces only for this agency, and the Library page's entries.
import { describe, expect, it } from "vitest";
import { matchBff } from "@/lib/bff";
import {
  SEARCH_OFF,
  contentPieceHref,
  foldText,
  hrefFor,
  libraryEntries,
  matchContentPieces,
  paletteGroups,
  parseConfig,
  noticeOf,
  refusalOf,
  statusFor,
  shouldSearch,
  type SearchConfig,
  type SearchItem,
} from "@/lib/search";
import { fetchContentPieces, loadContentPieces, resetContentPieces } from "@/lib/searchContent";

const ON: SearchConfig = { enabled: true, kinds: ["client", "trip"], query_max_chars: 120, min_text_chars: 2, results_per_kind: 5 };
const T = "00000000-0000-4000-8000-0000000009a1";

function item(meta: SearchItem["meta"] = {}, id = T): SearchItem {
  return { id, label: "Label", meta };
}

describe("accent fold (E)", () => {
  it.each([
    ["Đà Lạt", "da lat"],
    ["ĐÀ LẠT", "da lat"],
    ["Hội An", "hoi an"],
    ["Ølsted Łódź", "olsted lodz"],
    ["Straße", "strasse"],
    ["Café Crème", "cafe creme"],
  ])("%s folds to %s", (a, b) => {
    expect(foldText(a)).toBe(b);
    expect(foldText(b)).toBe(b);
  });

  it("matches Content pieces by title or tour, folded, up to the limit", () => {
    const pieces = [
      { pieceId: "p1", title: "Mùa hoa Đà Lạt", tour: null },
      { pieceId: "p2", title: "Weekend in the hills", tour: "Đà Lạt highlands" },
      { pieceId: "p3", title: "Hội An lanterns", tour: "Hội An" },
    ];
    expect(matchContentPieces(pieces, "da lat", 5).map((p) => p.pieceId)).toEqual(["p1", "p2"]);
    expect(matchContentPieces(pieces, "DA LAT", 1).map((p) => p.pieceId)).toEqual(["p1"]);
    expect(matchContentPieces(pieces, "  ", 5)).toEqual([]);
    expect(matchContentPieces(pieces, "hoi", 0)).toEqual([]);
  });
});

describe("config", () => {
  it("fails closed: anything but an explicit enabled: true is off", () => {
    for (const bad of [null, {}, { enabled: "true", kinds: [] }, { enabled: true }, { enabled: false, kinds: ["client"] }]) {
      expect(parseConfig(bad)).toEqual(SEARCH_OFF);
    }
  });

  it("keeps only the kinds the app knows", () => {
    const cfg = parseConfig({ enabled: true, kinds: ["client", "teleport", "tag"], query_max_chars: 120, min_text_chars: 2, results_per_kind: 5 });
    expect(cfg.kinds).toEqual(["client", "tag"]);
  });

  it("asks the API only for a term worth sending", () => {
    expect(shouldSearch("", ON)).toBe(false);
    expect(shouldSearch("a", ON)).toBe(false);
    expect(shouldSearch("ab", ON)).toBe(true);
    expect(shouldSearch("a@", ON)).toBe(true); // the API explains the partial e-mail refusal
    expect(shouldSearch("+8", ON)).toBe(true);
    expect(shouldSearch("x".repeat(121), ON)).toBe(true); // the API answers query_too_long
    expect(shouldSearch("Da Lat", SEARCH_OFF)).toBe(false);
  });
});

describe("results", () => {
  it("open the page of their kind, never a URL that carries the term", () => {
    expect(hrefFor("client", item())).toBe(`/clients/${T}`);
    expect(hrefFor("trip", item())).toBe(`/pipeline/${T}`);
    expect(hrefFor("booking", item({ trip_id: T }))).toBe(`/pipeline/${T}`);
    expect(hrefFor("booking", item())).toBe("/pipeline");
    expect(hrefFor("tag", item({ applies_to: "client", code: "vip" }))).toBe("/clients?tag=vip");
    expect(hrefFor("tag", item({ applies_to: "trip", code: "honeymoon" }))).toBe("/settings/tags");
    expect(hrefFor("whatsapp_template", item())).toBe("/settings/channels");
    expect(contentPieceHref("p 1")).toBe("/portal/content?piece=p%201");
  });

  it("group in the API's order, then the Content pieces as a full page load", () => {
    const groups = paletteGroups(
      {
        mode: "text",
        kinds: ["client", "booking"],
        groups: [
          { kind: "client", items: [item({ archived: true })] },
          { kind: "booking", items: [item({ trip_id: T, trip_title: "Hill weekend" }, "b1")] },
        ],
      },
      [{ pieceId: "p1", title: "Mùa hoa Đà Lạt", tour: "Đà Lạt highlands" }],
      "Archived",
    );
    expect(groups.map((g) => g.kind)).toEqual(["client", "booking", "content"]);
    expect(groups[0].rows[0]).toMatchObject({ sub: "Archived", external: false });
    expect(groups[1].rows[0]).toMatchObject({ sub: "Hill weekend", href: `/pipeline/${T}` });
    expect(groups[2].rows[0]).toMatchObject({ external: true, href: "/portal/content?piece=p1" });
  });

  it("explain the API's refusals and nothing else", () => {
    expect(refusalOf("partial_contact_search")).toBe("partial_contact_search");
    expect(refusalOf("search_not_enabled")).toBe("search_not_enabled");
    expect(refusalOf("role_forbidden")).toBeNull();
    expect(refusalOf("query_too_short")).toBe("query_too_short"); // a term that folds to too few letters
  });

  it("say why clients were left out of a text search, and nothing else", () => {
    const base = { mode: "text" as const, kinds: [], groups: [] };
    expect(noticeOf({ ...base, notice: "partial_search_refused" })).toBe("partial_search_refused");
    expect(noticeOf({ ...base, notice: "partial_contact_search" })).toBe("partial_contact_search");
    expect(noticeOf({ ...base, notice: null })).toBeNull();
    expect(noticeOf(base)).toBeNull();
    expect(noticeOf(null)).toBeNull();
  });
});

describe("the status of the term typed now", () => {
  const done = { kind: "done" as const, term: "Da Lat", result: { mode: "text" as const, kinds: [], groups: [] } };

  it("keeps an answer only for its own term", () => {
    expect(statusFor(done, " Da Lat ", ON)).toBe(done);
    // typed on, the debounce has not sent the new term: searching, never the previous term's results
    expect(statusFor(done, "Da Lat x", ON)).toEqual({ kind: "loading", term: "Da Lat x" });
    expect(statusFor(done, "D", ON)).toEqual({ kind: "idle" }); // too short to send
    expect(statusFor({ kind: "error", term: "anna@", key: "search.error" }, "anna@x", ON)).toEqual({ kind: "loading", term: "anna@x" });
    expect(statusFor({ kind: "idle" }, "", ON)).toEqual({ kind: "idle" });
  });
});

describe("BFF allow-list", () => {
  it("has the config read and the POST search, never a GET search", () => {
    expect(matchBff("GET", "search/config")?.tenant).toBe(true);
    expect(matchBff("POST", "search")?.tenant).toBe(true);
    expect(matchBff("GET", "search")).toBeNull();
    expect(matchBff("POST", "search/config")).toBeNull();
  });
});

describe("Content pieces of the studio", () => {
  function fake(tenant: string | null, pieces: unknown[] = []): typeof fetch {
    return (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/tenant/me") {
        return tenant ? new Response(JSON.stringify({ tenant_id: tenant })) : new Response("{}", { status: 401 });
      }
      return new Response(JSON.stringify({ data: pieces }));
    }) as typeof fetch;
  }
  const piece = { piece_id: "p1", content_text: "# Mùa hoa Đà Lạt\nText", angle: null, tour: { name: "Đà Lạt highlands" } };

  it("are read for this agency only", async () => {
    expect(await fetchContentPieces(T, "en", fake(T, [piece]))).toEqual([{ pieceId: "p1", title: "Mùa hoa Đà Lạt", tour: "Đà Lạt highlands" }]);
    expect(await fetchContentPieces(T, "en", fake("00000000-0000-4000-8000-0000000009a2", [piece]))).toEqual([]);
    expect(await fetchContentPieces(T, "en", fake(null, [piece]))).toEqual([]);
  });

  function counting(failFirst: number, pieces: unknown[]): { fetchImpl: typeof fetch; calls: () => number } {
    let n = 0;
    const fetchImpl = (async (input: RequestInfo | URL) => {
      if (String(input) !== "/api/tenant/me") return new Response(JSON.stringify({ data: pieces }));
      n += 1;
      return n <= failFirst ? new Response("{}", { status: 503 }) : new Response(JSON.stringify({ tenant_id: T }));
    }) as typeof fetch;
    return { fetchImpl, calls: () => n };
  }

  it("never keep a failed read: the next call reads again", async () => {
    resetContentPieces();
    const open = {};
    const { fetchImpl, calls } = counting(1, [piece]);
    expect(await loadContentPieces(T, "en", open, fetchImpl)).toEqual([]); // CIS briefly down
    expect(await loadContentPieces(T, "en", open, fetchImpl)).toHaveLength(1); // asked again, same palette open
    expect(await loadContentPieces(T, "en", open, fetchImpl)).toHaveLength(1);
    expect(calls()).toBe(2); // a good read is reused while the palette stays open
  });

  it("are read again at the next palette open (a deleted piece drops out)", async () => {
    resetContentPieces();
    const first = counting(0, [piece]);
    expect(await loadContentPieces(T, "en", {}, first.fetchImpl)).toHaveLength(1);
    const after = counting(0, []); // the piece was deleted in the studio
    expect(await loadContentPieces(T, "en", {}, after.fetchImpl)).toEqual([]);
    expect(after.calls()).toBe(1);
  });
});

describe("Library page", () => {
  it("lists the three CRM libraries, and the Content pieces only with studio access", () => {
    expect(libraryEntries(null).map((e) => e.key)).toEqual(["templates", "workflows", "inclusions"]);
    const withStudio = libraryEntries("/portal/dashboard");
    expect(withStudio.map((e) => e.key)).toEqual(["templates", "workflows", "inclusions", "content"]);
    expect(withStudio[3]).toEqual({ key: "content", href: "/portal/content", external: true });
  });
});
