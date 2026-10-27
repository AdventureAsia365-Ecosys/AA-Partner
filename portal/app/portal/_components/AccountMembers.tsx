"use client";
// portal/app/portal/_components/AccountMembers.tsx
// AA-978 follow-up — Account › Members: who belongs to this Content Studio workspace, read only, from the CIS
// membership directory (GET /api/tenant/v1/account/members → CIS /v1/account/members, CIS 276). The directory holds
// no name or e-mail, so a member shows as a short reference with role, status and dates. Changes go through the
// Adventure Asia team for now (invitations in the portal arrive with AA-979); CRM roles live in the Partner CRM.
// Kit: PageHeader, DataTable, Badge, StatusBadge; react-query key ["account-members"].

import { useQuery } from "@tanstack/react-query";
import { Mail } from "lucide-react";
import { useMemo } from "react";
import { apiGet, Badge, type ColumnDef, DataTable, formatDate, PageHeader, SUPPORT_EMAIL } from "@/app/_kit";
import {
  memberLabel,
  missingOwner,
  ROLE_HINT,
  ROLE_LABEL,
  STATUS_LABEL,
  summaryLine,
  type AccountMember,
  type AccountMembers as MembersBody,
} from "@/lib/members";
import { T, sans } from "./ui";

export const ACCOUNT_MEMBERS_QUERY_KEY = ["account-members"] as const;

export function AccountMembers() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ACCOUNT_MEMBERS_QUERY_KEY,
    queryFn: () => apiGet<MembersBody>("/api/tenant/v1/account/members"),
  });
  const rows = data?.data ?? [];

  const columns = useMemo<ColumnDef<AccountMember, unknown>[]>(
    () => [
      {
        id: "member",
        accessorFn: (m) => memberLabel(m),
        header: "Member",
        cell: (c) => (
          <span style={{ fontWeight: 600, color: T.ink, fontVariantNumeric: "tabular-nums" }}>
            {memberLabel(c.row.original)}
          </span>
        ),
      },
      {
        id: "role",
        accessorFn: (m) => ROLE_LABEL[m.role] ?? m.role,
        header: "Role",
        cell: (c) => {
          const r = c.row.original.role;
          return (
            <span title={ROLE_HINT[r]}>
              <Badge tone={r === "owner" ? "accent" : "neutral"}>{ROLE_LABEL[r] ?? r}</Badge>
            </span>
          );
        },
      },
      {
        id: "status",
        accessorFn: (m) => STATUS_LABEL[m.status] ?? m.status,
        header: "Status",
        cell: (c) => {
          const s = c.row.original.status;
          return <Badge tone={s === "active" ? "success" : "warning"} dot>{STATUS_LABEL[s] ?? s}</Badge>;
        },
      },
      {
        id: "added",
        accessorFn: (m) => m.added_at,
        header: "Added",
        cell: (c) => <span style={{ color: T.muted, fontSize: 12, whiteSpace: "nowrap" }}>{formatDate(c.row.original.added_at)}</span>,
      },
      {
        id: "updated",
        accessorFn: (m) => m.updated_at,
        header: "Last change",
        cell: (c) => <span style={{ color: T.muted, fontSize: 12, whiteSpace: "nowrap" }}>{formatDate(c.row.original.updated_at)}</span>,
      },
    ],
    [],
  );

  const subject = "Content Studio members: add, change or remove";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, fontFamily: sans }}>
      <PageHeader
        title="Members"
        description={
          <>
            Everyone in your Content Studio workspace and their role.{" "}
            {data ? <strong style={{ color: T.ink }}>{summaryLine(data)}.</strong> : null}
          </>
        }
        actions={
          <a
            href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`}
            data-testid="members-request-change"
            style={{
              display: "inline-flex", alignItems: "center", gap: 6, padding: "8px 14px", borderRadius: 999,
              border: `1px solid ${T.gold}`, color: T.goldDeep, fontSize: 12.5, fontWeight: 600, textDecoration: "none",
            }}
          >
            <Mail size={13} aria-hidden /> Request a change
          </a>
        }
      />

      <div style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.55, maxWidth: 760 }}>
        Adventure Asia adds, changes and removes members for you for now: use Request a change. A member who is removed
        is also signed out of the Partner CRM within a minute. Roles inside the Partner CRM are managed in the CRM.
      </div>

      {data && missingOwner(rows) && (
        <div role="status" style={{
          padding: "10px 14px", borderRadius: 10, border: `1px solid ${T.line}`, background: T.goldTint,
          color: T.ink, fontSize: 13,
        }}>
          Your workspace has no active owner. Ask Adventure Asia to restore one before you make account changes.
        </div>
      )}

      <DataTable<AccountMember>
        data={rows}
        columns={columns}
        tableId="account-members"
        getRowId={(m) => m.membership_id}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : "Could not load the members") : null}
        onRetry={() => refetch()}
        searchable={false}
        enableCsv={false}
        pageSize={25}
        emptyTitle="No members yet"
        emptyDescription="Members appear here once Adventure Asia adds your team to the workspace."
      />
    </div>
  );
}
