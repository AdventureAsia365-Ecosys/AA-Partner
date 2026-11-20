// AA-1168 follow-up (S286 review): the export reads the AI disclosure wording from CIS through the tenant BFF.
// A wrong path would serve the fallback line for good without any test failing, so pin the path and the
// null-on-failure contract of fetchAiDisclosure. A plain stub (not vi.fn) answers, so a rejected read is not
// tracked by the mock recorder.
import { beforeEach, describe, expect, test, vi } from "vitest";

let calls: unknown[][] = [];
let answer: () => Promise<unknown> = () => Promise.resolve(null);
vi.mock(import("@/app/_kit"), async (importOriginal) => {
  const actual = await importOriginal();
  const apiGet = ((...a: unknown[]) => {
    calls.push(a);
    return answer();
  }) as typeof actual.apiGet;
  return { ...actual, apiGet };
});

import { fetchAiDisclosure } from "@/app/(studio)/portal/_components/catalogApi";

describe("fetchAiDisclosure", () => {
  beforeEach(() => {
    calls = [];
  });

  test("reads the CIS setting through the tenant BFF path", async () => {
    const value = { text: { en: "Made with AI." }, social_tag: "#AIgenerated" };
    answer = () => Promise.resolve(value);
    await expect(fetchAiDisclosure()).resolves.toEqual(value);
    expect(calls).toEqual([["/api/tenant/v1/content-writing/ai-disclosure"]]);
  });

  test("a failed read gives null so the export falls back to its own line", async () => {
    answer = () => Promise.reject(new Error("503"));
    await expect(fetchAiDisclosure()).resolves.toBeNull();
  });
});
