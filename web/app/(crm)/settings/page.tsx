// web/app/(crm)/settings/page.tsx — Settings (A-18). AA-935: agency and role, language and dates (R32) with a
// live date reading; the other sections (team, channels, payments, forms, AI, data and privacy) arrive with their
// cards.
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
    <div style={{ display: "grid", gap: 16, maxWidth: 820 }} data-testid="crm-settings" data-card="AA-935">
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
      <Panel title={t(lang, "forms.title")} testId="settings-forms">
        <a href="/settings/forms" style={{ fontSize: 13.5 }} data-testid="settings-forms-link">
          {t(lang, "settings.forms")}
        </a>
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
