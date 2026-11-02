"use client";
// portal/lib/useCrmIdentity.ts — AA-1025 (S264): the CRM sign-in shown on the studio access pages and Account settings
// (lib/crmIdentity.ts). One request per mount; null until it answers, and when it cannot tell.
import { useEffect, useState } from "react";
import { fetchCrmIdentity, type CrmIdentity } from "./crmIdentity";

export function useCrmIdentity(): CrmIdentity {
  const [identity, setIdentity] = useState<CrmIdentity>(null);
  useEffect(() => {
    let live = true;
    void fetchCrmIdentity().then((value) => {
      if (live) setIdentity(value);
    });
    return () => {
      live = false;
    };
  }, []);
  return identity;
}
