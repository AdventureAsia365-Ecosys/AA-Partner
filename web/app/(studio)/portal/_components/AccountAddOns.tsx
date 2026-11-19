"use client";
// web/app/(studio)/portal/_components/AccountAddOns.tsx
// AA-1036 (R3, root ADR 0019 d7) — Account › Add-ons: the agency's add-ons (CRM, Trip planner) with their state, and the
// workspace owner's "Request" (replaces the mailto: link of the app switcher). Data: GET /api/tenant/v1/account/add-ons
// (CIS, through the studio BFF); the request: POST /api/tenant/v1/account/add-on-requests {product, note}. CIS lets only
// the owner send it (an editor or viewer gets 403), so the form shows only for the owner (useStudioRole) and the others
// read why. Adventure Asia staff approve or decline in the CIS admin; a decline shows its reason here.
// Kit: PageHeader, Section, Badge, Button, Field, Textarea, ErrorState; react-query keys ["account-add-ons"] and
// ["studio-apps"] (the sidebar switcher, refreshed after a request; its crmMember says whether the person holds a CRM
// role: only then does an active CRM show "Open CRM", else "Ask your owner for CRM access"). An approval whose setup
// is still running or failed says so (CIS `setup`), never a bare "Approved".
// AA-1115: an active Trip planner whose plan includes the launcher (CIS `planner.launcher`, entitlement tp.launcher)
// shows the launcher snippet: a link or a button the agency pastes on its own site that opens its planner (its own
// planner domain first, else the hosted /{slug}/plan) in a new tab. No iframe, no script (lib/plannerLauncher.ts). No
// `planner` in the CIS answer (before CIS sends it) = no snippet.
// AA-1139: an active Trip planner links the owner and editors to Account › Trip planner (STUDIO_TRIP_PLANNER_PATH): the
// lead route, the webhook signing key, the look, the leads and the month's use, for an agency without the CRM too.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import {
  ApiError, Badge, Button, ErrorState, Field, Input, K, PageHeader, Section, Select, SkeletonBar, Textarea, apiGet, apiSend,
  mono, sans, useFormat, useLang, useT, type Translator,
} from "@/app/_kit";
import { useStudioRole } from "@/app/_kit/local/studio/useShellData";
import { STUDIO_APPS_KEY, fetchStudioApps } from "@/app/_kit/local/studio/studioApps";
import { CRM_HOME_PATH } from "@/lib/nav";
import { STUDIO_TRIP_PLANNER_PATH } from "@/lib/studio/tripPlanner";
import {
  LAUNCHER_LABEL_MAX, launcherLabel, launcherSnippet, launcherTargets, type LauncherStyle, type PlannerSite,
} from "@/lib/plannerLauncher";
import { ADDONS_MESSAGES } from "./account.messages";
import { usePortalShell } from "./PortalShellContext";
import { plainError } from "./studioError";

export const ACCOUNT_ADD_ONS_QUERY_KEY = ["account-add-ons"] as const;

type Product = "crm" | "tripplanner";
interface AddOnRequest {
  request_id: string; product: Product; status: "pending" | "approved" | "declined"; note: string | null;
  requested_at: string; decided_at: string | null; decision_note: string | null;
  /** An approval's setup (CIS): in_progress, done, failed (the add-on is not on yet); null / absent = not known. */
  setup?: "in_progress" | "done" | "failed" | null;
}
interface AddOn {
  product: Product; state: "active" | "requested" | "available"; subscription_status: string | null;
  request: AddOnRequest | null; last_decision: AddOnRequest | null;
  /** AA-1115: the tripplanner entry's site (CIS): its addresses and whether the plan includes the launcher. */
  planner?: PlannerSite | null;
}
interface AddOnsBody { in_service: boolean; add_ons: AddOn[] }

type T = Translator<typeof ADDONS_MESSAGES.en>;

/** The sentence for a refused request: CIS's stable code (ApiError.code, from X-Error-Code or the structured detail)
 *  where it matters (409 has two causes), else the status. */
export function refusalText(t: T, e: unknown): string {
  const status = e instanceof ApiError ? e.status : 0;
  const code = e instanceof ApiError ? e.code : "";
  if (status === 403) return t("errOwnerOnly");
  if (status === 409) {
    if (code === "tenant_not_in_service") return t("errNotInService");
    if (code === "already_subscribed") return t("errAlreadyOn");
    return t("errSend");
  }
  if (status === 429) return t("errTooMany");
  return t("errSend");
}

/** The sentence of the last decision: a decline, an approval being set up, an approval whose setup failed, or done. */
export function decisionText(t: T, last: AddOnRequest, formatDate: (iso: string) => string): string {
  if (last.status === "declined") return t("declined", formatDate(last.requested_at));
  const date = formatDate(last.decided_at ?? last.requested_at);
  if (last.setup === "failed") return t("approvedSetupFailed", date);
  if (last.setup === "in_progress") return t("approvedSettingUp", date);
  return t("approved", date);
}

function stateBadge(t: T, state: AddOn["state"]) {
  if (state === "active") return <Badge tone="success" dot>{t("stateActive")}</Badge>;
  if (state === "requested") return <Badge tone="warning" dot>{t("stateRequested")}</Badge>;
  return <Badge tone="neutral">{t("stateAvailable")}</Badge>;
}

const small = { margin: 0, fontSize: 12.5, color: K.muted, lineHeight: 1.55 } as const;

/** AA-1115: the launcher snippet of an active Trip planner (only when the plan includes it and CIS sent an address);
 *  nothing for any other add-on or state. */
export function PlannerLauncher({ addOn }: { addOn: Pick<AddOn, "product" | "state" | "planner"> }) {
  const t = useT(ADDONS_MESSAGES);
  const targets = launcherTargets(addOn.product === "tripplanner" && addOn.state === "active" ? addOn.planner : null);
  const [target, setTarget] = useState("");
  const [style, setStyle] = useState<LauncherStyle>("button");
  const [label, setLabel] = useState("");
  const [copied, setCopied] = useState<"" | "ok" | "failed">("");
  if (targets.length === 0) return null;
  const url = targets.includes(target) ? target : targets[0];
  const code = launcherSnippet(url, launcherLabel(label, t("launcherLabelDefault")), style) ?? "";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied("ok");
    } catch {
      setCopied("failed");
    }
  };
  return (
    <div data-testid="planner-launcher" style={{ display: "flex", flexDirection: "column", gap: 10, paddingTop: 12, borderTop: `1px solid ${K.line}` }}>
      <div>
        <p style={{ ...small, color: K.ink, fontWeight: 600 }}>{t("launcherTitle")}</p>
        <p style={small}>{t("launcherAbout")}</p>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        {targets.length > 1 && (
          <Field label={t("launcherAddress")}>
            <Select value={url} onChange={(e) => { setCopied(""); setTarget(e.target.value); }}
              options={targets.map((u) => ({ value: u, label: u }))} data-testid="planner-launcher-target" />
          </Field>
        )}
        <Field label={t("launcherStyle")}>
          <Select value={style} onChange={(e) => { setCopied(""); setStyle(e.target.value === "link" ? "link" : "button"); }}
            options={[{ value: "button", label: t("launcherStyleButton") }, { value: "link", label: t("launcherStyleLink") }]}
            data-testid="planner-launcher-style" />
        </Field>
        <Field label={t("launcherLabel")}>
          <Input value={label} maxLength={LAUNCHER_LABEL_MAX} placeholder={t("launcherLabelDefault")}
            onChange={(e) => { setCopied(""); setLabel(e.target.value); }} data-testid="planner-launcher-label" />
        </Field>
      </div>
      <Field label={t("launcherCode")}>
        <Textarea readOnly rows={3} value={code} data-testid="planner-launcher-code"
          style={{ fontFamily: mono, fontSize: 12.5 }} onFocus={(e) => e.currentTarget.select()} />
      </Field>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
        <span data-testid="planner-launcher-copy">
          <Button variant="secondary" onClick={() => void copy()}>{copied === "ok" ? t("launcherCopied") : t("launcherCopy")}</Button>
        </span>
        <a href={url} target="_blank" rel="noopener noreferrer" data-testid="planner-launcher-preview"
          style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
          {t("launcherPreview")} <ExternalLink size={12} aria-hidden />
        </a>
        {copied === "failed" && <span role="alert" style={{ fontSize: 12.5, color: K.danger }}>{t("launcherCopyFailed")}</span>}
      </div>
    </div>
  );
}

/** AA-1139: the link to Account › Trip planner (owner and editors) of an active Trip planner; nothing otherwise. */
export function PlannerManageLink({ addOn, role }: { addOn: Pick<AddOn, "product" | "state">; role: string | null }) {
  const t = useT(ADDONS_MESSAGES);
  if (addOn.product !== "tripplanner" || addOn.state !== "active" || (role !== "owner" && role !== "editor")) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <a href={STUDIO_TRIP_PLANNER_PATH} data-testid="add-on-manage-planner"
         style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
        {t("plannerManage")}
      </a>
      <p style={small}>{t("plannerManageAbout")}</p>
    </div>
  );
}

function AddOnCard({ addOn, isOwner, role, inService, crmMember }: {
  addOn: AddOn; isOwner: boolean; role: string | null; inService: boolean; crmMember: boolean;
}) {
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
            {addOn.product === "crm" && (crmMember ? (
              <a href={CRM_HOME_PATH} data-testid="add-on-open-crm" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
                {t("openCrm")} <ExternalLink size={12} aria-hidden />
              </a>
            ) : (
              <p style={{ ...small, color: K.ink }} data-testid="add-on-ask-crm-access">{t("askCrmAccess")}</p>
            ))}
          </div>
        )}
        <PlannerManageLink addOn={addOn} role={role} />
        <PlannerLauncher addOn={addOn} />
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
            <p style={{ ...small, color: K.ink }} data-testid={`add-on-decision-${addOn.product}`}>
              {decisionText(t, last, formatDate)}
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
  const apps = useQuery({ queryKey: STUDIO_APPS_KEY, queryFn: fetchStudioApps, staleTime: 5 * 60_000, retry: false });
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
        <AddOnCard key={a.product} addOn={a} isOwner={role === "owner"} role={role} inService={data.in_service}
          crmMember={apps.data?.crmMember === true} />
      ))}
    </div>
  );
}
