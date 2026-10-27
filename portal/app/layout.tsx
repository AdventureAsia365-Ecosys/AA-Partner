// portal/app/layout.tsx
// AA-885 — root layout of the tenant portal app. Same brand fonts as the admin (AA-605: Fahkwang
// display, Poppins body, JetBrains Mono for IDs) and the same react-query provider (AA-662).
// AA-759 — the portal is no longer pinned light: the same two restore scripts as the admin root layout
// run before paint (theme: localStorage `cis_theme` light | dark | absent = system; sidebar rail:
// `cis_sidebar`), on the portal origin's own localStorage. `suppressHydrationWarning` covers only the
// <html> attributes those scripts set before React hydrates.
import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./_kit/core/Providers";
import { themeStore } from "./_kit/local/theme";

export const metadata: Metadata = {
  title: "Adventure Asia — Partner Portal",
  description: "Adventure Asia B2B partner portal",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light" data-sidebar="expanded" suppressHydrationWarning>
      <head>
        {/* AA-994 S2a: the theme half is the kit core's restore script for this app's key (_kit/local/theme.ts),
            as in CIS admin; the sidebar half stays here (`cis_sidebar`, matching core/sidebarStore.ts). */}
        <script dangerouslySetInnerHTML={{ __html: themeStore.restoreScript }} />
        <script dangerouslySetInnerHTML={{ __html: `
          (function() {
            try {
              var s = localStorage.getItem('cis_sidebar');
              document.documentElement.setAttribute('data-sidebar', s === 'collapsed' ? 'collapsed' : 'expanded');
            } catch (e) {
              document.documentElement.setAttribute('data-sidebar', 'expanded');
            }
          })();
        `}} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fahkwang:wght@500;600;700&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body style={{ margin: 0 }}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
