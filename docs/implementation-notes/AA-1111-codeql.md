# AA-1111 — open CodeQL alerts (S294, lane l9-codeql)

Scope: the 6 open code scanning alerts of this repo (25/11). Four are in test files and are fixed here; two are
accepted in the root exception register (`docs/security/vuln-exceptions.toml`, same wave) and dismissed "Won't fix":

- #4 `api/app/messaging/signature.py` HMAC-SHA1: the messaging provider signs its webhooks with HMAC-SHA1, so the
  check must compute the same; compared in constant time, nothing stored. Not a weak hash of stored data.
- #1 `.github/workflows/secret-scan.yml` `pull_request_target`: the PR head is checked out as data for gitleaks only
  (no script, action or config from the PR runs; rules and baseline come from the base commit).

## Decisions

- **URL substring in tests (#5, #6):** the two text-render tests check the official link with an anchored regex
  (`https://www\.immigration\.gov\.np\b`) instead of `"https://..." in body`. Same assertion, no substring check.
- **Tag stripping in a test (#3):** `marketplaceFromPrice.test.ts` read text with one `replace(/<[^>]+>/g, "")`,
  which CodeQL treats as an incomplete HTML sanitizer. The helper now splits on the tags; it reads text for an
  assertion and sanitizes nothing.
- **ReDoS in a test (#2):** `bffDownload.test.ts` `isClient()` matched block comments with `\/\*[\s\S]*?\*\/`, which
  can also match `*//*`. The body is now `(?:[^*]|\*(?!\/))*`, one way to match a run of comments.
- Checked with the CodeQL CLI locally (bundle v2.27.6): the four alerts reproduce on the base and are gone after.

## Changed

- Test files only; no app code.

## Tradeoffs

- None of weight: the HMAC-SHA1 row has to be renewed every 90 days while the provider signs with SHA-1.

## Should know

- The two register rows live in the root repo PR of this lane; the orchestrator dismisses #1 and #4 after it merges.
