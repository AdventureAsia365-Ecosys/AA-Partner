// AA-978 follow-up: Account › Members pure rules (lib/members.ts). Run: npm test (from portal/).
import { test } from "node:test";
import assert from "node:assert/strict";
import { memberLabel, missingOwner, summaryLine, type AccountMember } from "./members.ts";

const m = (over: Partial<AccountMember> = {}): AccountMember => ({
  membership_id: "m1", member_ref: "a1b2c3d4e5f6", role: "owner", status: "active",
  added_at: "2026-10-20T08:00:00.000000Z", updated_at: "2026-10-20T08:00:00.000000Z", ...over,
});

test("member label: grouped reference, or invited", () => {
  assert.equal(memberLabel(m()), "Member a1b2·c3d4·e5f6");
  assert.equal(memberLabel(m({ status: "invited", member_ref: null })), "Invited (not signed in yet)");
});

test("summary line counts and plurals", () => {
  assert.equal(summaryLine({ active: 0, invited: 0 }), "No members yet");
  assert.equal(summaryLine({ active: 1, invited: 0 }), "1 active member");
  assert.equal(summaryLine({ active: 3, invited: 2 }), "3 active members · 2 invitations pending");
});

test("missing owner only when there are members and no active owner", () => {
  assert.equal(missingOwner([]), false);
  assert.equal(missingOwner([m()]), false);
  assert.equal(missingOwner([m({ role: "editor" }), m({ status: "invited", member_ref: null })]), true);
});
