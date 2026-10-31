// web/app/(crm)/settings/page.tsx — Settings (A-18). AA-935: agency and role, language and dates (R32) with a
// live date reading; the other sections (team, channels, payments, forms, AI, data and privacy) arrive with their
// cards. AA-939: Team & roles (owner). AA-945: Mailbox and Saved replies.
import Link from "next/link";
import { DateInput } from "@/components/DateInput";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { Panel, PageTitle } from "@/components/PageTitle";
import { K } from "@/app/_kit";
import { crmContext } from "@/lib/crmContext";
import { t, type MessageKey } from "@/lib/i18n";

export default async function SettingsPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang, locale } = ctx;
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 820 }} data-testid="crm-settings">
      <PageTitle title={t(lang, "settings.title")} subtitle={t(lang, "settings.more")} />
      <Panel title={t(lang, "settings.agency")}>
        <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "6px 16px", margin: 0, fontSize: 13.5 }}>
          <dt style={{ color: K.muted }}>{t(lang, "settings.agency")}</dt>
          <dd style={{ margin: 0, color: K.ink, overflowWrap: "anywhere" }} data-testid="settings-agency-name">
            {ctx.me.display_name}
          </dd>
          <dt style={{ color: K.muted }}>{t(lang, "settings.role")}</dt>
          <dd style={{ margin: 0, color: K.ink }}>{t(lang, `role.${ctx.role}` as MessageKey)}</dd>
        </dl>
      </Panel>
      <Panel title={t(lang, "onb.settings.title")} testId="settings-onboarding">
        <p style={{ margin: "0 0 8px", fontSize: 13.5 }}>
          <Link href="/onboarding" data-testid="settings-onboarding-link">
            {t(lang, "onb.settings.checklist")}
          </Link>
        </p>
        <p style={{ margin: "0 0 8px", fontSize: 13.5 }}>
          <Link href="/settings/connections" data-testid="settings-connections-link">
            {t(lang, "onb.settings.connections")}
          </Link>
        </p>
        <p style={{ margin: 0, fontSize: 13.5 }}>
          {ctx.role === "owner" ? (
            <Link href="/settings/import" data-testid="settings-import-link">
              {t(lang, "onb.settings.import")}
            </Link>
          ) : (
            <span style={{ color: K.muted }}>{t(lang, "onb.imp.ownerOnly")}</span>
          )}
        </p>
      </Panel>
      <Panel title={t(lang, "team.title")} testId="settings-team">
        <a href="/settings/team" style={{ fontSize: 13.5 }} data-testid="settings-team-link">
          {t(lang, ctx.role === "owner" ? "settings.team" : "settings.teamView")}
        </a>
      </Panel>
      <Panel title={t(lang, "mail.title")} testId="settings-mailbox">
        <p style={{ margin: "0 0 8px", fontSize: 13 }}>
          <a href="/settings/mailbox" data-testid="settings-mailbox-link">
            {t(lang, "mail.link")}
          </a>
        </p>
        <a href="/settings/saved-replies" style={{ fontSize: 13.5 }} data-testid="settings-replies-link">
          {t(lang, "replies.link")}
        </a>
        <p style={{ margin: "8px 0 0", fontSize: 13.5 }}>
          <a href="/settings/templates" data-testid="settings-templates-link">
            {t(lang, "templates.link")}
          </a>
        </p>
      </Panel>
      <Panel title={t(lang, "forms.title")} testId="settings-forms">
        <a href="/settings/forms" style={{ fontSize: 13.5 }} data-testid="settings-forms-link">
          {t(lang, "settings.forms")}
        </a>
      </Panel>
      <Panel title={t(lang, "agreements.title")} testId="settings-agreements">
        <a href="/settings/agreements" style={{ fontSize: 13.5 }} data-testid="settings-agreements-link">
          {t(lang, "agreements.link")}
        </a>
      </Panel>
      <Panel title={t(lang, "payments.title")} testId="settings-payments">
        <a href="/settings/payments" style={{ fontSize: 13.5 }} data-testid="settings-payments-link">
          {t(lang, ctx.role === "owner" ? "payments.link" : "payments.ownerOnly")}
        </a>
      </Panel>
      <Panel title={t(lang, "settings.privacy")} testId="settings-privacy">
        <p style={{ margin: "0 0 8px", fontSize: 13 }}>
          <a href="/settings/data" data-testid="settings-data-link">
            {t(lang, "data.link")}
          </a>
        </p>
        <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>{t(lang, "settings.accessLogHelp")}</p>
        {ctx.role === "owner" ? (
          <a href="/settings/access-log" style={{ fontSize: 13.5 }} data-testid="settings-access-log-link">
            {t(lang, "settings.accessLog")}
          </a>
        ) : (
          <p style={{ margin: 0, fontSize: 13 }}>{t(lang, "settings.accessLogOwnerOnly")}</p>
        )}
      </Panel>
      <Panel title={t(lang, "settings.language")} testId="settings-language">
        <p style={{ margin: "0 0 12px", fontSize: 13, color: K.muted }}>{t(lang, "settings.languageHelp")}</p>
        <LocaleSwitch locale={locale} label={t(lang, "settings.language")} next="/settings" saveLabel={t(lang, "settings.save")} />
        <div style={{ marginTop: 18 }}>
          <DateInput
            locale={locale}
            name="sample_date"
            label={t(lang, "settings.tryDate")}
            help={t(lang, "settings.tryDateHelp")}
            readAs={t(lang, "settings.readAs")}
            invalid={t(lang, "settings.invalidDate")}
          />
        </div>
      </Panel>
    </div>
  );
}
