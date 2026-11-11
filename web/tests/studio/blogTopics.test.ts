// AA-1081 — the Articles page's tours-without-a-topic list (lib/studio/blogTopics.ts). Run: npm test (from web/).
// Invented tour names only.
import { test } from "vitest";
import assert from "node:assert/strict";
import { BLOG_TOPICS_MESSAGES } from "@/app/(studio)/portal/_components/writer.messages";
import { NO_TOPIC_REASONS, isNoTopicReason, toursWithoutTopic, type BlogTopics } from "@/lib/studio/blogTopics";

const BT: BlogTopics = {
  total: 4, with_topic: 1, tours: [
    { tour_id: "t-a", tour_name: "Valdoria lakes", topics: 0, reason: "seed_too_thin" },
    { tour_id: "t-b", tour_name: null, topics: 0, reason: "low_search_interest" },
    { tour_id: "t-c", tour_name: "Corvin passes", topics: 0, reason: "brand_fit_below_7" },
    { tour_id: "t-d", tour_name: "Aurel coast", topics: 2, reason: null },
  ],
};

test("lists only the tours without a topic, keeping known reasons and dropping unknown ones", () => {
  assert.deepEqual(toursWithoutTopic(BT), [
    { tourId: "t-a", name: "Valdoria lakes", reason: "seed_too_thin" },
    { tourId: "t-b", name: null, reason: "low_search_interest" },
    { tourId: "t-c", name: "Corvin passes", reason: null },
  ]);
});

test("no coverage yet (null, missing tours) means no list", () => {
  assert.deepEqual(toursWithoutTopic(null), []);
  assert.deepEqual(toursWithoutTopic(undefined), []);
  assert.deepEqual(toursWithoutTopic({ total: 0, with_topic: 0 } as unknown as BlogTopics), []);
});

test("the codes match the CIS contract and each has English and Vietnamese words with no internal terms", () => {
  assert.deepEqual([...NO_TOPIC_REASONS], ["not_ready", "low_search_interest", "no_place_match", "seed_too_thin",
    "no_fitting_topic", "places_covered"]);
  assert.equal(isNoTopicReason("places_covered"), true);
  assert.equal(isNoTopicReason("debate"), false);
  for (const lang of ["en", "vi"] as const) {
    const m = BLOG_TOPICS_MESSAGES[lang] as Record<string, unknown>;
    for (const code of NO_TOPIC_REASONS) {
      const text = m[`reason_${code}`];
      assert.equal(typeof text, "string", `${lang} reason_${code}`);
      assert.doesNotMatch(text as string, /\b(bar|debate|score|gate|judge|seed|segment|AA-\d+)\b/i);
    }
  }
  assert.match(BLOG_TOPICS_MESSAGES.en.reason_seed_too_thin, /too short to carry a full article/);
});
