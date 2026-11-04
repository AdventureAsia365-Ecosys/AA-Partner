"use client";
// web/app/(studio)/portal/_components/PublishedList.tsx — AA-815
//
// The tenant's published posts (and ones they removed), with an Unpublish action. Unpublish is a
// confirm step, then DELETE /api/tenant/v1/publish-log/{publish_id}. The backend removes the post
// from the channel first (WordPress: moved to trash; Facebook: deleted) and only then marks the
// row unpublished; if the channel refuses, the post stays "Published" and the reason is shown.
//
// API: GET /api/tenant/v1/publish-log (tenant-safe rows: no errors/keys/actors).
// UI kit: DataTable + ConfirmModal + Toast + Badge/StatusBadge, react-query key ["publish-log"].

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Undo2 } from "lucide-react";
import { useMemo, useState } from "react";
import {
  AiNotice, ApiError, Badge, Button, ConfirmModal, type ColumnDef, DataTable, Section, StatusBadge, ToastProvider, apiGet,
  apiSend, useFormat, useLang, useT, useToast, K, sans,
} from "@/app/_kit";
import { PUBLISH_MESSAGES } from "./publish.messages";
import { ServedBadge, afterUnpublishSentence, type ServedFields } from "./ServedBadge";

export interface PublishedRow extends ServedFields {
  publish_id: string;
  piece_id: string;
  title: string;
  channel: string;
  status: "published" | "unpublished";
  external_url: string | null;
  published_at: string | null;
  unpublished_at: string | null;
  /** AA-986 L3 (ADR 0015 Q3): a post still live about a tour AA no longer serves. AA never removes it on its own;
   *  the tenant's one-click Unpublish below does (the real WordPress / Facebook removal). */
  live_post_of_discontinued_tour?: boolean;
}

export const PUBLISH_LOG_QUERY_KEY = ["publish-log"] as const;

const CHANNEL_NAME: Record<string, string> = { blog: "WordPress", facebook: "Facebook" };

export function channelName(channel: string): string {
  return CHANNEL_NAME[channel] ?? channel.charAt(0).toUpperCase() + channel.slice(1);
}

// AA-760 — the shared date format ("9 Oct 2026"), was the browser locale ("10/20/2026").
function PublishedListInner({ onUnpublished }: { onUnpublished?: () => void }) {
  const t = useT(PUBLISH_MESSAGES);
  const lang = useLang();
  const { formatDate: fmtDate } = useFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const [confirmRow, setConfirmRow] = useState<PublishedRow | null>(null);

  const { data: rows = [], isLoading, isError, error, refetch } = useQuery({
    queryKey: PUBLISH_LOG_QUERY_KEY,
    queryFn: () => apiGet<{ data?: PublishedRow[] }>("/api/tenant/v1/publish-log").then(d => d.data ?? []),
  });

  const unpublishM = useMutation({
    mutationFn: (row: PublishedRow) =>
      apiSend<{ status: string }>(`/api/tenant/v1/publish-log/${row.publish_id}`, { method: "DELETE" }),
    onSuccess: (_d, row) => {
      toast.success(t("removedFrom", channelName(row.channel)));
      setConfirmRow(null);
      qc.invalidateQueries({ queryKey: PUBLISH_LOG_QUERY_KEY });
      onUnpublished?.();
    },
    onError: (e, row) => {
      // 404 = already unpublished elsewhere (refresh shows it); anything else = still live.
      const msg = e instanceof Error ? e.message : t("unpublishFailed");
      toast.error(e instanceof ApiError && e.status === 404
        ? t("alreadyUnpublished")
        : t("stillPublished", channelName(row.channel), msg), 8000);
      setConfirmRow(null);
      qc.invalidateQueries({ queryKey: PUBLISH_LOG_QUERY_KEY });
    },
  });

  const columns = useMemo<ColumnDef<PublishedRow, unknown>[]>(
    () => [
      {
        accessorKey: "title",
        header: t("colTitle"),
        cell: (c) => {
          const r = c.row.original;
          return (
            <div>
              <span style={{ fontWeight: 600, color: K.ink }}>{r.title}</span>
              {r.served_notice && <div style={{ marginTop: 4 }}><ServedBadge fields={r} /></div>}
              {r.live_post_of_discontinued_tour && (
                <div style={{ marginTop: 4, fontSize: 11.5, color: K.danger, fontWeight: 600 }}>
                  {t("discontinued")}
                </div>
              )}
            </div>
          );
        },
      },
      {
        accessorKey: "channel",
        header: t("colChannel"),
        // AA-1040 — the "AI-assisted" label of every piece (kit AiNotice badge form), under the channel.
        cell: (c) => (
          <span style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-start", gap: 4 }}>
            <Badge tone="neutral">{channelName(c.row.original.channel)}</Badge>
            <AiNotice variant="publisher" form="badge" testId="published-ai-badge" />
          </span>
        ),
      },
      {
        accessorKey: "status",
        header: t("colStatus"),
        cell: (c) => c.row.original.status === "published"
          ? <StatusBadge status="approved" label={t("statusPublished")} />
          : <StatusBadge status="pending" label={t("statusUnpublished")} />,
      },
      {
        id: "date",
        accessorFn: (r) => new Date(r.unpublished_at ?? r.published_at ?? 0).getTime(),
        header: t("colDate"),
        cell: (c) => {
          const r = c.row.original;
          return (
            <span style={{ color: K.muted, fontSize: 12 }}>
              {r.status === "unpublished" ? t("removedOn", fmtDate(r.unpublished_at)) : fmtDate(r.published_at)}
            </span>
          );
        },
      },
      {
        id: "actions",
        header: "",
        enableSorting: false,
        cell: (c) => {
          const r = c.row.original;
          return (
            <div style={{ display: "flex", gap: 12, alignItems: "center", justifyContent: "flex-end" }}
              onClick={(e) => e.stopPropagation()}>
              {r.status === "published" && r.external_url && (
                <a href={r.external_url} target="_blank" rel="noreferrer" style={linkStyle}>
                  {t("view")} <ExternalLink size={11} />
                </a>
              )}
              {r.status === "published" && (
                <Button variant="danger" size="sm" onClick={() => setConfirmRow(r)} style={{ whiteSpace: "nowrap" }}>
                  <Undo2 size={12} /> {r.live_post_of_discontinued_tour ? t("removePost") : t("unpublish")}
                </Button>
              )}
            </div>
          );
        },
      },
    ],
    [t, fmtDate],
  );

  return (
    <Section title={t("publishedTitle")} bodyStyle={{ padding: "14px 20px 20px" }}>
      <DataTable<PublishedRow>
        data={rows}
        columns={columns}
        tableId="portal-published-posts"
        getRowId={(r) => r.publish_id}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : t("loadError")) : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder={t("searchTitle")}
        pageSize={10}
        tableMinWidth={560}
        emptyTitle={t("emptyTitle")}
        emptyDescription={t("emptySub")}
      />
      <ConfirmModal
        open={!!confirmRow}
        onClose={() => { if (!unpublishM.isPending) setConfirmRow(null); }}
        onConfirm={() => { if (confirmRow) unpublishM.mutate(confirmRow); }}
        destructive
        busy={unpublishM.isPending}
        title={t("confirmTitle")}
        confirmLabel={unpublishM.isPending ? t("removing") : t("unpublish")}
        body={confirmRow ? (
          <div style={{ fontSize: 13, lineHeight: 1.6, color: K.body }}>
            <p style={{ margin: "0 0 10px" }}>
              <strong>{confirmRow.title}</strong> {t("willBeRemoved", channelName(confirmRow.channel))}
              {confirmRow.channel === "blog" ? t("wpTrash") : ""}.
            </p>
            <p style={{ margin: 0, color: K.muted }}>
              {afterUnpublishSentence(confirmRow, lang)}
            </p>
          </div>
        ) : null}
      />
    </Section>
  );
}

const linkStyle: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, fontWeight: 600,
  color: K.ink3, textDecoration: "none", fontFamily: sans, whiteSpace: "nowrap",
};

export function PublishedList({ onUnpublished }: { onUnpublished?: () => void }) {
  return (
    <ToastProvider>
      <PublishedListInner onUnpublished={onUnpublished} />
    </ToastProvider>
  );
}
