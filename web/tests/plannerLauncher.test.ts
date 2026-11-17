// AA-1115 — the Trip planner launcher snippet (lib/plannerLauncher.ts): one link, https only, the label escaped, the
// agency's own planner domain first. Invented agency names and hosts only.
import { describe, expect, it } from "vitest";
import {
  LAUNCHER_LABEL_MAX, escapeHtml, launcherLabel, launcherSnippet, launcherTargets, safeHttpsUrl,
} from "@/lib/plannerLauncher";

const HOSTED = "https://planner.example.invalid/lantern-bay/plan";

describe("launcher targets", () => {
  it("offers the agency's own planner domains first, then the hosted planner", () => {
    expect(launcherTargets({ url: HOSTED, domains: ["trip.lanternbay.example", "TRIP.lanternbay.example"], launcher: true }))
      .toEqual(["https://trip.lanternbay.example/plan", HOSTED]);
    expect(launcherTargets({ url: HOSTED, launcher: true })).toEqual([HOSTED]);
  });

  it("offers nothing without the launcher entitlement or without CIS's answer", () => {
    expect(launcherTargets({ url: HOSTED, domains: ["trip.lanternbay.example"], launcher: false })).toEqual([]);
    expect(launcherTargets(null)).toEqual([]);
    expect(launcherTargets(undefined)).toEqual([]);
  });

  it("drops an address that is not https or not a host name", () => {
    expect(launcherTargets({ url: "http://planner.example.invalid/x/plan", domains: ["bad host", "javascript:x", ""],
      launcher: true })).toEqual([]);
    expect(safeHttpsUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpsUrl("https://user:pw@planner.example.invalid/")).toBeNull();
    expect(safeHttpsUrl(42)).toBeNull();
  });
});

describe("the snippet", () => {
  it("is one link that opens the planner in a new tab, no iframe and no script", () => {
    const link = launcherSnippet(HOSTED, "Plan your trip", "link");
    expect(link).toBe(`<a href="${HOSTED}" target="_blank" rel="noopener">Plan your trip</a>`);
    const button = launcherSnippet(HOSTED, "Plan your trip", "button") ?? "";
    expect(button).toMatch(/^<a href="[^"]+" target="_blank" rel="noopener" style="[^"]+">Plan your trip<\/a>$/);
    for (const s of [link ?? "", button]) expect(s).not.toMatch(/<iframe|<script|on[a-z]+=/i);
  });

  it("escapes the label and refuses an unsafe address", () => {
    expect(launcherSnippet(HOSTED, `"><script>x</script>`, "link")).toBe(
      `<a href="${HOSTED}" target="_blank" rel="noopener">&quot;&gt;&lt;script&gt;x&lt;/script&gt;</a>`);
    expect(launcherSnippet("javascript:alert(1)", "Go", "link")).toBeNull();
    expect(escapeHtml(`a&b'c`)).toBe("a&amp;b&#39;c");
  });

  it("trims and caps the label, with a fallback when it is empty", () => {
    expect(launcherLabel("  Plan   with us  ", "Fallback")).toBe("Plan with us");
    expect(launcherLabel("   ", "Fallback")).toBe("Fallback");
    expect(launcherLabel("x".repeat(200), "Fallback")).toHaveLength(LAUNCHER_LABEL_MAX);
  });
});
