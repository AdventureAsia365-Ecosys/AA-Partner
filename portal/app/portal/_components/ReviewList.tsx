"use client";
// portal/app/portal/_components/ReviewList.tsx — My Content (/portal/content, was t10-review).
// AA-669: the shared UI-kit DataTable over GET /v1/content-writing/reviews (react-query).
//   - columns: title (+ the angle's name under it when it differs from the title), channel, place / topic, goal,
//     status (published / ready / writing / not ready), words, created, published at, actions (view in the drawer,
//     export HTML / fragment / text, publish, write again)
//   - S251 D1/D2: the table fits a 1440 px screen without a sideways scroll (kit `compact` cells, no separate Angle
//     column, titles wrap between words only); the angle's writing brief (formula_fit) is never shown in the list
//   - filters: channel, status, goal, date range (created or published), place search, plus the table's own search
//   - sort: any column; views: "All content", "Ready to publish", "Published this month" and the tenant's own saved
//     views (filters + sort, localStorage through the kit's savedViews store)
//   - bulk: export selected (blog as HTML, other channels as text), publish selected (blog pieces, WordPress
//     connected only)
//   - row click → ReviewDrawer (content with copy-lock, context, flags, inline edit, exports)
// The rules (status, words, title, place, filters, views, who may be published) live in lib/myContent.ts (unit
// tested). Constraints kept from AA-501/AA-519/AA-614: tenant-safe projection (no gate data; a held piece is
// "ready" and its publish is refused by the API without a reason), copy-lock (no content text in the table, only
// its title and word count). Deep link ?piece= opens that piece's drawer; ?view= picks a built-in view.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Download, ExternalLink, Eye, FileDown, RefreshCw, Save, Send, Trash2, X } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { T, sans } from "./ui";
import { ServedBadge, plainError } from "./ServedBadge";
import {
  Badge,
  ConfirmModal,
  type ColumnDef,
  DataTable,
  Drawer,
  formatDate,
  StatusBadge,
  ToastProvider,
  deleteView,
  listViews,
  saveView,
  useHydrated,
  useIsPhone,
  useToast,
} from "@/app/_kit";
import { ReviewDrawer } from "./ReviewDrawer";
import { channelName } from "./channelLabels";
import { useWordPressStatus } from "./WordPressConnect";
import { useFacebookStatus } from "./FacebookConnect";
import { usePortalShell } from "./PortalShellContext";
import {
  exportPiece,
  fetchReviews,
  publishPiece,
  regeneratePiece,
  savePieceText,
  type ReviewItem,
} from "./reviewListApi";
import { activePollInterval, pieceWorkStatus } from "./workStatus";
import {
  builtInViews,
  bulkPublishPlan,
  CONTENT_STATUS_LABEL,
  contentStatus,
  exportFormat,
  angleLabel,
  hasFilters,
  liveIsEarlierVersion,
  matchesFilters,
  NO_FILTERS,
  pieceTitle,
  placeTopic,
  publishBlock,
  regenerateBlock,
  sanitizeView,
  wordCount,
  type ContentFilters,
  type ContentStatus,
  type ContentView,
  type SortRule,
} from "@/lib/myContent";

const VIEWS_TABLE = "my-content-views";
/** Kit Badge tone per status (writing uses the spinning StatusBadge instead). */
const STATUS_TONE: Record<Exclude<ContentStatus, "writing">, "accent" | "success" | "danger"> = {
  published: "accent",
  ready: "success",
  not_ready: "danger",
};

type Confirm =
  | { kind: "publish"; rows: ReviewItem[]; skipped: number }
  | { kind: "regenerate"; row: ReviewItem }
  | null;

function ReviewListInner() {
  const toast = useToast();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const isPhone = useIsPhone();
  const { tenantName } = usePortalShell();
  const viewsUser = tenantName || "tenant";

  const builtIns = useMemo(() => builtInViews(new Date()), []);
  const initialView = builtIns.find((v) => v.id === searchParams.get("view")) ?? builtIns[0];
  const [viewId, setViewId] = useState<string>(initialView.id);
  const [filters, setFilters] = useState<ContentFilters>(initialView.filters);
  const [sorting, setSorting] = useState<SortRule[]>(initialView.sorting);
  // Bumped after a save / delete so the stored views are read again (no setState in an effect).
  const [viewsRev, setViewsRev] = useState(0);
  const hydrated = useHydrated();
  // Open drawer: initialise from ?piece= as a lazy value (client-only), no setState-in-effect.
  const [openPieceId, setOpenPieceId] = useState<string | null>(() => searchParams.get("piece"));
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);

  // The tenant's own views (localStorage via the kit store, client only; a malformed entry is sanitized).
  const userViews = useMemo<ContentView[]>(
    () => (hydrated ? readUserViews(viewsUser) : []),
    // viewsRev: re-read after a save / delete.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [hydrated, viewsUser, viewsRev],
  );

  const { data: items = [], isLoading, isError, error, refetch } = useQuery({
    queryKey: ["content-reviews"],
    queryFn: fetchReviews,
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      // AA-760 — shared work-status model: poll while a piece is writing, stop once all settled.
      return activePollInterval(rows.map(pieceWorkStatus));
    },
  });
  const wp = useWordPressStatus();
  const fb = useFacebookStatus();
  const connections = useMemo(
    () => ({ wordpress: !!wp.status?.connected, facebook: !!fb.status?.connected }),
    [wp.status, fb.status],
  );

  const invalidate = () => qc.invalidateQueries({ queryKey: ["content-reviews"] });

  const saveM = useMutation({
    mutationFn: (vars: { pieceId: string; text: string }) => savePieceText(vars.pieceId, vars.text),
    onSuccess: () => {
      toast.success("Saved");
      invalidate();
    },
    onError: () => toast.error("Couldn't save — try again."),
  });

  const channels = useMemo(() => [...new Set(items.map((i) => i.channel))].sort(), [items]);
  const goals = useMemo(
    () => ([...new Set(items.map((i) => i.goal?.label).filter(Boolean))] as string[]).sort(),
    [items],
  );
  const visible = useMemo(() => items.filter((it) => matchesFilters(it, filters)), [items, filters]);
  const openItem = useMemo(
    () => (openPieceId ? items.find((i) => i.piece_id === openPieceId) ?? null : null),
    [items, openPieceId],
  );

  // ── views ──
  const allViews = [...builtIns, ...userViews];
  function applyView(id: string) {
    const v = allViews.find((x) => x.id === id);
    if (!v) return;
    setViewId(v.id);
    setFilters(v.filters);
    setSorting(v.sorting);
  }
  function changeFilters(next: Partial<ContentFilters>) {
    setFilters((f) => ({ ...f, ...next }));
    setViewId("");
  }
  function saveCurrentView() {
    const name = typeof window !== "undefined" ? window.prompt("Save this view as:")?.trim() : "";
    if (!name) return;
    saveView(VIEWS_TABLE, viewsUser, name.slice(0, 60), { filters, sorting });
    setViewsRev((n) => n + 1);
    setViewId(`user:${name.slice(0, 60).toLowerCase().replace(/\s+/g, "-")}`);
    toast.success(`View "${name}" saved`);
  }
  function deleteCurrentView() {
    if (!viewId.startsWith("user:")) return;
    deleteView(VIEWS_TABLE, viewsUser, viewId.slice(5));
    setViewsRev((n) => n + 1);
    const all = builtIns[0];
    setViewId(all.id);
    setFilters(all.filters);
    setSorting(all.sorting);
  }

  // ── actions ──
  async function onExport(item: ReviewItem, format: "text" | "html", mode?: "document" | "fragment") {
    try {
      // S251 D5: name the file and the page <title> from the same title the list and the drawer show.
      await exportPiece({ ...item, title: pieceTitle(item) }, format, mode);
    } catch {
      toast.error("Export failed — try again.");
    }
  }

  async function bulkExport(rows: ReviewItem[]) {
    const exportable = rows.filter((r) => exportFormat(r) !== null);
    if (exportable.length === 0) {
      toast.info("Nothing to export yet in the selection.");
      return;
    }
    for (const r of exportable) {
      await onExport(r, exportFormat(r) ?? "text");
    }
    toast.success(`Exported ${exportable.length} piece${exportable.length === 1 ? "" : "s"}`);
  }

  function askBulkPublish(rows: ReviewItem[]) {
    if (!connections.wordpress) {
      toast.info("Connect WordPress to publish blog posts from here (Account › Integrations).");
      return;
    }
    const plan = bulkPublishPlan(rows, connections);
    if (plan.eligible.length === 0) {
      toast.info("Only ready blog posts that are not published yet can be published together.");
      return;
    }
    setConfirm({ kind: "publish", rows: plan.eligible as ReviewItem[], skipped: plan.skipped });
  }

  const publishM = useMutation({
    mutationFn: async (rows: ReviewItem[]) => {
      let ok = 0;
      const failures: string[] = [];
      for (const r of rows) {
        try {
          await publishPiece(r.piece_id);
          ok += 1;
        } catch (e) {
          failures.push(`${pieceTitle(r)}: ${plainError(e, "failed")}`);
        }
      }
      return { ok, failures };
    },
    onSuccess: ({ ok, failures }) => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["publish-log"] });
      if (ok > 0) toast.success(`Published ${ok} piece${ok === 1 ? "" : "s"}`);
      if (failures.length === 1) toast.error(failures[0]);
      else if (failures.length > 1) toast.error(`${failures.length} could not publish — ${failures[0]}`);
      setConfirm(null);
    },
  });

  const regenerateM = useMutation({
    mutationFn: (row: ReviewItem) => regeneratePiece(row.request_id),
    onSuccess: () => {
      toast.success("Writing a new version — it shows here as Writing, then Ready.");
      invalidate();
      setConfirm(null);
    },
    onError: (e) => {
      toast.error(plainError(e, "Could not start the rewrite."));
      setConfirm(null);
    },
  });

  const columns = useMemo<ColumnDef<ReviewItem, unknown>[]>(
    () => [
      {
        id: "title",
        accessorFn: (r) => pieceTitle(r),
        header: "Title",
        cell: (c) => {
          const it = c.row.original;
          const angle = angleLabel(it);
          // S251 D1: a real minimum width and wrapping between words only (overflowWrap "anywhere" let the column
          // shrink to ~68 px and broke words mid-letter).
          return (
            <div style={{ minWidth: 160, maxWidth: 300 }} data-testid="my-content-title">
              <div style={{ fontWeight: 600, color: T.ink, overflowWrap: "break-word", lineHeight: 1.4 }}>{pieceTitle(it)}</div>
              {angle && (
                <div style={{ marginTop: 2, color: T.muted, fontSize: 12, lineHeight: 1.4, overflowWrap: "break-word" }}>
                  Angle: {angle}
                </div>
              )}
              {/* AA-986 L3: persistent badge when AA no longer serves (or has paused) the tour */}
              {it.served_notice && <div style={{ marginTop: 4 }}><ServedBadge fields={it} /></div>}
            </div>
          );
        },
      },
      {
        id: "channel",
        accessorFn: (r) => channelName(r.channel),
        header: "Channel",
        cell: (c) => <Badge tone="neutral">{channelName(c.row.original.channel)}</Badge>,
      },
      {
        id: "place",
        accessorFn: (r) => placeTopic(r) ?? "",
        header: "Place / topic",
        cell: (c) => (
          <span style={{ display: "inline-block", color: T.body, fontSize: 12.5, minWidth: 110, maxWidth: 220, lineHeight: 1.45 }}>
            {placeTopic(c.row.original) ?? "—"}
          </span>
        ),
      },
      {
        id: "goal",
        accessorFn: (r) => r.goal?.label ?? "",
        header: "Goal",
        // AA-760: "Trust-building" broke at the hyphen, so each hyphenated word sits in a nowrap span (the text
        // people copy keeps its plain "-"). S251 D1: the label may wrap between words ("Introduction / Awareness"
        // on two lines) to keep the table inside the screen.
        cell: (c) => {
          const label = c.row.original.goal?.label;
          if (!label) return <span style={{ color: T.body, lineHeight: 1.45 }}>—</span>;
          return (
            <span style={{ color: T.body, lineHeight: 1.45 }}>
              {label.split(" ").map((w, i) => (
                <Fragment key={i}>
                  {i > 0 ? " " : null}
                  {w.includes("-") ? <span style={{ whiteSpace: "nowrap" }}>{w}</span> : w}
                </Fragment>
              ))}
            </span>
          );
        },
      },
      {
        id: "status",
        accessorFn: (r) => CONTENT_STATUS_LABEL[contentStatus(r)],
        header: "Status",
        cell: (c) => {
          const s = contentStatus(c.row.original);
          // "writing" spins (StatusBadge running); the others are plain tones: published = gold, ready = green.
          return s === "writing"
            ? <StatusBadge status="running" label={CONTENT_STATUS_LABEL[s]} />
            : <Badge tone={STATUS_TONE[s]} dot>{CONTENT_STATUS_LABEL[s]}</Badge>;
        },
      },
      {
        id: "words",
        accessorFn: (r) => wordCount(r.content_text) ?? undefined,
        header: "Words",
        sortUndefined: "last",
        meta: { align: "right" },
        cell: (c) => {
          const n = wordCount(c.row.original.content_text);
          return <span style={{ color: T.muted, fontSize: 12 }}>{n == null ? "—" : n.toLocaleString("en")}</span>;
        },
      },
      {
        id: "created",
        accessorFn: (r) => r.created_at ?? "",
        header: "Created",
        cell: (c) => (
          <span style={{ color: T.muted, fontSize: 12, whiteSpace: "nowrap" }}>
            {formatDate(c.row.original.created_at)}
          </span>
        ),
      },
      {
        id: "published",
        accessorFn: (r) => r.published_at ?? "",
        header: "Published",
        cell: (c) => {
          const it = c.row.original;
          if (!it.published_at) return <span style={{ color: T.muted2 }}>—</span>;
          return (
            <span style={{ color: T.muted, fontSize: 12, whiteSpace: "nowrap", display: "inline-flex", gap: 4, alignItems: "center" }}>
              {formatDate(it.published_at)}
              {it.published_url && (
                <a href={it.published_url} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
                   aria-label="Open the published post"
                   title={liveIsEarlierVersion(it) ? "Open the published post (it shows an earlier version of this text)" : "Open the published post"}
                   style={{ color: T.goldDeep, display: "inline-flex" }}>
                  <ExternalLink size={12} />
                </a>
              )}
            </span>
          );
        },
      },
      {
        id: "actions",
        header: "",
        enableSorting: false,
        enableHiding: false,
        meta: { hideLabelOnCard: true },
        cell: (c) => (
          <RowActions
            item={c.row.original}
            menuOpen={menuFor === c.row.original.piece_id}
            onMenu={(open) => setMenuFor(open ? c.row.original.piece_id : null)}
            publishReason={publishBlock(c.row.original, connections)}
            onView={() => setOpenPieceId(c.row.original.piece_id)}
            onExport={(format, mode) => { setMenuFor(null); void onExport(c.row.original, format, mode); }}
            onPublish={() => setConfirm({ kind: "publish", rows: [c.row.original], skipped: 0 })}
            onRegenerate={() => setConfirm({ kind: "regenerate", row: c.row.original })}
          />
        ),
      },
    ],
    // onExport is stable in behaviour (reads nothing from render state but the toast).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [menuFor, connections],
  );

  const sel: React.CSSProperties = isPhone ? { ...selectStyle, flex: "1 1 140px", minWidth: 0 } : selectStyle;
  const filtered = hasFilters(filters);

  return (
    <>
      <DataTable<ReviewItem>
        data={visible}
        columns={columns}
        tableId="my-content-pieces"
        getRowId={(r) => r.piece_id}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : "Could not load your content") : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder="Search title, place…"
        enableSelection
        enableCsv
        csvFilename="my-content"
        pageSize={25}
        tableMinWidth={1040}
        compact
        sorting={sorting}
        onSortingChange={(next) => { setSorting(next); setViewId(""); }}
        emptyTitle={filtered ? "Nothing matches these filters" : "Nothing written yet"}
        emptyDescription={
          filtered
            ? "Clear the filters or pick another view."
            : "Once you've written content from Social Posts or Articles, it'll show up here to review before publishing."
        }
        emptyAction={filtered ? (
          <button style={linkBtn} onClick={() => applyView("all")}>Show all content</button>
        ) : undefined}
        onRowClick={(r) => setOpenPieceId(r.piece_id)}
        toolbarExtra={
          // AA-836 / AA-760 — the controls wrap inside their own row on a phone and share it evenly.
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", maxWidth: "100%", minWidth: 0, ...(isPhone ? { flex: "1 1 100%" } : {}) }}>
            <select value={viewId} onChange={(e) => applyView(e.target.value)} style={{ ...sel, fontWeight: 600 }} aria-label="View" data-testid="my-content-view">
              {viewId === "" && <option value="">Custom view</option>}
              {builtIns.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
              {userViews.length > 0 && (
                <optgroup label="Your views">
                  {userViews.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
                </optgroup>
              )}
            </select>
            <button style={iconBtn} onClick={saveCurrentView} title="Save these filters and sort as a view" aria-label="Save view">
              <Save size={13} />
            </button>
            {viewId.startsWith("user:") && (
              <button style={iconBtn} onClick={deleteCurrentView} title="Delete this saved view" aria-label="Delete view">
                <Trash2 size={13} />
              </button>
            )}
            <select value={filters.channel} onChange={(e) => changeFilters({ channel: e.target.value })} style={sel} aria-label="Channel">
              <option value="all">All channels</option>
              {channels.map((c) => <option key={c} value={c}>{channelName(c)}</option>)}
            </select>
            <select value={filters.status} onChange={(e) => changeFilters({ status: e.target.value as ContentFilters["status"] })} style={sel} aria-label="Status">
              <option value="all">All statuses</option>
              {(Object.keys(CONTENT_STATUS_LABEL) as ContentStatus[]).map((s) => (
                <option key={s} value={s}>{CONTENT_STATUS_LABEL[s]}</option>
              ))}
            </select>
            {/* AA-760 — always there (disabled until goals exist) so the toolbar does not shift on load. */}
            <select value={filters.goal} onChange={(e) => changeFilters({ goal: e.target.value })} style={sel} disabled={goals.length === 0} aria-label="Goal">
              <option value="all">All goals</option>
              {goals.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
            <input
              value={filters.place}
              onChange={(e) => changeFilters({ place: e.target.value.slice(0, 100) })}
              placeholder="Place or topic"
              aria-label="Place or topic"
              style={{ ...sel, cursor: "text", width: isPhone ? undefined : 150 }}
            />
            {/* S251 D6: on a phone the date field takes its own line and "from – to" share the next one evenly (the
                "to" label and the second field used to wrap apart). On a desktop it stays one group. */}
            <div
              data-testid="my-content-date-range"
              style={isPhone
                ? { display: "flex", flexWrap: "wrap", gap: 8, flex: "1 1 100%", minWidth: 0 }
                : { display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "nowrap" }}
            >
              <select value={filters.dateField} onChange={(e) => changeFilters({ dateField: e.target.value as ContentFilters["dateField"] })} style={isPhone ? { ...sel, flex: "1 1 100%" } : sel} aria-label="Date">
                <option value="created">Created</option>
                <option value="published">Published</option>
              </select>
              <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "nowrap", minWidth: 0, ...(isPhone ? { flex: "1 1 100%" } : {}) }}>
                <input type="date" value={filters.from} max={filters.to || undefined} onChange={(e) => changeFilters({ from: e.target.value })} style={{ ...dateInput, ...(isPhone ? { flex: "1 1 0" } : {}) }} aria-label="From" />
                <span style={{ color: T.muted, fontSize: 12, flexShrink: 0 }}>to</span>
                <input type="date" value={filters.to} min={filters.from || undefined} onChange={(e) => changeFilters({ to: e.target.value })} style={{ ...dateInput, ...(isPhone ? { flex: "1 1 0" } : {}) }} aria-label="To" />
              </div>
            </div>
            {filtered && (
              <button style={linkBtn} onClick={() => { setFilters(NO_FILTERS); setViewId(""); }}>
                <X size={12} /> Clear filters
              </button>
            )}
          </div>
        }
        bulkActions={({ selectedRows, clearSelection }) => (
          <>
            <button style={bulkBtn} onClick={() => bulkExport(selectedRows)}>
              <Download size={13} /> Export selected
            </button>
            <button
              style={bulkBtn}
              onClick={() => { askBulkPublish(selectedRows); clearSelection(); }}
              title={connections.wordpress ? "Publish the selected blog posts to WordPress" : "Connect WordPress to publish"}
            >
              <Send size={13} /> Publish selected
            </button>
          </>
        )}
      />

      <Drawer
        open={!!openItem}
        onClose={() => setOpenPieceId(null)}
        width={720}
        title={
          openItem ? (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              {pieceTitle(openItem)}
              <Badge tone="neutral">{channelName(openItem.channel)}</Badge>
            </span>
          ) : (
            ""
          )
        }
      >
        {openItem && (
          <ReviewDrawer
            key={openItem.piece_id}
            item={openItem}
            saving={saveM.isPending}
            onSave={(pieceId, text) => saveM.mutateAsync({ pieceId, text }).then(() => undefined)}
            onExport={onExport}
            onCopyAttempt={() => toast.info("Use Export to download this content.")}
          />
        )}
      </Drawer>

      <ConfirmModal
        open={confirm?.kind === "publish"}
        onClose={() => !publishM.isPending && setConfirm(null)}
        onConfirm={() => confirm?.kind === "publish" && publishM.mutate(confirm.rows)}
        busy={publishM.isPending}
        title={confirm?.kind === "publish" && confirm.rows.length > 1 ? `Publish ${confirm.rows.length} posts?` : "Publish this piece?"}
        confirmLabel={publishM.isPending ? "Publishing…" : "Publish"}
        body={confirm?.kind === "publish" ? (
          <div style={{ fontSize: 13, color: T.body, lineHeight: 1.55 }}>
            {confirm.rows.length === 1
              ? <>&ldquo;{pieceTitle(confirm.rows[0])}&rdquo; goes live on your {confirm.rows[0].channel === "blog" ? "WordPress site" : "Facebook Page"}.</>
              : <>These {confirm.rows.length} blog posts go live on your WordPress site, one after the other.</>}
            {confirm.skipped > 0 && (
              <div style={{ marginTop: 8, color: T.muted }}>
                {confirm.skipped} other selected piece{confirm.skipped === 1 ? " is" : "s are"} left out: only ready blog posts
                that are not published yet go out together. Publish other channels one by one, or export them.
              </div>
            )}
          </div>
        ) : null}
      />

      <ConfirmModal
        open={confirm?.kind === "regenerate"}
        onClose={() => !regenerateM.isPending && setConfirm(null)}
        onConfirm={() => confirm?.kind === "regenerate" && regenerateM.mutate(confirm.row)}
        busy={regenerateM.isPending}
        title="Write this piece again?"
        confirmLabel={regenerateM.isPending ? "Starting…" : "Write again"}
        body={confirm?.kind === "regenerate" ? (
          <div style={{ fontSize: 13, color: T.body, lineHeight: 1.55 }}>
            A new version of &ldquo;{pieceTitle(confirm.row)}&rdquo; is written with the same angle and call to action. It
            replaces this one in My Content; your edits to this version are not carried over.
          </div>
        ) : null}
      />

      {/* StatusBadge "running" uses animation: spin */}
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
    </>
  );
}

function readUserViews(user: string): ContentView[] {
  return listViews<unknown>(VIEWS_TABLE, user).map((v) => ({
    id: `user:${v.id}`, name: v.name, builtIn: false, ...sanitizeView(v.state),
  }));
}

/** One row's actions. Every control stops the click so the row does not also open the drawer. */
function RowActions({
  item, menuOpen, onMenu, publishReason, onView, onExport, onPublish, onRegenerate,
}: {
  item: ReviewItem;
  menuOpen: boolean;
  onMenu: (open: boolean) => void;
  publishReason: string | null;
  onView: () => void;
  onExport: (format: "text" | "html", mode?: "document" | "fragment") => void;
  onPublish: () => void;
  onRegenerate: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onMenu(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [menuOpen, onMenu]);
  const canExport = exportFormat(item) !== null;
  const stop = (fn: () => void) => (e: React.MouseEvent) => { e.stopPropagation(); fn(); };
  const status = contentStatus(item);
  const regenReason = regenerateBlock(item);
  return (
    <div ref={ref} style={{ display: "inline-flex", gap: 4, alignItems: "center", position: "relative" }} onClick={(e) => e.stopPropagation()}>
      <button style={iconBtn} onClick={stop(onView)} title="View" aria-label="View"><Eye size={13} /></button>
      <button style={{ ...iconBtn, opacity: canExport ? 1 : 0.4 }} disabled={!canExport} onClick={stop(() => onMenu(!menuOpen))}
              title={canExport ? "Export" : "Nothing to export yet"} aria-label="Export" aria-haspopup="menu" aria-expanded={menuOpen}>
        <FileDown size={13} />
      </button>
      {status !== "published" && (
        <button style={{ ...iconBtn, opacity: publishReason ? 0.4 : 1 }} disabled={!!publishReason} onClick={stop(onPublish)}
                title={publishReason ?? "Publish"} aria-label="Publish">
          <Send size={13} />
        </button>
      )}
      <button style={{ ...iconBtn, opacity: regenReason ? 0.4 : 1 }} disabled={!!regenReason} onClick={stop(onRegenerate)}
              title={regenReason ?? "Write again"} aria-label="Write again">
        <RefreshCw size={13} />
      </button>
      {menuOpen && canExport && (
        <div role="menu" style={menuStyle}>
          {item.channel === "blog" && (
            <>
              <button role="menuitem" style={menuItem} onClick={stop(() => onExport("html", "document"))}>HTML page</button>
              <button role="menuitem" style={menuItem} onClick={stop(() => onExport("html", "fragment"))}>HTML fragment (paste into a CMS)</button>
            </>
          )}
          <button role="menuitem" style={menuItem} onClick={stop(() => onExport("text"))}>Plain text</button>
        </div>
      )}
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  maxWidth: "100%",
  minWidth: 0,
  padding: "7px 12px",
  background: T.card,
  border: `1px solid ${T.line}`,
  borderRadius: 8,
  color: T.body,
  fontSize: 13,
  fontFamily: sans,
  cursor: "pointer",
  outline: "none",
  boxSizing: "border-box",
};

const dateInput: React.CSSProperties = { ...selectStyle, cursor: "text", minWidth: 0 };

const iconBtn: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 28,
  height: 28,
  borderRadius: 8,
  border: `1px solid ${T.line}`,
  background: T.card,
  color: T.body,
  cursor: "pointer",
  padding: 0,
};

const linkBtn: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  background: "none",
  border: "none",
  // S251 D7: link text in the accent-deep token (5.2:1 on the cream page, 11:1 in dark); the accent itself is 2.3:1.
  color: T.goldDeep,
  fontSize: 12.5,
  fontWeight: 600,
  cursor: "pointer",
  fontFamily: sans,
  padding: "4px 2px",
};

const menuStyle: React.CSSProperties = {
  position: "absolute",
  top: 32,
  right: 0,
  zIndex: 20,
  minWidth: 220,
  background: T.card,
  border: `1px solid ${T.line}`,
  borderRadius: 10,
  boxShadow: "0 8px 24px rgba(0,0,0,0.12)",
  padding: 4,
  display: "flex",
  flexDirection: "column",
};

const menuItem: React.CSSProperties = {
  textAlign: "left",
  background: "none",
  border: "none",
  padding: "8px 10px",
  borderRadius: 6,
  color: T.body,
  fontSize: 12.5,
  cursor: "pointer",
  fontFamily: sans,
};

const bulkBtn: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "7px 12px",
  borderRadius: 999,
  border: `1px solid ${T.gold}`,
  background: T.goldTint,
  color: T.goldDeep,
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
  fontFamily: sans,
};

export function ReviewList() {
  return (
    <ToastProvider>
      <ReviewListInner />
    </ToastProvider>
  );
}
