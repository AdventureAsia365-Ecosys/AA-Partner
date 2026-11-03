// AA-820 (S266): the CRM terms of use summarise the AI Schedule v1.0 (root docs/legal/tenant-ai-schedule.md): the
// models run on Amazon Bedrock under Adventure Asia's own accounts, the providers' usage policies are linked, the
// prohibited uses, the high-risk advice rule for ancillaries (insurance, visas, financing; root ADR 0019 b6) and which
// text binds. The same version and policy links as the Content studio terms (api/tests/unit/test_portal_guards.py).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { AI_PROVIDER_POLICIES, AI_SCHEDULE_VERSION } from "@/lib/aiTerms";
import { AI_TERMS, termsEn, termsVi } from "@/lib/i18nTerms";

const WEB = join(__dirname, "..");

describe("CRM terms: AI Schedule v1.0 summary", () => {
  it("lists the new bullets in order, after the models and before what binds the agency", () => {
    expect(AI_TERMS.indexOf("terms.ai.policies")).toBe(AI_TERMS.indexOf("terms.ai.models") + 1);
    for (const key of ["terms.ai.prohibited", "terms.ai.highRisk", "terms.ai.schedule"] as const) {
      expect(AI_TERMS).toContain(key);
    }
    expect(AI_TERMS[AI_TERMS.length - 1]).toBe("terms.ai.schedule");
  });

  it("says who runs the models and that the agency gets no provider account", () => {
    const models = termsEn["terms.ai.models"];
    expect(models).toContain("on Amazon Bedrock");
    expect(models).toContain("accounts Adventure Asia holds in its own company name");
    expect(models).toContain("Your agency gets no account or key");
    expect(termsVi["terms.ai.models"]).toContain("đứng tên công ty Adventure Asia");
  });

  it("links the three provider policies", () => {
    expect(AI_PROVIDER_POLICIES.map((p) => p.href)).toEqual([
      "https://www.anthropic.com/legal/aup",
      "https://openai.com/policies/usage-policies/",
      "https://aws.amazon.com/aup/",
    ]);
    const page = readFileSync(join(WEB, "app/(crm)/legal/terms/page.tsx"), "utf8");
    expect(page).toMatch(/key === "terms\.ai\.policies"[\s\S]*AI_PROVIDER_POLICIES\.map[\s\S]*href=\{policy\.href\}/);
    expect(page).toContain('rel="noopener noreferrer"');
  });

  it("keeps a qualified person and an AI disclosure in front of high-risk advice to a client", () => {
    const rule = termsEn["terms.ai.highRisk"];
    for (const word of ["Insurance", "visa", "instalment", "specific client", "qualified person", "told that AI was used"]) {
      expect(rule).toContain(word);
    }
    expect(rule).toContain("General travel information is not advice.");
    expect(termsVi["terms.ai.highRisk"]).toContain("người có chuyên môn");
  });

  it("bans the deceptive uses and names the Schedule version it summarises", () => {
    const banned = termsEn["terms.ai.prohibited"];
    for (const word of ["fake reviews", "invented personas", "hidden sponsorship", "spam", "profiles"]) {
      expect(banned).toContain(word);
    }
    const version = AI_SCHEDULE_VERSION.replace("AI Schedule ", "");
    expect(termsEn["terms.ai.schedule"]).toContain(`AI Schedule ${version}`);
    expect(termsVi["terms.ai.schedule"]).toContain(`AI Schedule ${version}`);
    expect(termsEn["terms.ai.schedule"]).toContain("the AI Schedule applies");
  });
});
