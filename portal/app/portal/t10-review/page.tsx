"use client";
// app/(tenant)/portal/t10-review/page.tsx — AA-501
//
// The screen that sits between T10's automatic quality gate and T11 publish. Route slug
// confirmed with Nghiệp (AA-501 STEP0 §6): "t10" is the one number in the existing
// t0/t1/t4/t6/t7/t8/t11 sequence with no page of its own (T9/T10 were folded into t8-angle-gate's
// wizard by AA-450) — this reads naturally as "the review of what T10 decided" and slots exactly
// where it belongs: between t8-angle-gate ("Write Content") and t11-publish ("Publish").
import { Suspense } from "react";
import { T, serif } from "../_components/ui";
import { ReviewList } from "../_components/ReviewList";

export default function T10ReviewPage() {
  return (
    // AA-569 — was maxWidth:720 (the "hẹp/nhỏ" complaint); no cap now, matches PlanningTab.tsx/
    // t7-planning's own full-width convention (the portal layout's <main> has no width cap of
    // its own, so a page either sets one or fills the available flex space).
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: T.ink, margin: "0 0 6px" }}>
          My Content
        </h1>
        <p style={{ fontSize: 13, color: T.muted, margin: 0, lineHeight: 1.5 }}>
          See everything you&rsquo;ve written — where it came from and whether it&rsquo;s ready —
          before you publish.
        </p>
      </div>

      {/* AA-569 — ReviewList now reads useSearchParams() (?piece= from the write-done popup's
          "Open in My Content" link) — needs a Suspense boundary or `next build` fails
          prerendering this route, same pattern t8-angle-gate/page.tsx already established. */}
      <Suspense>
        <ReviewList />
      </Suspense>
    </div>
  );
}
