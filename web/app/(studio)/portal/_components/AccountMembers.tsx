"use client";
// web/app/(studio)/portal/_components/AccountMembers.tsx
// AA-978 follow-up — Account › Members: who belongs to this Content Studio workspace, read only, from the CIS
// membership directory (GET /api/tenant/v1/account/members → CIS /v1/account/members, CIS 276). The directory holds
// no name or e-mail, so a member shows as a short reference with role, status and dates. Changes go through the
// Adventure Asia team for now (invitations in the portal arrive with AA-979); CRM roles live in the Partner CRM.
// Kit: PageHeader, DataTable, Badge, StatusBadge; react-query key ["account-members"].

import { useQuery } from "@tanstack/react-query";
import { Mail } from "lucide-react";
import { useMemo } from "react";
import { apiGet, Badge, type ColumnDef, DataTable, PageHeader, SUPPORT_EMAIL, K, sans, useFormat, useLang, useT } from "@/app/_kit";
import { MEMBERS_MESSAGES } from "./account.messages";
import {
  memberLabel,
  missingOwner,
  roleHint,
  roleLabel,
  statusLabel,
  summaryLine,
  type AccountMember,
  type AccountMembers as MembersBody,
} from "@/lib/studio/members";

export const ACCOUNT_MEMBERS_QUERY_KEY = ["account-members"] as const;

export function AccountMembers() {
  const t = useT(MEMBERS_MESSAGES);
  const lang = useLang();
  const { formatDate } = useFormat();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ACCOUNT_MEMBERS_QUERY_KEY,
    queryFn: () => apiGet<MembersBody>("/api/tenant/v1/account/members"),
  });
  const rows = data?.data ?? [];

  const columns = useMemo<ColumnDef<AccountMember, unknown>[]>(
    () => [
      {
        id: "member",
        accessorFn: (m) => memberLabel(m, lang),
        header: t("colMember"),
        cell: (c) => (
          <span style={{ fontWeight: 600, color: K.ink, fontVariantNumeric: "tabular-nums" }}>
            {memberLabel(c.row.original, lang)}
          </span>
        ),
      },
      {
        id: "role",
        accessorFn: (m) => roleLabel(m.role, lang),
        header: t("colRole"),
        cell: (c) => {
          const r = c.row.original.role;
          return (
            <span title={roleHint(r, lang)}>
              <Badge tone={r === "owner" ? "accent" : "neutral"}>{roleLabel(r, lang)}</Badge>
            </span>
          );
        },
      },
      {
        id: "status",
        accessorFn: (m) => statusLabel(m.status, lang),
        header: t("colStatus"),
        cell: (c) => {
          const s = c.row.original.status;
          return <Badge tone={s === "active" ? "success" : "warning"} dot>{statusLabel(s, lang)}</Badge>;
        },
      },
      {
        id: "added",
        accessorFn: (m) => m.added_at,
        header: t("colAdded"),
        cell: (c) => <span style={{ color: K.muted, fontSize: 12, whiteSpace: "nowrap" }}>{formatDate(c.row.original.added_at)}</span>,
      },
      {
        id: "updated",
        accessorFn: (m) => m.updated_at,
        header: t("colChanged"),
        cell: (c) => <span style={{ color: K.muted, fontSize: 12, whiteSpace: "nowrap" }}>{formatDate(c.row.original.updated_at)}</span>,
      },
    ],
    [t, lang, formatDate],
  );

  const subject = t("mailSubject");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, fontFamily: sans }}>
      <PageHeader
        title={t("title")}
        description={
          <>
            {t("sub")}{" "}
            {data ? <strong style={{ color: K.ink }}>{summaryLine(data, lang)}.</strong> : null}
          </>
        }
        actions={
          <a
            href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`}
            data-testid="members-request-change"
            style={{
              display: "inline-flex", alignItems: "center", gap: 6, padding: "8px 14px", borderRadius: 999,
              border: `1px solid ${K.accent}`, color: K.accentDeep, fontSize: 12.5, fontWeight: 600, textDecoration: "none",
            }}
          >
            <Mail size={13} aria-hidden /> {t("requestChange")}
          </a>
        }
      />

      <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.55, maxWidth: 760 }}>
        {t("howItWorks")}
      </div>

      {data && missingOwner(rows) && (
        <div role="status" style={{
          padding: "10px 14px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.accentTint,
          color: K.ink, fontSize: 13,
        }}>
          {t("noOwner")}
        </div>
      )}

      <DataTable<AccountMember>
        data={rows}
        columns={columns}
        tableId="account-members"
        getRowId={(m) => m.membership_id}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : t("loadError")) : null}
        onRetry={() => refetch()}
        searchable={false}
        enableCsv={false}
        pageSize={25}
        emptyTitle={t("emptyTitle")}
        emptyDescription={t("emptySub")}
      />
    </div>
  );
}
