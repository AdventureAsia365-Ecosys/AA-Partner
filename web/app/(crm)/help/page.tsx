// web/app/(crm)/help/page.tsx — Help & what's new. AA-962 (card CRM-31 "help links"; R43 in-app): the getting-started
// guides each onboarding step and connection links to (/help#<topic>). AA-970 (card CRM-37b; R42, R43): the What's
// new feed (lib/whatsNew.ts, EN/VI; preview entries only for an agency on preview), the Preview panel (an agency on
// preview sees it, others do not: lib/preview.ts, /me `preview`, settings key crm.release.preview), the help topics of
// the most-used CRM areas, and the links to the public data and sub-processor pages.
import Link from "next/link";
import { Panel, PageTitle } from "@/components/PageTitle";
import { Badge, K } from "@/app/_kit";
import { crmContext } from "@/lib/crmContext";
import { formatDate } from "@/lib/dates";
import { t, type MessageKey } from "@/lib/i18n";
import { previewFeatures, previewOn } from "@/lib/preview";
import { DATA_PATH, SUBPROCESSORS_PATH } from "@/lib/studio/legal";
import { entryHref, visibleEntries } from "@/lib/whatsNew";

const TOPICS = [
  "mailbox",
  "team",
  "form",
  "import",
  "duplicates",
  "payments",
  "templates",
  "privacy",
  "kickoff",
  "sms",
  "calendar",
  "esign",
] as const;

/** AA-970: the most-used CRM areas beyond getting started (lib/i18nHelp.ts). */
const USING_TOPICS = ["pipeline", "clients", "proposals", "inbox", "tasks"] as const;

const bodyStyle = { margin: 0, fontSize: 13.5, color: K.body, lineHeight: 1.55 } as const;
const linkStyle = { color: K.accentDeep } as const;

function Heading({ id, children }: { id: string; children: string }) {
  return (
    <h2 id={id} style={{ fontSize: 16, fontWeight: 600, color: K.ink, margin: "8px 0 0", scrollMarginTop: 72 }}>
      {children}
    </h2>
  );
}

export default async function HelpPage() {
  const ctx = await crmContext();
  if (ctx.apiDown) return null;
  const { lang, me, role, locale } = ctx;
  const entries = visibleEntries(me);
  const preview = previewOn(me, "help.preview-panel") ? previewFeatures(me) : [];
  return (
    <div data-testid="crm-help" style={{ display: "grid", gap: 14, maxWidth: 820 }}>
      <PageTitle title={t(lang, "onb.helpTitle")} subtitle={t(lang, "onb.helpSubtitle")} />
      <p style={{ margin: 0, fontSize: 13.5 }}>
        <Link href="/onboarding" style={linkStyle}>
          {t(lang, "onb.open")} →
        </Link>
      </p>

      {preview.length > 0 && (
        <section id="preview" style={{ scrollMarginTop: 72 }}>
          <Panel title={t(lang, "help.preview.title")} testId="help-preview">
            <p style={bodyStyle}>{t(lang, "help.preview.body")}</p>
            <ul style={{ margin: "8px 0 0", paddingLeft: 20, fontSize: 13.5, color: K.body, lineHeight: 1.55 }}>
              {preview.map(({ flag, feature }) => (
                <li key={flag} data-testid={`help-preview-${flag}`}>
                  {feature.label[lang]}
                </li>
              ))}
            </ul>
          </Panel>
        </section>
      )}

      <section id="whats-new" style={{ scrollMarginTop: 72 }}>
        <Panel title={t(lang, "help.whatsNew.title")} testId="help-whats-new">
          <p style={{ ...bodyStyle, color: K.muted }}>{t(lang, "help.whatsNew.subtitle")}</p>
          <ol style={{ listStyle: "none", margin: "10px 0 0", padding: 0, display: "grid", gap: 12 }}>
            {entries.map((e) => {
              const href = entryHref(e, role);
              const words = e.text[lang];
              return (
                <li key={e.id} id={`new-${e.id}`} data-testid={`whats-new-${e.id}`} style={{ borderTop: `1px solid ${K.line}`, paddingTop: 10 }}>
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 12, color: K.muted }}>{formatDate(e.date, locale)}</span>
                    {e.preview && (
                      <span data-testid="whats-new-preview-badge">
                        <Badge tone="info">{t(lang, "help.whatsNew.previewBadge")}</Badge>
                      </span>
                    )}
                  </div>
                  <p style={{ margin: "4px 0 2px", fontSize: 14, fontWeight: 600, color: K.ink }}>{words.title}</p>
                  <p style={bodyStyle}>
                    {words.body}
                    {href && (
                      <>
                        {" "}
                        <Link href={href} style={linkStyle}>
                          {t(lang, "help.whatsNew.open")} →
                        </Link>
                      </>
                    )}
                  </p>
                </li>
              );
            })}
          </ol>
        </Panel>
      </section>

      <Heading id="getting-started">{t(lang, "help.gettingStarted.title")}</Heading>
      {TOPICS.map((topic) => (
        <section key={topic} id={topic} style={{ scrollMarginTop: 72 }}>
          <Panel title={t(lang, `onb.h.${topic}.t` as MessageKey)} testId={`help-${topic}`}>
            <p style={bodyStyle}>{t(lang, `onb.h.${topic}.b` as MessageKey)}</p>
          </Panel>
        </section>
      ))}

      <Heading id="using">{t(lang, "help.using.title")}</Heading>
      {USING_TOPICS.map((topic) => (
        <section key={topic} id={`using-${topic}`} style={{ scrollMarginTop: 72 }}>
          <Panel title={t(lang, `help.u.${topic}.t` as MessageKey)} testId={`help-using-${topic}`}>
            <p style={bodyStyle}>{t(lang, `help.u.${topic}.b` as MessageKey)}</p>
          </Panel>
        </section>
      ))}

      <section id="data" style={{ scrollMarginTop: 72 }}>
        <Panel title={t(lang, "help.data.title")} testId="help-data">
          <p style={bodyStyle}>{t(lang, "help.data.body")}</p>
          <p style={{ margin: "8px 0 0", fontSize: 13.5, display: "flex", flexWrap: "wrap", gap: 16 }}>
            <a href={DATA_PATH} style={linkStyle}>
              {t(lang, "help.data.page")}
            </a>
            <a href={SUBPROCESSORS_PATH} style={linkStyle}>
              {t(lang, "help.data.subprocessors")}
            </a>
            <Link href="/settings/data" style={linkStyle}>
              {t(lang, "help.data.settings")}
            </Link>
          </p>
        </Panel>
      </section>
    </div>
  );
}
