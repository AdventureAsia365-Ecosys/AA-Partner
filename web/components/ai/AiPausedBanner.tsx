// web/components/ai/AiPausedBanner.tsx — AA-1036 (R3, S265 follow-up): the amber banner on every CRM page while Adventure
// Asia has switched the CRM's AI off for the whole app (crm.llm_app_profile.ai_enabled = false, set from the CIS admin's
// App profiles; the API answers it as /crm/v1/me ai_paused). The gateway then refuses every AI call (app_disabled) and
// drafts degrade to "reply by hand", so the agency is told once, on every page, instead of per failed draft. Not
// dismissible: it goes when AA switches AI on again. Kit tokens only, like the mailbox and support banners.
import { K } from "@/app/_kit";
import { t, type Lang } from "@/lib/i18n";

export function AiPausedBanner({ lang }: { lang: Lang }) {
  return (
    <div
      role="status"
      data-testid="ai-paused-banner"
      style={{
        display: "flex",
        gap: 10,
        alignItems: "center",
        flexWrap: "wrap",
        padding: "10px 14px",
        marginBottom: 12,
        borderRadius: 10,
        background: K.amberSoft,
        border: `1px solid ${K.amber}`,
        color: K.ink,
        fontSize: 13,
      }}
    >
      <span>{t(lang, "ai.pausedBanner")}</span>
    </div>
  );
}
