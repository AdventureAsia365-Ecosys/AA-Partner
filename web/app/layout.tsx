// web/app/layout.tsx
// AA-935 — root layout of the CRM web. Same brand fonts as the CIS admin and portal (Fahkwang display, Poppins body,
// JetBrains Mono for ids) and the same theme restore script as the portal: the kit core's, bound to `cis_theme` on this
// origin by _kit/local/theme.ts (light | dark | absent = system), so there is no flash of the wrong theme. <html lang>
// follows the resolved locale. S262 (AA-957): <html> carries no data-theme from the server (the restore script is
// the only writer before paint), and _kit/local/ThemeKeeper applies the stored choice again whenever React rebuilds
// the document (a failed hydration re-renders the root on the client and drops the attributes the script set).
import type { Metadata, Viewport } from "next";
// AA-994 S2a: the `--aa-*` palette is the vendored kit core's tokens.css (no PostCSS in this app, so it is imported
// here, before globals.css, rather than by a CSS @import).
import "./_kit/core/tokens.css";
import "./globals.css";
import { serverContext } from "@/lib/serverContext";
import { themeStore } from "./_kit/local/theme";
import { ThemeKeeper } from "./_kit/local/ThemeKeeper";

export const metadata: Metadata = {
  title: "Adventure Asia Partner",
  description: "Adventure Asia partner CRM",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const { lang } = await serverContext();
  return (
    <html lang={lang} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeStore.restoreScript }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fahkwang:wght@500;600;700&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <ThemeKeeper />
        {children}
      </body>
    </html>
  );
}
