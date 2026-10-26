"use client";
// portal/app/portal/_shell/SessionWatch.tsx
// AA-979 P2b (critic S1) — an exchanged studio session lives at most 15 minutes, and the browser's BFF calls never pass
// the page proxy that renews it. The BFF renews it itself while the CRM web's partner session is valid; when that is
// gone too it answers 401 with `x-aa-session: expired`. This component reloads the page ONCE on such an answer, so the
// page proxy can send the person through the CRM web's /auth/refresh (renew or sign in) and back. Never a push to
// /tenant-login, and never a loop: a second expiry within a minute is left to the page's own error state.
import { useEffect } from "react";

const HEADER = "x-aa-session";
const KEY = "aa_studio_reload_at";

export function SessionWatch() {
  useEffect(() => {
    const original = window.fetch;
    const watched: typeof fetch = async (input, init) => {
      const res = await original(input, init);
      if (res.status === 401 && res.headers.get(HEADER) === "expired") {
        let last = 0;
        try {
          last = Number(window.sessionStorage.getItem(KEY) || "0");
          window.sessionStorage.setItem(KEY, String(Date.now()));
        } catch {
          /* storage off: still reload once per page */
        }
        if (Date.now() - last > 60_000) window.location.reload();
      }
      return res;
    };
    window.fetch = watched;
    return () => {
      if (window.fetch === watched) window.fetch = original;
    };
  }, []);
  return null;
}
