// web/app/layout.tsx
// AA-935 — root layout of the CRM web. Same brand fonts as the CIS admin and portal (Fahkwang display, Poppins body,
// JetBrains Mono for ids) and the same theme restore script as the portal (localStorage `cis_theme` on this origin:
// light | dark | absent = system), so there is no flash of the wrong theme. <html lang> follows the resolved locale.
import type { Metadata, Viewport } from "next";
import "./globals.css";
import { serverContext } from "@/lib/serverContext";

export const metadata: Metadata = {
  title: "Adventure Asia Partner",
  description: "Adventure Asia partner CRM",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

const RESTORE_THEME = `
(function() {
  try {
    var c = localStorage.getItem('cis_theme');
    var r = (c === 'light' || c === 'dark') ? c
      : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', r);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();`;

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const { lang } = await serverContext();
  return (
    <html lang={lang} data-theme="light" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: RESTORE_THEME }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fahkwang:wght@500;600;700&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
