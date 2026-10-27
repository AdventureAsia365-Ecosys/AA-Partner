#!/usr/bin/env bash
# AA-Partner pre-push checks (AA-993 H1, root ADR 0016 draft D5): run the CI Lint / Security Audit / Unit / Web /
# Portal / docs checks locally before a push, with the same commands and pinned tool versions as
# .github/workflows/ci.yml and docs-lint.yml.
#
# Usage (from anywhere inside the repo):
#   scripts/pre-push.sh              fast mode (default): only the checks the diff vs origin/main needs
#   scripts/pre-push.sh --full       every local check, the unit step = the exact CI command with coverage
#   scripts/pre-push.sh --ci         reserved for H2 (CI calls this script): like --full, never skips by diff,
#                                    and a missing optional tool (lychee) is a failure, not a warning
#   scripts/pre-push.sh --plan       print the checks it would run and exit (no tool is invoked)
#   scripts/pre-push.sh --self-test  test the decision logic on fixture diffs (no tool is invoked)
#   --from-hook (set by .githooks/pre-push): refuse when tracked files differ from HEAD (the push sends HEAD)
# Environment: AA_PREPUSH=full (same as --full), AA_PREPUSH_BASE=<ref> (default origin/main),
#   AA_PREPUSH_VENV=<venv dir> (Python env with api/requirements-dev.txt installed).
# Bypass once: git push --no-verify. Uninstall: git config --unset core.hooksPath.
#
# Not run locally (CI only): Integration Tests (needs PostgreSQL), the three Docker Build Checks / Trivy / SBOM and
# the standalone smoke (heavy), Vendored CIS copies (network), gitleaks, PR title. Never `next build`: the web's type
# check runs as `tsc --noEmit`. Portal Lint is non-required in CI, so here it only warns.
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "pre-push: not inside a git repo" >&2; exit 2; }
BYPASS_HINT="push without the checks (once): git push --no-verify"

# ---------------------------------------------------------------------------------------------------------------
# Decision logic (pure: file list in, check ids out). --self-test exercises only this part.
# ---------------------------------------------------------------------------------------------------------------

# classify_file <path> -> space-separated groups
classify_file() {
  local f="$1" groups=""
  case "$f" in
    api/requirements*.txt) groups+=" py deps unit_all" ;;
    api/pyproject.toml) groups+=" py unit_all" ;;
  esac
  case "$f" in
    api/tests/conftest.py | api/tests/unit/conftest.py) groups+=" py unit_all" ;;
    api/*.py) groups+=" py unit" ;;
    api/*.md) ;;
    # Migrations, the table templates, the Dockerfile: read by unit tests by file name (select_unit_tests).
    api/*) groups+=" unit" ;;
    # The portal guard tests walk portal/; other tests read web/ and portal/ files by name.
    web/package.json | web/package-lock.json) groups+=" web web_deps unit" ;;
    web/*) groups+=" web unit" ;;
    portal/package.json | portal/package-lock.json) groups+=" portal portal_deps unit" ;;
    portal/*) groups+=" portal unit" ;;
    e2e/*) groups+=" e2e" ;;
    .trivyignore | .github/scripts/check_trivyignore.py) groups+=" trivyignore" ;;
    tools/docs/*) groups+=" docs docs_tools" ;;
    # The vendored contract and its samples: unit tests read them by directory (and pin the contract's sha256).
    docs/contracts/*) groups+=" unit" ;;
    scripts/pre-push.sh | scripts/install-hooks.sh | .githooks/*) groups+=" hook" ;;
    # Unit tests read these files (deploy/verify scripts, workflows, release config, the root .vercelignore);
    # select_unit_tests matches them by file name.
    .github/workflows/* | .github/scripts/* | .github/verify/* | .release-please-manifest.json | \
      release-please-config.json | .vercelignore) groups+=" unit" ;;
    # Marker (not a path): the diff deletes or renames a file, which can break a Markdown link.
    :deleted-or-renamed) groups+=" links" ;;
  esac
  case "$f" in
    *.md | docs/*) groups+=" docs" ;;
  esac
  echo "$groups"
}

# plan_checks <mode> [files...] -> one check id per line, in run order. mode: fast | full | ci
# Fast mode never plans unit-full (the whole suite with coverage, ~2 min in CI): a shared fixture or dependency change
# plans unit-changed and main() prints a warning (see unit_all_files). --full / --ci do not plan e2e-typecheck (CI
# runs it only in the portal UI smoke); fast mode plans it when e2e/ changed.
plan_checks() {
  local mode="$1"; shift
  local g all=" " f
  if [[ "$mode" != "fast" ]]; then
    all=" hook py deps unit_full trivyignore docs docs_tools web web_deps portal portal_deps "
  else
    for f in "$@"; do
      [[ -z "$f" ]] && continue
      for g in $(classify_file "$f"); do
        [[ "$all" == *" $g "* ]] || all+="$g "
      done
    done
    if [[ "$all" == *" unit_all "* ]] && [[ "$all" != *" unit "* ]]; then all+="unit "; fi
  fi
  echo "resource-ids"
  [[ "$all" == *" hook "* ]] && echo "hook-self-test"
  [[ "$all" == *" trivyignore "* ]] && echo "trivyignore"
  if [[ "$all" == *" py "* ]]; then echo "ruff-check"; echo "ruff-format"; fi
  [[ "$all" == *" deps "* ]] && echo "pip-audit"
  if [[ "$all" == *" unit_full "* ]]; then echo "unit-full"
  elif [[ "$all" == *" unit "* ]]; then echo "unit-changed"; fi
  if [[ "$all" == *" docs "* ]]; then
    [[ "$all" == *" docs_tools "* ]] && echo "docs-tools-tests"
    echo "docs-frontmatter"; echo "docs-adr"; echo "docs-aws-multiline"; echo "docs-links"
  elif [[ "$all" == *" links "* ]]; then
    echo "docs-links"
  fi
  if [[ "$all" == *" web "* ]]; then echo "web-typecheck"; echo "web-test"; fi
  [[ "$all" == *" web_deps "* ]] && echo "web-audit"
  if [[ "$all" == *" portal "* ]]; then echo "portal-typecheck"; echo "portal-test"; echo "portal-lint"; fi
  [[ "$all" == *" portal_deps "* ]] && echo "portal-audit"
  [[ "$all" == *" e2e "* ]] && echo "e2e-typecheck"
  return 0
}

# unit_all_files [files...] -> the changed files that a targeted unit selection cannot cover (shared fixtures, the
# dependency set, pytest config), one per line.
unit_all_files() {
  local f
  for f in "$@"; do
    [[ "$(classify_file "$f")" == *" unit_all"* ]] && echo "$f"
  done
  return 0
}

# changed_files <repo root> <base> -> NUL-separated paths changed on HEAD since the merge base, unquoted (non-ASCII
# names stay as they are). Fails (non-zero) when git cannot compute the diff, e.g. no merge base.
changed_files() {
  git -C "$1" -c core.quotePath=false diff --name-only -z "$2...HEAD"
}

# deletes_or_renames <repo root> <base> -> exit 0 when the diff deletes or renames a file.
deletes_or_renames() {
  [[ "$(git -C "$1" diff --name-only -z --diff-filter=DR "$2...HEAD" | head -c 1 | wc -c)" -gt 0 ]]
}

# select_unit_tests <api dir> [files...] -> test files under tests/unit (relative to api/), one per line.
# Changed test files; tests/unit/test_<stem>.py and test_<stem>_*.py of a changed module; the tests importing it;
# the tests whose text names a changed file (workflows, .github scripts, migrations, web/portal files, scripts loaded
# by path); the code-rule guards that walk app/ (a line with APP and rglob) and test_route_guard.py when any api/app
# file changed; the portal guards (a line with PORTAL and rglob) when any portal/ file changed; the tests that name
# the contracts folder when any docs/contracts/ file changed (they read the samples by directory, not by name).
select_unit_tests() {
  local adir="$1"; shift
  local f rel stem mod pkg leaf t base
  [[ -d "$adir/tests/unit" ]] || return 0
  {
    for f in "$@"; do
      [[ -z "$f" ]] && continue
      base="$(basename "$f")"
      if [[ "$base" != "__init__.py" ]]; then
        grep -lF -- "$base" "$adir"/tests/unit/test_*.py 2>/dev/null | sed "s#^$adir/##"
      fi
      case "$f" in
        api/app/*)
          grep -lE 'APP.*rglob' "$adir"/tests/unit/test_*.py 2>/dev/null | sed "s#^$adir/##"
          [[ -f "$adir/tests/unit/test_route_guard.py" ]] && echo "tests/unit/test_route_guard.py" ;;
        portal/*)
          grep -lE 'PORTAL.*rglob' "$adir"/tests/unit/test_*.py 2>/dev/null | sed "s#^$adir/##" ;;
        docs/contracts/*)
          grep -lE '"contracts"|docs/contracts' "$adir"/tests/unit/test_*.py 2>/dev/null | sed "s#^$adir/##" ;;
      esac
      case "$f" in
        api/tests/unit/test_*.py) rel="${f#api/}"; [[ -f "$adir/$rel" ]] && echo "$rel" ;;
        # Test package markers: every test imports through them, so import matching would pick the whole suite.
        api/tests/__init__.py | api/tests/*/__init__.py) ;;
        api/*.py)
          rel="${f#api/}"; stem="$(basename "$rel" .py)"
          mod="${rel%.py}"; mod="${mod//\//.}"; [[ "$stem" == "__init__" ]] && { mod="${mod%.__init__}"; stem="${mod##*.}"; }
          pkg="${mod%.*}"; leaf="${mod##*.}"
          for t in "$adir"/tests/unit/test_"$stem".py "$adir"/tests/unit/test_"$stem"_*.py; do
            [[ -f "$t" ]] && echo "tests/unit/${t##*/}"
          done
          grep -lE "(^|[^A-Za-z0-9_.])${mod//./\\.}([^A-Za-z0-9_]|$)|from ${pkg//./\\.} import .*\b${leaf}\b" \
            "$adir"/tests/unit/test_*.py 2>/dev/null | sed "s#^$adir/##"
          ;;
      esac
    done
  } | sort -u
}

# eslint_targets [files...] -> the changed portal files ESLint lints, relative to portal/, one per line.
eslint_targets() {
  local f
  for f in "$@"; do
    case "$f" in
      portal/*.ts | portal/*.tsx | portal/*.js | portal/*.jsx | portal/*.mjs) echo "${f#portal/}" ;;
    esac
  done
  return 0
}

# ---------------------------------------------------------------------------------------------------------------
# --self-test
# ---------------------------------------------------------------------------------------------------------------
self_test() {
  local fails=0 n=0
  # Run from a real hook, git may export these; the scratch repos below must not inherit them.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  expect() { # expect <name> <expected ids, space-separated> <mode> [files...]
    local name="$1" want="$2" mode="$3"; shift 3
    local got; got="$(plan_checks "$mode" "$@" | tr '\n' ' ' | sed 's/ $//')"
    n=$((n + 1))
    if [[ "$got" == "$want" ]]; then echo "ok   $name"; else echo "FAIL $name"; echo "     want: $want"; echo "     got:  $got"; fails=$((fails + 1)); fi
  }
  local DOCS="docs-frontmatter docs-adr docs-aws-multiline docs-links"
  local WEB="web-typecheck web-test" PORTAL="portal-typecheck portal-test portal-lint"
  local ALL="resource-ids hook-self-test trivyignore ruff-check ruff-format pip-audit unit-full docs-tools-tests $DOCS $WEB web-audit $PORTAL portal-audit"
  expect "empty diff: resource IDs only" "resource-ids" fast
  expect "app module" "resource-ids ruff-check ruff-format unit-changed" fast api/app/routers/pipeline.py
  expect "test file" "resource-ids ruff-check ruff-format unit-changed" fast api/tests/unit/test_health.py
  expect "shared test helper" "resource-ids ruff-check ruff-format unit-changed" fast api/tests/unit/fakes.py
  expect "unit conftest -> targeted unit + warning (never the full suite in fast mode)" \
    "resource-ids ruff-check ruff-format unit-changed" fast api/tests/conftest.py
  expect "pyproject -> targeted unit + warning" "resource-ids ruff-check ruff-format unit-changed" fast api/pyproject.toml
  expect "requirements -> pip-audit + targeted unit + warning" "resource-ids ruff-check ruff-format pip-audit unit-changed" \
    fast api/requirements-dev.txt
  expect "migration -> the unit tests that read it" "resource-ids unit-changed" fast api/migrations/031_example.sql
  expect "api README -> docs only" "resource-ids $DOCS" fast api/README.md
  expect "web -> tsc + vitest, never build" "resource-ids unit-changed $WEB" fast web/app/pipeline/page.tsx
  expect "web lockfile -> npm audit too" "resource-ids unit-changed $WEB web-audit" fast web/package-lock.json
  expect "portal -> tsc + node tests + lint (warn), never build" "resource-ids unit-changed $PORTAL" fast portal/proxy.ts
  expect "portal manifest -> npm audit too" "resource-ids unit-changed $PORTAL portal-audit" fast portal/package.json
  expect "e2e -> its tsc only" "resource-ids e2e-typecheck" fast e2e/tests/slice1.spec.ts
  expect "markdown -> docs lint" "resource-ids $DOCS" fast CONTEXT.md
  expect "markdown with a space in the name" "resource-ids $DOCS" fast "docs/a b.md"
  expect "non-ASCII markdown name" "resource-ids $DOCS" fast "docs/Tài liệu.md"
  expect "docs tools -> their unit tests too" "resource-ids docs-tools-tests $DOCS" fast tools/docs/check_frontmatter.py
  expect ".trivyignore" "resource-ids trivyignore" fast .trivyignore
  expect "trivyignore checker" "resource-ids trivyignore" fast .github/scripts/check_trivyignore.py
  expect "workflow only -> the unit tests that read it" "resource-ids unit-changed" fast .github/workflows/deploy-dev.yml
  expect "release config -> unit tests" "resource-ids unit-changed" fast .release-please-manifest.json
  expect "contract copy -> its unit tests + docs lint" "resource-ids unit-changed $DOCS" fast \
    docs/contracts/tenant.membership.v1.openapi.yaml
  expect ".vercelignore -> unit tests" "resource-ids unit-changed" fast .vercelignore
  expect "hook files -> hook self-test" "resource-ids hook-self-test" fast scripts/pre-push.sh .githooks/pre-push
  expect "delete or rename -> link check" "resource-ids docs-links" fast :deleted-or-renamed
  expect "mixed diff" "resource-ids ruff-check ruff-format unit-changed $DOCS $WEB $PORTAL" fast \
    api/app/db.py docs/implementation-notes/AA-993.md web/lib/pipeline.ts portal/lib/config.ts api/migrations/031_x.sql
  expect "--full ignores the diff" "$ALL" full
  expect "--ci never skips by diff" "$ALL" ci README.md

  local got
  got="$(unit_all_files api/app/db.py api/tests/conftest.py api/requirements.txt | tr '\n' ' ' | sed 's/ $//')"
  n=$((n + 1))
  if [[ "$got" == "api/tests/conftest.py api/requirements.txt" ]]; then echo "ok   unit_all warning files"
  else echo "FAIL unit_all warning files"; echo "     got:  $got"; fails=$((fails + 1)); fi

  got="$(eslint_targets portal/proxy.ts portal/app/x/page.tsx portal/README.md portal/eslint.config.mjs web/a.ts | tr '\n' ' ' | sed 's/ $//')"
  n=$((n + 1))
  if [[ "$got" == "proxy.ts app/x/page.tsx eslint.config.mjs" ]]; then echo "ok   eslint targets"
  else echo "FAIL eslint targets"; echo "     got:  $got"; fails=$((fails + 1)); fi

  # Unit test selection on a fixture tree (no pytest run).
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "$tmp/tests/unit"
  : >"$tmp/tests/unit/test_pipeline.py"; : >"$tmp/tests/unit/test_pipeline_board.py"; : >"$tmp/tests/unit/test_route_guard.py"
  echo "from app.vault.cipher import seal" >"$tmp/tests/unit/test_vault_detail.py"
  echo "from app.vault import cipher" >"$tmp/tests/unit/test_vault_other.py"
  echo "import app.vault.cipherx" >"$tmp/tests/unit/test_not_this.py"
  echo 'for p in sorted(APP.rglob("*.py")):' >"$tmp/tests/unit/test_code_rules.py"
  printf '%s\n' 'for p in PORTAL.rglob("*"):' 'lines = _read(".vercelignore").splitlines()' >"$tmp/tests/unit/test_portal_guards.py"
  echo 'DOCS = Path(__file__).resolve().parents[3] / "docs" / "contracts"' >"$tmp/tests/unit/test_cis_contract.py"
  echo 'SAMPLES = ROOT / "docs" / "contracts" / "samples" / "tenant.membership.v1"' >"$tmp/tests/unit/test_ops_e2e_staff.py"
  echo 'WORKFLOW = ROOT / ".github/workflows/deploy-dev.yml"' >"$tmp/tests/unit/test_deploy_wf.py"
  echo 'sql = (ROOT / "migrations" / "005_crm_intake_pipeline.sql").read_text()' >"$tmp/tests/unit/test_lifecycle.py"
  echo 'web = (WEB / "lib" / "pipeline.ts").read_text()' >"$tmp/tests/unit/test_lost_codes.py"
  echo "from tests.unit.fakes import ScriptedConn" >"$tmp/tests/unit/test_health.py"
  echo "from tests.unit.slice_fakes import FakeVault" >"$tmp/tests/unit/test_slice.py"
  sel() { select_unit_tests "$tmp" "$@" | tr '\n' ' ' | sed 's/ $//'; }
  check_sel() { # check_sel <name> <want> [files...]
    local name="$1" want="$2"; shift 2
    local got; got="$(sel "$@")"
    n=$((n + 1))
    if [[ "$got" == "$want" ]]; then echo "ok   $name"; else echo "FAIL $name"; echo "     want: $want"; echo "     got:  $got"; fails=$((fails + 1)); fi
  }
  local GUARDS="tests/unit/test_code_rules.py tests/unit/test_route_guard.py"
  check_sel "select: tests named after the module + app guards" \
    "tests/unit/test_code_rules.py tests/unit/test_pipeline.py tests/unit/test_pipeline_board.py tests/unit/test_route_guard.py" \
    api/app/routers/pipeline.py
  check_sel "select: tests importing the module + app guards" \
    "$GUARDS tests/unit/test_vault_detail.py tests/unit/test_vault_other.py" api/app/vault/cipher.py
  check_sel "select: app module without tests -> the app guards only" "$GUARDS" api/app/nothing_here.py
  check_sel "select: migration read by name" "tests/unit/test_lifecycle.py" api/migrations/005_crm_intake_pipeline.sql
  check_sel "select: changed test file" "tests/unit/test_pipeline.py" api/tests/unit/test_pipeline.py
  check_sel "select: shared helper by import, not its sibling" "tests/unit/test_health.py" api/tests/unit/fakes.py
  check_sel "select: workflow named in a test" "tests/unit/test_deploy_wf.py" .github/workflows/deploy-dev.yml
  check_sel "select: web file read by name" "tests/unit/test_lost_codes.py" web/lib/pipeline.ts
  check_sel "select: any portal file -> portal guards" "tests/unit/test_portal_guards.py" portal/app/x/page.tsx
  check_sel "select: root .vercelignore read by name" "tests/unit/test_portal_guards.py" .vercelignore
  check_sel "select: a contract sample -> the tests reading the contracts folder" \
    "tests/unit/test_cis_contract.py tests/unit/test_ops_e2e_staff.py" \
    docs/contracts/samples/tenant.membership.v1/activation-valid.json
  check_sel "select: a test package marker selects nothing" "" api/tests/__init__.py
  check_sel "select: an app package __init__ -> every importer of the package + app guards" \
    "tests/unit/test_code_rules.py tests/unit/test_not_this.py tests/unit/test_route_guard.py tests/unit/test_vault_detail.py tests/unit/test_vault_other.py" \
    api/app/vault/__init__.py
  rm -rf "$tmp"

  # The diff reader on a scratch repo: unquoted non-ASCII and spaced names, a failing diff is reported.
  tmp="$(mktemp -d)"
  local g=(git -C "$tmp" -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false -c core.hooksPath=/dev/null)
  local files=()
  if "${g[@]}" init -q -b main 2>/dev/null && "${g[@]}" commit -q --allow-empty -m base &&
    mkdir -p "$tmp/docs" && : >"$tmp/docs/a b.md" && : >"$tmp/docs/Tài.md" && "${g[@]}" add -A &&
    "${g[@]}" commit -q -m docs; then
    mapfile -d '' -t files < <(changed_files "$tmp" main~1)
    got="$(plan_checks fast "${files[@]}" | tr '\n' ' ' | sed 's/ $//')"
    n=$((n + 1))
    if [[ ${#files[@]} -eq 2 && "$got" == "resource-ids $DOCS" ]]; then echo "ok   diff reader: non-ASCII and spaced names"
    else echo "FAIL diff reader: non-ASCII and spaced names"; echo "     files: ${files[*]}"; echo "     got:  $got"; fails=$((fails + 1)); fi
    n=$((n + 1))
    if changed_files "$tmp" no-such-ref >/dev/null 2>&1; then echo "FAIL diff reader: a bad base must fail"; fails=$((fails + 1))
    else echo "ok   diff reader: a bad base fails"; fi
    n=$((n + 1))
    "${g[@]}" mv "docs/a b.md" docs/c.md && "${g[@]}" commit -q -m mv
    if deletes_or_renames "$tmp" main~1 && ! deletes_or_renames "$tmp" main~2; then echo "ok   diff reader: deletes/renames"
    else echo "FAIL diff reader: deletes/renames"; fails=$((fails + 1)); fi

    # The hook (.githooks/pre-push) on stdin push lines; a stub scripts/pre-push.sh stands in for the checks.
    local hook="$REPO_ROOT/.githooks/pre-push" head base zero out rc
    head="$("${g[@]}" rev-parse HEAD)"; base="$("${g[@]}" rev-parse main~1)"; zero="0000000000000000000000000000000000000000"
    mkdir -p "$tmp/scripts"
    # shellcheck disable=SC2016 # the stub is literal shell text
    printf '%s\n' '[[ "${1:-}" == --from-hook ]] && echo stub-ran' >"$tmp/scripts/pre-push.sh"
    hook_case() { # hook_case <name> <want exit> <want output substring> <push line> [<forbidden substring>]
      n=$((n + 1))
      out="$(cd "$tmp" && bash "$hook" <<<"$4" 2>&1)"; rc=$?
      if [[ $rc -eq $2 && "$out" == *"$3"* && ( -z "${5:-}" || "$out" != *"$5"* ) ]]; then echo "ok   $1"
      else echo "FAIL $1"; echo "     exit $rc, output: $out"; fails=$((fails + 1)); fi
    }
    hook_case "hook: a pushed sha that is not HEAD exits 1" 1 "checks run on HEAD" \
      "refs/heads/other $base refs/heads/other $zero" stub-ran
    hook_case "hook: a delete-only push exits 0 without checks" 0 "" "(delete) $zero refs/heads/old $base" stub-ran
    "${g[@]}" -c tag.gpgSign=false tag -a -m t v1 HEAD
    hook_case "hook: an annotated tag on HEAD runs the checks" 0 "stub-ran" \
      "refs/tags/v1 $("${g[@]}" rev-parse v1) refs/tags/v1 $zero"
    hook_case "hook: HEAD runs the checks with --from-hook" 0 "stub-ran" "refs/heads/main $head refs/heads/main $zero"

    # From the hook, a tracked file that differs from HEAD stops the push before any check.
    echo edit >>"$tmp/docs/c.md"
    n=$((n + 1))
    out="$(cd "$tmp" && bash "$REPO_ROOT/scripts/pre-push.sh" --from-hook </dev/null 2>&1)"; rc=$?
    if [[ $rc -eq 1 && "$out" == *"tracked files differ from HEAD"* && "$out" != *"==> ["* ]]; then
      echo "ok   hook: a dirty tracked file blocks the push"
    else echo "FAIL hook: a dirty tracked file blocks the push"; echo "     exit $rc, output: $out"; fails=$((fails + 1)); fi
  else
    n=$((n + 1)); echo "FAIL diff reader: could not build the scratch repo"; fails=$((fails + 1))
  fi
  rm -rf "$tmp"

  echo "self-test: $((n - fails))/$n passed"
  [[ $fails -eq 0 ]]
}

# ---------------------------------------------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------------------------------------------
pin_of() { grep -E "^$1==" "$REPO_ROOT/api/requirements-dev.txt" | head -1; }

# The api Python: AA_PREPUSH_VENV, this checkout's api/.venv, the main checkout's api/.venv (worktrees share it),
# then python3 on PATH. It must import the dev requirements (fastapi, pytest).
resolve_api_python() {
  local main_root cand
  main_root="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"
  for cand in "${AA_PREPUSH_VENV:+$AA_PREPUSH_VENV/bin/python}" "$REPO_ROOT/api/.venv/bin/python" \
    "$main_root/api/.venv/bin/python" "$(command -v python3 || true)"; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    if "$cand" -c "import fastapi, pytest" >/dev/null 2>&1; then echo "$cand"; return 0; fi
  done
  return 1
}

# last_failed_unit_files <api dir> -> unit test files recorded as failed in pytest's cache that still exist,
# relative to api/, one per line. Entries outside tests/unit/ (integration runs) and stale ones are dropped.
last_failed_unit_files() {
  [[ -n "${API_PY:-}" && -s "$1/.pytest_cache/v/cache/lastfailed" ]] || return 0
  "$API_PY" - "$1" <<'PYEOF' 2>/dev/null || true
import json, os, sys
root = sys.argv[1]
try:
    with open(os.path.join(root, ".pytest_cache/v/cache/lastfailed"), encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
seen = set()
for key in data if isinstance(data, dict) else ():
    if not isinstance(key, str) or not key.startswith("tests/unit/"):
        continue
    path = key.split("::", 1)[0]
    if path.endswith(".py") and path not in seen and os.path.isfile(os.path.join(root, path)):
        seen.add(path)
        print(path)
PYEOF
}

# Python for tools/docs (needs tools/docs/requirements.txt: jsonschema, PyYAML).
resolve_docs_python() {
  local cand
  for cand in "${API_PY:-}" "$(command -v python3 || true)"; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    if "$cand" -c "import jsonschema, yaml" >/dev/null 2>&1; then echo "$cand"; return 0; fi
  done
  return 1
}

# ---------------------------------------------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------------------------------------------
FAILED=()
WARNED=()
# FAILED entries are "id SEP dir SEP command SEP ci"; the unit separator cannot appear in a command (docs-links has '|').
SEP=$'\x1f'
LYCHEE_VERSION="0.24.2"
LYCHEE_HINT="install lychee v0.24.2 (the version lychee-action v2.9.0 runs in CI): a release binary from https://github.com/lycheeverse/lychee/releases, or cargo install lychee --version 0.24.2"
API_SETUP_HINT="python3.12 -m venv api/.venv && api/.venv/bin/pip install -r api/requirements-dev.txt (or set AA_PREPUSH_VENV)"

# run_step <check id> <dir relative to repo root> <ci reference> <display command> -- <argv...>
# A failing step is recorded in FAILED (blocks the push); with SOFT_STEP=1 it goes to WARNED instead.
run_step() {
  local id="$1" dir="$2" ci="$3" shown="$4"; shift 5
  local t0 rc
  t0=$(date +%s)
  echo
  echo "==> [$id] (cd ${dir:-.} && $shown)"
  (cd "$REPO_ROOT/$dir" && "$@")
  rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "    ok ($(( $(date +%s) - t0 ))s)"
  elif [[ "${SOFT_STEP:-0}" == "1" ]]; then
    echo "    failed, not blocking (exit $rc, $(( $(date +%s) - t0 ))s)"
    WARNED+=("$id failed ($ci is non-required): (cd ${dir:-.} && $shown)")
  else
    echo "    FAILED (exit $rc, $(( $(date +%s) - t0 ))s)"
    FAILED+=("$id${SEP}${dir:-.}${SEP}$shown${SEP}$ci")
  fi
}

# fail_setup <check id> <ci reference> <what is missing> <how to fix>
fail_setup() {
  echo
  echo "==> [$1] cannot run: $3"
  echo "    fix: $4"
  FAILED+=("$1${SEP}.${SEP}$4${SEP}$2")
}

# need_node_modules <check id> <dir> <ci reference> <install command> -> 0 when <dir>/node_modules exists
need_node_modules() {
  [[ -d "$REPO_ROOT/$2/node_modules" ]] && return 0
  fail_setup "$1" "$3" "$2/node_modules missing" "(cd $2 && $4)"
  return 1
}

run_check() {
  local id="$1"
  case "$id" in
    resource-ids)
      run_step "$id" "" "ci.yml / Security Audit / No literal AWS resource IDs" \
        "python3 .github/scripts/check_resource_ids.py" -- python3 .github/scripts/check_resource_ids.py ;;
    trivyignore)
      run_step "$id" "" "ci.yml / Docker Build Check / Trivy ignore entries" \
        "python3 .github/scripts/check_trivyignore.py .trivyignore" -- python3 .github/scripts/check_trivyignore.py .trivyignore ;;
    ruff-check | ruff-format)
      if [[ -z "$API_PY" ]]; then
        fail_setup "$id" "ci.yml / Lint" "no Python env with api/requirements-dev.txt" "$API_SETUP_HINT"
        return
      fi
      local want have
      want="$(pin_of ruff)"; have="ruff==$("$API_PY" -m ruff --version 2>/dev/null | awk '{print $2}')"
      if [[ "$have" != "$want" ]]; then
        fail_setup "$id" "ci.yml / Lint" "ruff version $have, CI pins $want" "$API_PY -m pip install '$want'"
        return
      fi
      if [[ "$id" == "ruff-check" ]]; then
        run_step "$id" api "ci.yml / Lint / ruff check" "ruff check ." -- "$API_PY" -m ruff check .
      else
        run_step "$id" api "ci.yml / Lint / ruff format --check" "ruff format --check ." -- "$API_PY" -m ruff format --check .
      fi ;;
    pip-audit)
      local want ver; want="pip-audit==2.10.1"; ver="${want#*==}"
      if [[ -z "$API_PY" ]] || ! "$API_PY" -m pip_audit --version 2>/dev/null | grep -qE "(^| )${ver//./\\.}\$"; then
        fail_setup "$id" "ci.yml / Security Audit / pip-audit" "$want not installed in the api env" \
          "${API_PY:-python3} -m pip install $want"
        return
      fi
      run_step "$id" api "ci.yml / Security Audit / pip-audit" \
        "pip-audit -r requirements.txt && pip-audit -r requirements-dev.txt" -- \
        bash -c '"$0" -m pip_audit -r requirements.txt && "$0" -m pip_audit -r requirements-dev.txt' "$API_PY" ;;
    unit-full | unit-changed)
      if [[ -z "$API_PY" ]]; then
        fail_setup "$id" "ci.yml / Unit Tests" "no Python env with api/requirements-dev.txt" "$API_SETUP_HINT"
        return
      fi
      if [[ "$id" == "unit-full" ]]; then
        if ! "$API_PY" -c "import pytest_cov" >/dev/null 2>&1; then
          fail_setup "$id" "ci.yml / Unit Tests" "pytest-cov missing in $API_PY (the CI command uses --cov)" \
            "$API_PY -m pip install -r api/requirements-dev.txt"
          return
        fi
        run_step "$id" api "ci.yml / Unit Tests / Run unit tests" \
          "ENVIRONMENT=test COVERAGE_CORE=sysmon pytest tests/unit -v --tb=short --cov=app --cov-report=term-missing --cov-fail-under=90" -- \
          env ENVIRONMENT=test COVERAGE_CORE=sysmon "$API_PY" -m pytest tests/unit -v --tb=short --cov=app \
          --cov-report=term-missing --cov-fail-under=90
        return
      fi
      local tests=()
      mapfile -t tests < <(select_unit_tests "$REPO_ROOT/api" "${CHANGED[@]}")
      # Plus the unit test files that failed last time and still exist (pytest's cache), in the same run. Not --lf:
      # with only stale or non-unit entries in the cache, --lf deselects nothing and runs the whole suite.
      local last=()
      mapfile -t last < <(last_failed_unit_files "$REPO_ROOT/api")
      if [[ ${#last[@]} -gt 0 ]]; then
        echo "    + ${#last[@]} test file(s) that failed last time"
        mapfile -t tests < <(printf '%s\n' "${tests[@]}" "${last[@]}" | sed '/^$/d' | sort -u)
      fi
      if [[ ${#tests[@]} -eq 0 ]]; then
        echo
        echo "==> [$id] no unit test matches the changed files (--full runs the CI command)"
        return
      fi
      run_step "$id" api "ci.yml / Unit Tests (changed modules only; CI runs all with coverage)" \
        "ENVIRONMENT=test pytest ${tests[*]} -q --tb=short" -- \
        env ENVIRONMENT=test "$API_PY" -m pytest "${tests[@]}" -q --tb=short ;;
    docs-tools-tests | docs-frontmatter | docs-adr | docs-aws-multiline)
      # check_frontmatter.py (and the tools' tests, which import it) need jsonschema + PyYAML; the ADR and AWS text
      # checks are stdlib only and run on any python3.
      local dpy=""
      if [[ "$id" == "docs-frontmatter" || "$id" == "docs-tools-tests" ]] && ! dpy="$(resolve_docs_python)"; then
        fail_setup "$id" "docs-lint.yml" "no Python with jsonschema + PyYAML" \
          "python3 -m pip install -r tools/docs/requirements.txt"
        return
      fi
      [[ -z "$dpy" ]] && dpy="$(resolve_docs_python || command -v python3)"
      if [[ -z "$dpy" ]]; then
        fail_setup "$id" "docs-lint.yml" "no python3 on PATH" "install Python 3.12, then python3 -m pip install -r tools/docs/requirements.txt"
        return
      fi
      case "$id" in
        docs-tools-tests) run_step "$id" "" "docs-lint.yml / Unit tests of the lint tools" \
          "python -m unittest discover -s tools/docs -p 'test_*.py'" -- "$dpy" -m unittest discover -s tools/docs -p 'test_*.py' ;;
        docs-frontmatter) run_step "$id" "" "docs-lint.yml / Front-matter schema" \
          "python tools/docs/check_frontmatter.py" -- "$dpy" tools/docs/check_frontmatter.py ;;
        docs-adr) run_step "$id" "" "docs-lint.yml / ADR status and index" \
          "python tools/docs/check_adr_index.py docs/adr" -- "$dpy" tools/docs/check_adr_index.py docs/adr ;;
        docs-aws-multiline) run_step "$id" "" "docs-lint.yml / Single-line AWS CLI" \
          "python tools/docs/check_docs_text.py aws-multiline --all" -- "$dpy" tools/docs/check_docs_text.py aws-multiline --all ;;
      esac ;;
    docs-links)
      if ! command -v lychee >/dev/null 2>&1; then
        if [[ "$MODE" == "ci" ]]; then
          fail_setup "$id" "docs-lint.yml / Internal links (offline)" "lychee not installed" "$LYCHEE_HINT"
        else
          echo; echo "==> [$id] skipped: lychee not installed (CI checks internal links)"
          WARNED+=("$id: lychee not installed; $LYCHEE_HINT")
        fi
        return
      fi
      local lychee_have; lychee_have="$(lychee --version 2>/dev/null | awk '{print $2}')"
      if [[ "$lychee_have" != "$LYCHEE_VERSION" ]]; then
        WARNED+=("$id: lychee ${lychee_have:-unknown} installed, CI runs $LYCHEE_VERSION (results may differ); $LYCHEE_HINT")
      fi
      # The tracked pages only (what the CI checkout has): a local './**/*.md' would also walk the node_modules
      # folders and api/.venv.
      run_step "$id" "" "docs-lint.yml / Internal links (offline)" \
        "git ls-files -z '*.md' | xargs -0 -r lychee --config tools/docs/lychee.toml" -- \
        bash -c 'git ls-files -z "*.md" | xargs -0 -r lychee --config tools/docs/lychee.toml' ;;
    hook-self-test)
      run_step "$id" "" "this hook's own decision tests (not a CI step)" \
        "bash scripts/pre-push.sh --self-test" -- bash scripts/pre-push.sh --self-test ;;
    web-typecheck)
      need_node_modules "$id" web "ci.yml / Web Build" "npm ci --no-audit --no-fund" || return
      # CI type-checks inside `npm run build` (next build); locally tsc only, never the build.
      run_step "$id" web "ci.yml / Web Build / Build (includes the type check)" "npm run typecheck" -- npm run typecheck --silent ;;
    web-test)
      need_node_modules "$id" web "ci.yml / Web Build" "npm ci --no-audit --no-fund" || return
      run_step "$id" web "ci.yml / Web Build / Unit tests" "npm test" -- npm test --silent ;;
    web-audit)
      run_step "$id" web "ci.yml / Security Audit / npm audit (web, failing on high)" \
        "npm audit --audit-level=high" -- npm audit --audit-level=high ;;
    portal-typecheck)
      need_node_modules "$id" portal "ci.yml / Portal Build" "npm ci --no-audit --no-fund" || return
      run_step "$id" portal "ci.yml / Portal Build / Type-check (tsc --noEmit)" "npx tsc --noEmit -p ." -- npx tsc --noEmit -p . ;;
    portal-test)
      need_node_modules "$id" portal "ci.yml / Portal Build" "npm ci --no-audit --no-fund" || return
      run_step "$id" portal "ci.yml / Portal Build / Unit tests (node --test)" "npm test" -- npm test --silent ;;
    portal-lint)
      # Non-required in CI (continue-on-error, pre-existing errors came with the portal): a warning, never a block.
      # Fast mode lints only the changed portal files; --full / --ci run the CI command on the whole portal.
      if [[ ! -d "$REPO_ROOT/portal/node_modules" ]]; then
        echo; echo "==> [$id] skipped: portal/node_modules missing (Portal Lint is non-required in CI)"
        WARNED+=("$id: portal/node_modules missing; (cd portal && npm ci --no-audit --no-fund)")
        return
      fi
      if [[ "$MODE" != "fast" ]]; then
        SOFT_STEP=1 run_step "$id" portal "ci.yml / Portal Lint" "npm run lint" -- npm run lint --silent
        return
      fi
      local targets=() existing=() t
      mapfile -t targets < <(eslint_targets "${CHANGED[@]}")
      for t in "${targets[@]}"; do [[ -f "$REPO_ROOT/portal/$t" ]] && existing+=("$t"); done
      if [[ ${#existing[@]} -eq 0 ]]; then
        echo; echo "==> [$id] no changed portal file for ESLint"
        return
      fi
      SOFT_STEP=1 run_step "$id" portal "ci.yml / Portal Lint (changed files only)" "npx eslint ${existing[*]}" -- \
        npx eslint -- "${existing[@]}" ;;
    portal-audit)
      run_step "$id" portal "ci.yml / Security Audit / npm audit (portal, failing on high)" \
        "npm audit --audit-level=high" -- npm audit --audit-level=high ;;
    e2e-typecheck)
      need_node_modules "$id" e2e "ui-smoke-portal.yml (tsc)" "npm install --no-audit --no-fund --ignore-scripts" || return
      run_step "$id" e2e "ui-smoke-portal.yml / tsc --noEmit" "npx tsc --noEmit" -- npx tsc --noEmit ;;
    *) echo "pre-push: unknown check $id" >&2; FAILED+=("$id${SEP}.${SEP}unknown check${SEP}-") ;;
  esac
}

# ---------------------------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------------------------
MODE="fast"
PLAN_ONLY=0
FROM_HOOK=0
[[ "${AA_PREPUSH:-}" == "full" ]] && MODE="full"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) [[ "$MODE" != "ci" ]] && MODE="full" ;;
    --ci) MODE="ci" ;;
    --plan) PLAN_ONLY=1 ;;
    --from-hook) FROM_HOOK=1 ;;
    --self-test) self_test; exit $? ;;
    -h | --help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "pre-push: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# The checks read the working tree but the push sends HEAD: from the hook, an edit not in HEAD (a fix not committed
# yet) would let a broken commit through, so refuse before any check. Manual runs only print a note (below).
if [[ $FROM_HOOK -eq 1 && -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "pre-push: tracked files differ from HEAD; commit or stash them (git stash), or git push --no-verify" >&2
  exit 1
fi

BASE="${AA_PREPUSH_BASE:-origin/main}"
CHANGED=()
PLAN_INPUT=()
if [[ "$MODE" == "fast" ]]; then
  if ! git -C "$REPO_ROOT" rev-parse --verify -q "$BASE" >/dev/null; then
    echo "pre-push: $BASE not found (git fetch origin?), running every check"
    MODE="full"
  else
    # A failing diff (no merge base: shallow clone, rewritten main) must not look like an empty diff.
    diff_out="$(mktemp)"
    if changed_files "$REPO_ROOT" "$BASE" >"$diff_out"; then
      mapfile -d '' -t CHANGED <"$diff_out"
      PLAN_INPUT=("${CHANGED[@]}")
      deletes_or_renames "$REPO_ROOT" "$BASE" && PLAN_INPUT+=(":deleted-or-renamed")
      mapfile -t unit_all < <(unit_all_files "${CHANGED[@]}")
      if [[ ${#unit_all[@]} -gt 0 ]]; then
        WARNED+=("shared fixture/deps changed (${unit_all[*]}): CI runs the whole unit suite; run AA_PREPUSH=full git push to run it locally")
      fi
      for f in "${CHANGED[@]}"; do
        if [[ "$f" == api/migrations/* ]]; then
          WARNED+=("a migration changed: the catalogue guard, RLS and re-apply checks are CI Integration Tests (PostgreSQL), not run here")
          break
        fi
      done
    else
      echo "pre-push: cannot diff $BASE...HEAD, running every check"
      MODE="full"
    fi
    rm -f "$diff_out"
  fi
fi

mapfile -t CHECKS < <(plan_checks "$MODE" "${PLAN_INPUT[@]}")
echo "pre-push ($MODE mode): ${#CHANGED[@]} file(s) changed vs $BASE; checks: ${CHECKS[*]}"
[[ $PLAN_ONLY -eq 1 ]] && exit 0
if [[ $FROM_HOOK -eq 0 && "$MODE" == "fast" && -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "note: the working tree has uncommitted changes; the checks read the working tree, the push sends commits"
fi

API_PY="$(resolve_api_python || true)"
T_START=$(date +%s)
for c in "${CHECKS[@]}"; do run_check "$c"; done
ELAPSED=$(( $(date +%s) - T_START ))

echo
for w in "${WARNED[@]}"; do echo "warning: $w"; done
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "pre-push: all ${#CHECKS[@]} check(s) passed in ${ELAPSED}s ($MODE mode)"
  exit 0
fi
echo "pre-push: ${#FAILED[@]} check(s) FAILED in ${ELAPSED}s ($MODE mode). Re-run the failing command to see it:"
for f in "${FAILED[@]}"; do
  IFS=$SEP read -r id dir shown ci <<<"$f"
  echo "  - [$id] CI: $ci"
  echo "      (cd $dir && $shown)"
done
echo "$BYPASS_HINT"
exit 1
