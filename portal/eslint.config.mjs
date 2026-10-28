import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

import { kitConfig } from "./app/_kit/core/eslint-kit.mjs";

// AA-994 S2b (root ADR 0016 decision 7) — UI kit conformance rules, shipped with the vendored kit core
// (app/_kit/core/eslint-kit.mjs, byte-identical to AA-CIS-App frontend/app/_kit/core; never edit it here). The
// `inline` preset (inline style + K tokens) runs at `error` on all page code under app/: no raw <main> / <table> /
// dialog, no hard-coded colour, no hand-styled <button>, no var(--aa-*) string, no --aa-* definition from JS, no
// import of the legacy portal ui.tsx layer (app/portal/_components/ui), no className. The vendored core is exempt
// (it is where the raw elements belong). The portal's own kit parts in app/_kit/local/** (the shell, moved there in
// S2a) are page code for this rule set and stay linted, as they were under the AA-732 rules.
//
// Existing violations (of these and of the Next rules) are frozen in eslint-suppressions.json (ESLint bulk
// suppressions; the three AA-760 legacy files that used to be `warn` are in it too). The file may only shrink: CI
// ("Portal Build") runs `eslint --prune-suppressions`, fails when the committed file differs from the pruned one, and
// fails when an entry grows against the base branch (app/_kit/core/check-kit-copy.mjs --suppressions; a new entry
// only for a deleted/renamed file's violations). After fixing violations: npx eslint --prune-suppressions. Never run
// --suppress-all to hide a new violation, and never switch a kit rule off inline (`eslint-disable… aa-kit/…` or a
// bare `eslint-disable`): CI fails on it (check-kit-copy.mjs --no-kit-disable). Disables that name other rules
// (react-hooks/…) stay allowed.
const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),

  kitConfig({
    preset: "inline",
    files: ["app/**/*.{ts,tsx,js,jsx,mjs}"],
    ignores: ["app/_kit/core/**"],
  }),
]);

export default eslintConfig;
