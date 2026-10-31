"use client";
// web/app/_kit/local/ThemeKeeper.tsx
// S262 (AA-957 live evidence): dark mode was lost on some trip pages: <html data-theme="dark"> at DOMContentLoaded (the
// restore script of ./theme.ts), "light" after hydration, no console error. The cause: when hydration of a page fails
// (any server/client text difference, e.g. a time printed in the server's zone), React 19 renders the whole document
// again on the client and, taking over <html>, drops every attribute the restore script set and writes the props of
// app/layout.tsx back: the stored dark choice was gone until the next full load.
//
// The layout no longer renders a data-theme of its own (the restore script alone sets it before paint), and this
// component, mounted at the root, applies the stored choice again in a layout effect: on the first commit and on any
// commit that rebuilds the root, before the browser paints. It renders nothing and sets no React state (the store
// writes the attribute), so the server and the client render the same thing.
import { useLayoutEffect } from "react";
import { themeStore } from "./theme";

export function ThemeKeeper() {
  useLayoutEffect(() => {
    try {
      themeStore.applyResolvedTheme(themeStore.readChoice());
    } catch {
      // Storage blocked (a private window): the restore script fell back to light already.
    }
  }, []);
  return null;
}
