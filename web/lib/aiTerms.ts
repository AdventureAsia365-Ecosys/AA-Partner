// web/lib/aiTerms.ts
// AA-820 (S266) — the AI Schedule facts every tenant-facing terms page shows: its version and the usage policies of the
// AI providers. Source: root docs/legal/tenant-ai-schedule.md (v1.0, approved by the tech lead under root ADR 0019 b20;
// counsel reviews it before the first paying agency). The Content studio keeps the same values in portal/app/_legal/aiTerms.ts
// (api/tests/unit/test_portal_guards.py checks both); the UI kit has no legal-links module yet, so the copy lives here.

/** The AI Schedule version the terms summarise (root docs/legal/tenant-ai-schedule.md). */
export const AI_SCHEDULE_VERSION = "AI Schedule v1.0";

/** The usage policies of the AI providers, in the order the terms list them. */
export const AI_PROVIDER_POLICIES: ReadonlyArray<{ name: string; href: string }> = [
  { name: "Anthropic Usage Policy", href: "https://www.anthropic.com/legal/aup" },
  { name: "OpenAI Usage Policies", href: "https://openai.com/policies/usage-policies/" },
  { name: "AWS Acceptable Use Policy", href: "https://aws.amazon.com/aup/" },
];
