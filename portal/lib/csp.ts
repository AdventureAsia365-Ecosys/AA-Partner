// portal/lib/csp.ts
// AA-979 P2b (S253) — the Content-Security-Policy of every studio response, the same policy as the CRM web
// (web/proxy.ts contentSecurityPolicy): no framing, no <base> hijack, no plugins, forms only to this origin and the
// managed-login origin. Sign-out posts a form to the web's /auth/sign-out, which answers 303 to the managed-login
// logout page, and browsers apply form-action to that redirect: with `form-action 'self'` alone the sign-out was
// blocked and the Cognito session stayed open. Set at runtime by proxy.ts (next.config.ts headers are fixed at build
// time, and a second CSP header would be enforced on top of this one). script-src / style-src stay open for the
// kit's inline styles and Next's scripts.

/** The policy; `formOrigin` empty (Vercel, no partner sign-in) = forms to this origin only, as before. */
export function contentSecurityPolicy(formOrigin: string): string {
  const forms = ["'self'", formOrigin].filter(Boolean).join(" ");
  return `frame-ancestors 'none'; base-uri 'self'; form-action ${forms}; object-src 'none'`;
}
