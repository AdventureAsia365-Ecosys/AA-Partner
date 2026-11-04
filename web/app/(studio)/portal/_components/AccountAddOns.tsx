"use client";
// web/app/(studio)/portal/_components/AccountAddOns.tsx
// AA-1036 (R3, root ADR 0019 d7) — Account › Add-ons: the agency's add-ons (CRM, Trip planner) with their state, and the
// workspace owner's "Request" (replaces the mailto: link of the app switcher). Data: GET /api/tenant/v1/account/add-ons
// (CIS, through the studio BFF); the request: POST /api/tenant/v1/account/add-on-requests {product, note}. CIS lets only
// the owner send it (an editor or viewer gets 403), so the form shows only for the owner (useStudioRole) and the others
// read why. Adventure Asia staff approve or decline in the CIS admin; a decline shows its reason here.
// Kit: PageHeader, Section, Badge, Button, Field, Textarea, ErrorState; react-query keys ["account-add-ons"] and
// ["studio-apps"] (the sidebar switcher, refreshed after a request).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import {
  ApiError, Badge, Button, ErrorState, Field, K, PageHeader, Section, SkeletonBar, Textarea, apiGet, apiSend, sans, useFormat,
  useLang, useT, type Translator,
} from "@/app/_kit";
import { useStudioRole } from "@/app/_kit/local/studio/useShellData";
import { CRM_HOME_PATH } from "@/lib/nav";
import { ADDONS_MESSAGES } from "./account.messages";
import { usePortalShell } from "./PortalShellContext";
import { plainError } from "./studioError";

export const ACCOUNT_ADD_ONS_QUERY_KEY = ["account-add-ons"] as const;
const STUDIO_APPS_KEY = ["studio-apps"] as const;

type Product = "crm" | "tripplanner";
interface AddOnRequest {
  request_id: string; product: Product; status: "pending" | "approved" | "declined"; note: string | null;
  requested_at: string; decided_at: string | null; decision_note: string | null;
}
interface AddOn {
  product: Product; state: "active" | "requested" | "available"; subscription_status: string | null;
  request: AddOnRequest | null; last_decision: AddOnRequest | null;
}
interface AddOnsBody { in_service: boolean; add_ons: AddOn[] }

type T = Translator<typeof ADDONS_MESSAGES.en>;

/** The sentence for a refused request (CIS error codes; X-Error-Code is not readable here, the status is). */
export function refusalText(t: T, e: unknown): string {
  const status = e instanceof ApiError ? e.status : 0;
  if (status === 403) return t("errOwnerOnly");
  if (status === 409) return t("errAlreadyOn");
  if (status === 429) return t("errTooMany");
  return t("errSend");
}

function stateBadge(t: T, state: AddOn["state"]) {
  if (state === "active") return <Badge tone="success" dot>{t("stateActive")}</Badge>;
  if (state === "requested") return <Badge tone="warning" dot>{t("stateRequested")}</Badge>;
  return <Badge tone="neutral">{t("stateAvailable")}</Badge>;
}

const small = { margin: 0, fontSize: 12.5, color: K.muted, lineHeight: 1.55 } as const;

function AddOnCard({ addOn, isOwner, inService }: { addOn: AddOn; isOwner: boolean; inService: boolean }) {
  const t = useT(ADDONS_MESSAGES);
  const { formatDate } = useFormat();
  const { showToast } = usePortalShell();
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const name = addOn.product === "crm" ? t("crmName") : t("plannerName");
  const send = useMutation({
    mutationFn: () => apiSend("/api/tenant/v1/account/add-on-requests", {
      body: { product: addOn.product, note: note.trim() || null },
    }),
    onSuccess: () => {
      setNote("");
      showToast(t("sent", name));
      void qc.invalidateQueries({ queryKey: ACCOUNT_ADD_ONS_QUERY_KEY });
      void qc.invalidateQueries({ queryKey: STUDIO_APPS_KEY });
    },
    onError: (e) => setError(refusalText(t, e)),
  });
  const last = addOn.last_decision;
  return (
    <Section
      title={name}
      description={addOn.product === "crm" ? t("crmAbout") : t("plannerAbout")}
      actions={<span data-testid={`add-on-state-${addOn.product}`} data-state={addOn.state}>{stateBadge(t, addOn.state)}</span>}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }} data-testid={`add-on-${addOn.product}`}>
        {addOn.state === "active" && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
            <p style={small}>{addOn.product === "crm" ? t("activeNote") : t("plannerActiveNote")}</p>
            {addOn.product === "crm" && (
              <a href={CRM_HOME_PATH} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
                {t("openCrm")} <ExternalLink size={12} aria-hidden />
              </a>
            )}
          </div>
        )}
        {addOn.state === "requested" && addOn.request && (
          <>
            <p style={small}>{t("requestedNote", formatDate(addOn.request.requested_at))}</p>
            {addOn.request.note && (
              <Field label={t("yourNote")}>
                <p style={{ ...small, color: K.ink, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{addOn.request.note}</p>
              </Field>
            )}
          </>
        )}
        {last && addOn.state !== "requested" && (
          <div role="status" style={{ padding: "10px 14px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.accentTint }}>
            <p style={{ ...small, color: K.ink }}>
              {last.status === "declined" ? t("declined", formatDate(last.requested_at))
                : t("approved", formatDate(last.decided_at ?? last.requested_at))}
            </p>
            {last.decision_note && (
              <p style={{ ...small, marginTop: 4 }}>
                <strong style={{ color: K.ink }}>{t("reason")}:</strong> {last.decision_note}
              </p>
            )}
          </div>
        )}
        {addOn.state === "available" && inService && (isOwner ? (
          <>
            <Field label={t("noteLabel")}>
              <Textarea rows={2} maxLength={500} value={note} disabled={send.isPending}
                placeholder={t("notePlaceholder")} onChange={(e) => { setError(""); setNote(e.target.value); }} />
            </Field>
            <span data-testid={`add-on-request-${addOn.product}`}>
              <Button variant="primary" disabled={send.isPending} onClick={() => { setError(""); send.mutate(); }}>
                {send.isPending ? t("requesting") : t("request", name)}
              </Button>
            </span>
            {error && <span role="alert" style={{ fontSize: 12.5, color: K.danger }}>{error}</span>}
          </>
        ) : (
          <p style={small}>{t("ownerOnly")}</p>
        ))}
      </div>
    </Section>
  );
}

export function AccountAddOns() {
  const t = useT(ADDONS_MESSAGES);
  const lang = useLang();
  const role = useStudioRole();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ACCOUNT_ADD_ONS_QUERY_KEY,
    queryFn: () => apiGet<AddOnsBody>("/api/tenant/v1/account/add-ons"),
  });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, fontFamily: sans }} data-testid="account-add-ons">
      <PageHeader title={t("title")} description={t("sub")} />
      {isError && <ErrorState message={plainError(error, t("loadError"), lang)} onRetry={() => void refetch()} />}
      {isLoading && <SkeletonBar h={120} r={12} />}
      {data && !data.in_service && (
        <div role="status" style={{ padding: "10px 14px", borderRadius: 10, border: `1px solid ${K.line}`, background: K.accentTint, color: K.ink, fontSize: 13 }}>
          {t("notInService")}
        </div>
      )}
      {data && data.add_ons.map((a) => (
        <AddOnCard key={a.product} addOn={a} isOwner={role === "owner"} inService={data.in_service} />
      ))}
    </div>
  );
}
