"use client";
// web/app/_kit/local/StaffShell.tsx
// AA-1019 — the AA staff console's frame (S-02, S-04): a top bar (title, the environment's name on every non-prod
// deployment, the sections the staff member's groups open, language, theme, sign-out of the staff console) over the kit PageFrame. Deliberately not the agency CrmShell:
// no agency, no agency sidebar, no Content studio link, and its sign-out posts to /staff/auth/sign-out (the agency
// session on the same host is left alone). Every value comes from the server; the shell holds no state but the theme.

import { LogOut, Moon, Sun, SunMoon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Badge, IconButton, K, PageFrame, ToastProvider, useThemeChoice, type ThemeChoice } from "@/app/_kit";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import type { Environment } from "@/lib/config";
import type { DateLocale } from "@/lib/dates";
import type { MessageKey } from "@/lib/i18n";
import { envBadgeKey } from "@/lib/staffConsole";
import type { StaffGroup } from "@/lib/staffSession";

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

const SECTIONS: { href: string; key: MessageKey; group: StaffGroup | null }[] = [
  { href: "/staff/ops", key: "staff.nav.ops", group: "crm-ops" },
  { href: "/staff/support", key: "staff.nav.support", group: null },
];

export function StaffShell({
  dict,
  groups,
  locale,
  environment,
  children,
}: {
  dict: Dict;
  groups: StaffGroup[];
  locale: DateLocale;
  /** The deployment's environment (WebConfig.environment): every one but prod shows its name as a badge. */
  environment: Environment;
  children: React.ReactNode;
}) {
  const pathname = usePathname() ?? "/staff";
  const sections = SECTIONS.filter((s) => s.group === null || groups.includes(s.group));
  const envKey = envBadgeKey(environment);
  return (
    <ToastProvider>
      <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", background: K.bg }} data-testid="staff-shell">
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
            <span style={{ fontFamily: "'Fahkwang', 'Poppins', Georgia, serif", fontSize: 17, color: K.accent }}>
              {dict["staff.title"]}
            </span>
            {envKey && (
              <span data-testid="staff-env-badge">
                <Badge tone="info">{dict[envKey]}</Badge>
              </span>
            )}
          </div>
          <nav aria-label={dict["staff.title"]} style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {sections.map((s) => {
              const active = pathname === s.href || pathname.startsWith(`${s.href}/`);
              return (
                <Link
                  key={s.href}
                  href={s.href}
                  aria-current={active ? "page" : undefined}
                  style={{
                    padding: "6px 10px",
                    borderRadius: 8,
                    fontSize: 13,
                    textDecoration: "none",
                    color: active ? K.accentDeep : K.body,
                    background: active ? K.accentSoft : "transparent",
                    fontWeight: active ? 600 : 400,
                  }}
                >
                  {dict[s.key]}
                </Link>
              );
            })}
          </nav>
          <span style={{ flex: 1 }} />
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <LocaleSwitch locale={locale} label={dict["shell.language"]} next={pathname} compact />
            <ThemeButton label={dict["shell.theme"]} />
            <form action="/staff/auth/sign-out" method="post">
              <IconButton type="submit" label={dict["staff.signOut"]} title={dict["staff.signOut"]} testId="staff-sign-out">
                <LogOut size={16} />
              </IconButton>
            </form>
          </div>
        </header>
        <PageFrame width="wide" style={{ padding: "20px 16px 48px" }}>
          {children}
        </PageFrame>
      </div>
    </ToastProvider>
  );
}
