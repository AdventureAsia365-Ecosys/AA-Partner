"use client";
// web/app/(studio)/portal/_components/PutBackList.tsx — AA-1039 (option A)
//
// Publish page: "Pages to put back online". The WordPress pages Adventure Asia moved to draft while their tour was off
// sale, now that the tour is back on sale; each row has one "Put back online" button (putBackApi.ts). Nothing is
// published until the tenant clicks. The section is not shown when the list is empty or cannot be read (a staff
// view-as session is refused this read): the Published list below stays the page's record.
//
// API: GET /api/tenant/v1/publish-log/put-back, POST /api/tenant/v1/publish-log/{publish_id}/put-back.
// UI kit: Section + Button + Badge + Toast; react-query key ["publish-log", "put-back"].

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, UploadCloud } from "lucide-react";
import { Badge, Button, Section, ToastProvider, useLang, useT, useToast, K, sans } from "@/app/_kit";
import { PUT_BACK_MESSAGES } from "./putBack.messages";
import { PUT_BACK_ANCHOR, PUT_BACK_QUERY_KEY, isOnline, listPutBack, outcomeText, putBack, type PutBackPage } from "./putBackApi";
import { plainError } from "./studioError";

function PutBackListInner({ onPutBack }: { onPutBack?: () => void }) {
  const t = useT(PUT_BACK_MESSAGES);
  const lang = useLang();
  const toast = useToast();
  const qc = useQueryClient();

  const { data: pages = [], isError } = useQuery({ queryKey: PUT_BACK_QUERY_KEY, queryFn: listPutBack, retry: false });

  const putBackM = useMutation({
    mutationFn: (page: PutBackPage) => putBack(page.publish_id),
    onSuccess: (r) => {
      if (isOnline(r)) toast.success(outcomeText(t, r));
      else toast.info(outcomeText(t, r), 8000);
      qc.invalidateQueries({ queryKey: ["publish-log"] });
      onPutBack?.();
    },
    onError: (e) => {
      toast.error(plainError(e, t("failed"), lang), 8000);
      qc.invalidateQueries({ queryKey: PUT_BACK_QUERY_KEY });
    },
  });

  if (isError || pages.length === 0) return null;

  return (
    <div id={PUT_BACK_ANCHOR}>
    <Section title={t("title")} description={t("sub")} testId="put-back-list" bodyStyle={{ padding: "6px 20px 14px" }}>
      {pages.map((p) => {
        const busy = putBackM.isPending && putBackM.variables?.publish_id === p.publish_id;
        return (
          <div key={p.publish_id} data-testid={`put-back-${p.publish_id}`} style={{
            display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", justifyContent: "space-between",
            padding: "10px 0", borderBottom: `1px solid ${K.line2}`, fontFamily: sans,
          }}>
            <div style={{ minWidth: 0, flex: "1 1 260px" }}>
              <div style={{ fontWeight: 600, color: K.ink, fontSize: 13 }}>{p.title}</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4, flexWrap: "wrap" }}>
                <Badge tone="neutral">WordPress</Badge>
                {p.tour_name && <span style={{ fontSize: 12, color: K.muted }}>{t("tourLine", p.tour_name)}</span>}
              </div>
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
              {p.external_url && (
                <a href={p.external_url} target="_blank" rel="noreferrer" style={{
                  display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, fontWeight: 600, color: K.ink3,
                  textDecoration: "none", whiteSpace: "nowrap",
                }}>
                  {t("viewPage")} <ExternalLink size={11} />
                </a>
              )}
              <Button size="sm" variant="primary" disabled={putBackM.isPending} onClick={() => putBackM.mutate(p)}
                testId={`put-back-button-${p.publish_id}`} style={{ whiteSpace: "nowrap" }}>
                <UploadCloud size={12} /> {busy ? t("puttingBack") : t("putBack")}
              </Button>
            </div>
          </div>
        );
      })}
    </Section>
    </div>
  );
}

export function PutBackList({ onPutBack }: { onPutBack?: () => void }) {
  return (
    <ToastProvider>
      <PutBackListInner onPutBack={onPutBack} />
    </ToastProvider>
  );
}
