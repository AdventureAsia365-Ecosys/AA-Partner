"use client";
// web/app/_kit/local/AcademyShell.tsx
// AA-1105 — the AA Academy's frame on the academy host: a top bar (title, the environment's name on every non-prod
// deployment, Courses / Review / Certificates, the learner's name, language, theme, sign-out of the academy) over the
// kit PageFrame. Deliberately not the agency CrmShell or the staff console: no agency, no CRM link, and its sign-out
// posts to /academy/auth/sign-out. Every value comes from the server; the shell holds no state but the theme.

import { GraduationCap, LogOut, Moon, Sun, SunMoon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Badge, IconButton, K, PageFrame, ToastProvider, useThemeChoice, type ThemeChoice } from "@/app/_kit";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import type { Environment } from "@/lib/config";
import type { DateLocale } from "@/lib/dates";
import type { MessageKey } from "@/lib/i18n";
import { envBadgeKey } from "@/lib/staffConsole";

type Dict = Record<MessageKey, string>;

const THEME_NEXT: Record<ThemeChoice, ThemeChoice> = { system: "light", light: "dark", dark: "system" };

function ThemeButton({ label }: { label: string }) {
  const [choice, setChoice] = useThemeChoice();
  const Icon = choice === "light" ? Sun : choice === "dark" ? Moon : SunMoon;
  return (
    <IconButton label={`${label}: ${choice}`} onClick={() => setChoice(THEME_NEXT[choice])}>
      <Icon size={16} />
    </IconButton>
  );
}

export const ACADEMY_SECTIONS: { href: string; key: MessageKey }[] = [
  { href: "/academy", key: "academy.nav.courses" },
  { href: "/academy/review", key: "academy.nav.review" },
  { href: "/academy/certificates", key: "academy.nav.certificates" },
];

/** The section a path belongs to: Courses also covers modules, lessons and quizzes. */
export function activeSection(pathname: string): string {
  if (pathname.startsWith("/academy/review")) return "/academy/review";
  if (pathname.startsWith("/academy/certificates")) return "/academy/certificates";
  return "/academy";
}

export function AcademyShell({
  dict,
  locale,
  environment,
  learnerName,
  testLearner,
  children,
}: {
  dict: Dict;
  locale: DateLocale;
  environment: Environment;
  /** The learner's display name from the learning API (GET /learn/v1/me), or null. */
  learnerName: string | null;
  testLearner: boolean;
  children: React.ReactNode;
}) {
  const pathname = usePathname() ?? "/academy";
  const active = activeSection(pathname);
  const envKey = envBadgeKey(environment);
  return (
    <ToastProvider>
      <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", background: K.bg }} data-testid="academy-shell">
        <header
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            gap: "8px 16px",
            padding: "10px 16px",
            borderBottom: `1px solid ${K.line}`,
            background: K.card,
            position: "sticky",
            top: 0,
            zIndex: 10,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            <GraduationCap size={18} color={K.accent} aria-hidden />
            <span style={{ fontFamily: "'Fahkwang', 'Poppins', Georgia, serif", fontSize: 17, color: K.accent }}>{dict["academy.title"]}</span>
            {envKey && (
              <span data-testid="academy-env-badge">
                <Badge tone="info">{dict[envKey]}</Badge>
              </span>
            )}
          </div>
          <nav aria-label={dict["academy.title"]} style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {ACADEMY_SECTIONS.map((s) => {
              const on = active === s.href;
              return (
                <Link
                  key={s.href}
                  href={s.href}
                  aria-current={on ? "page" : undefined}
                  style={{
                    padding: "6px 10px",
                    borderRadius: 8,
                    fontSize: 13,
                    textDecoration: "none",
                    color: on ? K.accentDeep : K.body,
                    background: on ? K.accentSoft : "transparent",
                    fontWeight: on ? 600 : 400,
                  }}
                >
                  {dict[s.key]}
                </Link>
              );
            })}
          </nav>
          <span style={{ flex: 1 }} />
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
            {learnerName && (
              <span style={{ fontSize: 12.5, color: K.muted, overflowWrap: "anywhere" }} data-testid="academy-learner">
                {dict["academy.learner"].replace("{name}", learnerName)}
              </span>
            )}
            <LocaleSwitch locale={locale} label={dict["shell.language"]} next={pathname} compact />
            <ThemeButton label={dict["shell.theme"]} />
            <form action="/academy/auth/sign-out" method="post">
              <IconButton type="submit" label={dict["academy.signOut"]} title={dict["academy.signOut"]} testId="academy-sign-out">
                <LogOut size={16} />
              </IconButton>
            </form>
          </div>
        </header>
        {testLearner && (
          <div style={{ padding: "6px 16px", fontSize: 12.5, color: K.muted, background: K.card, borderBottom: `1px solid ${K.line}` }} data-testid="academy-test-learner">
            {dict["academy.testLearner"]}
          </div>
        )}
        <PageFrame width="wide" style={{ padding: "20px 16px 48px" }}>
          {children}
        </PageFrame>
      </div>
    </ToastProvider>
  );
}
