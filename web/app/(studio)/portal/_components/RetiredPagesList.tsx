"use client";
// web/app/(studio)/portal/_components/RetiredPagesList.tsx — AA-1039 follow-up (tech lead decision S276)
//
// Publish page: "Posts about tours no longer offered". When Adventure Asia stops offering a tour, the agency's live
// posts about it stay online (nothing changes on its site by default); this section lists them. A WordPress page has
// one "Move to draft" button (retiredPagesApi.ts, one CIS call, one WordPress call); a Facebook post says to edit or
// remove it on Facebook. A page of an agency that turned automatic drafts on (Settings) shows its draft date. The
// section is not shown when the list is empty or cannot be read (a staff view-as session is refused this read).
//
// API: GET /api/tenant/v1/publish-log/retired-pages, POST /api/tenant/v1/publish-log/{publish_id}/move-to-draft.
// UI kit: Section + Button + Badge + Toast; react-query key ["publish-log", "retired-pages"].

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, FileDown } from "lucide-react";
import { Badge, Button, Section, ToastProvider, useLang, useT, useToast, K, sans } from "@/app/_kit";
import { fmtDate } from "@/lib/studio/format";
import { RETIRED_PAGES_MESSAGES } from "./retiredPages.messages";
import {
  RETIRED_PAGES_ANCHOR, RETIRED_PAGES_QUERY_KEY, listRetiredPages, moveOutcomeText, moveToDraft, type RetiredPage,
} from "./retiredPagesApi";
import { plainError } from "./studioError";

function RetiredPagesListInner({ onMoved }: { onMoved?: () => void }) {
  const t = useT(RETIRED_PAGES_MESSAGES);
  const lang = useLang();
  const toast = useToast();
  const qc = useQueryClient();

  const { data: pages = [], isError } = useQuery({
    queryKey: RETIRED_PAGES_QUERY_KEY, queryFn: listRetiredPages, retry: false,
  });

  const moveM = useMutation({
    mutationFn: (page: RetiredPage) => moveToDraft(page.publish_id),
    onSuccess: (r) => {
      toast.success(moveOutcomeText(t, r));
      qc.invalidateQueries({ queryKey: ["publish-log"] });
      onMoved?.();
    },
    onError: (e) => {
      toast.error(plainError(e, t("failed"), lang), 8000);
      qc.invalidateQueries({ queryKey: RETIRED_PAGES_QUERY_KEY });
    },
  });

  if (isError || pages.length === 0) return null;

  return (
    <div id={RETIRED_PAGES_ANCHOR}>
    <Section title={t("title")} description={t("sub")} testId="retired-pages-list" bodyStyle={{ padding: "6px 20px 14px" }}>
      {pages.map((p) => {
        const busy = moveM.isPending && moveM.variables?.publish_id === p.publish_id;
        return (
          <div key={p.publish_id} data-testid={`retired-page-${p.publish_id}`} style={{
            display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", justifyContent: "space-between",
            padding: "10px 0", borderBottom: `1px solid ${K.line2}`, fontFamily: sans,
          }}>
            <div style={{ minWidth: 0, flex: "1 1 260px" }}>
              <div style={{ fontWeight: 600, color: K.ink, fontSize: 13 }}>{p.title}</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4, flexWrap: "wrap" }}>
                <Badge tone="neutral">{p.can_move_to_draft ? "WordPress" : "Facebook"}</Badge>
                {p.tour_name && <span style={{ fontSize: 12, color: K.muted }}>{t("tourLine", p.tour_name)}</span>}
              </div>
              {p.draft_due_at && (
                <div data-testid={`retired-page-due-${p.publish_id}`} style={{ fontSize: 12, color: K.muted, marginTop: 4 }}>
                  {t("draftOn", fmtDate(p.draft_due_at, lang))}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
              {p.external_url && (
                <a href={p.external_url} target="_blank" rel="noreferrer" style={{
                  display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, fontWeight: 600, color: K.ink3,
                  textDecoration: "none", whiteSpace: "nowrap",
                }}>
                  {t("viewPage")} <ExternalLink size={11} />
                </a>
              )}
              {p.can_move_to_draft ? (
                <Button size="sm" variant="secondary" disabled={moveM.isPending} onClick={() => moveM.mutate(p)}
                  testId={`move-to-draft-button-${p.publish_id}`} style={{ whiteSpace: "nowrap" }}>
                  <FileDown size={12} /> {busy ? t("moving") : t("moveToDraft")}
                </Button>
              ) : (
                <span style={{ fontSize: 12, color: K.muted }}>{t("facebookNote")}</span>
              )}
            </div>
          </div>
        );
      })}
    </Section>
    </div>
  );
}

export function RetiredPagesList({ onMoved }: { onMoved?: () => void }) {
  return (
    <ToastProvider>
      <RetiredPagesListInner onMoved={onMoved} />
    </ToastProvider>
  );
}
