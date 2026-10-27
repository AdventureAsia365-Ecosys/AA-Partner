#!/usr/bin/env bash
# AA-993 V3 (ADR 0016 D3) — the post-deploy checks of the CRM web and the Content studio that a rows file cannot
# express (post_deploy_verify.sh rows read a status code and a JSON body only): where a signed-out visitor is
# redirected, and that the studio's Next assets are served. Run by .github/workflows/post-deploy-verify.yml after
# post_deploy_verify.sh for the apps crm-web and crm-portal. Red/green, no warnings.
#
# Usage: edge_verify.sh <crm-web|crm-portal>
#
# Checks (each gets VERIFY_TRIES tries, VERIFY_RETRY_DELAY seconds apart):
#   crm-web     signin-redirect   GET /pipeline without a session -> 307 to <BASE>/auth/refresh?next=%2Fpipeline
#   crm-portal  signin-page       GET /tenant-login -> 200 (the API-key sign-in page, on while PORTAL_API_KEY_SIGNIN is)
#               studio-assets     the first /portal-assets/_next/static/*.js of that page -> 200
#               signin-redirect   GET /portal/dashboard without a session -> 307 to
#                                 <BASE>/auth/refresh?next=%2Fportal%2Fdashboard (only the Fargate studio sends a
#                                 signed-out visitor to the CRM web's refresh; the Vercel copy sends /tenant-login)
#
# Env: BASE (https://<host>, no path); optional VERIFY_TRIES (3), VERIFY_RETRY_DELAY (10), GITHUB_STEP_SUMMARY.
# Needs bash, curl.
# Exit 0 = every check passed, 1 = at least one failed, 2 = bad usage.
set -uo pipefail

APP="${1:-}"
case "$APP" in
  crm-web|crm-portal) ;;
  *) echo "::error::usage: edge_verify.sh <crm-web|crm-portal> (got '${APP}')"; exit 2 ;;
esac
TRIES="${VERIFY_TRIES:-3}"
DELAY="${VERIFY_RETRY_DELAY:-10}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ROWS=()   # "name<TAB>PASS|FAIL<TAB>detail"
FAILED=0
DETAIL=""

record() {  # name, PASS|FAIL, detail
  ROWS+=("$1"$'\t'"$2"$'\t'"$3")
  if [ "$2" = "PASS" ]; then
    echo "edge [$1] OK: $3"
  else
    echo "::error::edge [$1] $3"
    FAILED=1
  fi
}

retry() {  # name, function, args... — the function sets DETAIL and returns 0 (pass) / 1 (fail)
  local name="$1"; shift
  local try
  for try in $(seq 1 "$TRIES"); do
    DETAIL=""
    if "$@"; then
      record "$name" PASS "$DETAIL"
      return 0
    fi
    [ "$try" -lt "$TRIES" ] && { echo "edge [$name] try $try/$TRIES failed: $DETAIL"; sleep "$DELAY"; }
  done
  record "$name" FAIL "$DETAIL (after $TRIES tries)"
  return 1
}

check_redirect() {  # path, expected Location
  local path="$1" want="$2" out code loc
  out=$(curl -s --max-time 20 -o /dev/null -w '%{http_code} %{redirect_url}' "${BASE}${path}" 2>/dev/null || true)
  code="${out%% *}"
  loc="${out#* }"
  [ "$loc" = "$out" ] && loc=""
  DETAIL="GET $path -> HTTP ${code:-none}, Location '${loc}'"
  if [ "$code" != "307" ] || [ "$loc" != "$want" ]; then
    DETAIL="$DETAIL, expected 307 to '$want'"
    return 1
  fi
}

check_page() {  # path -> $WORK/page.html
  local path="$1" code
  rm -f "$WORK/page.html"
  code=$(curl -s --max-time 20 -o "$WORK/page.html" -w '%{http_code}' "${BASE}${path}" 2>/dev/null || true)
  DETAIL="GET $path -> HTTP ${code:-none}"
  [ "$code" = "200" ] || { DETAIL="$DETAIL, expected 200"; return 1; }
}

check_asset() {  # the first studio Next asset linked from $WORK/page.html
  local asset code
  asset=$(grep -o '/portal-assets/_next/static/[^"]*\.js' "$WORK/page.html" 2>/dev/null | head -n 1)
  if [ -z "$asset" ]; then DETAIL="no /portal-assets/_next/static/*.js link in the sign-in page"; return 1; fi
  code=$(curl -s --max-time 20 -o /dev/null -w '%{http_code}' "${BASE}${asset}" 2>/dev/null || true)
  DETAIL="GET $asset -> HTTP ${code:-none}"
  [ "$code" = "200" ] || { DETAIL="$DETAIL, expected 200"; return 1; }
}

write_summary() {
  local out="${GITHUB_STEP_SUMMARY:-/dev/stdout}" verdict="PASS" row name status detail mark
  [ "$FAILED" = "0" ] || verdict="FAIL"
  {
    echo
    echo "### ${APP} edge checks: ${verdict}"
    echo
    echo "| Check | Result | Detail |"
    echo "|---|---|---|"
    for row in "${ROWS[@]}"; do
      IFS=$'\t' read -r name status detail <<< "$row"
      mark="PASS"; [ "$status" = "PASS" ] || mark="**FAIL**"
      echo "| ${name} | ${mark} | ${detail//|/\\|} |"
    done
  } >> "$out"
}

# ── main ──────────────────────────────────────────────────────────────────────────────────────────────────────────
if ! command -v curl >/dev/null 2>&1; then
  record tool-curl FAIL "curl is not installed"
elif ! [[ "${BASE:-}" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]]; then
  record inputs FAIL "BASE must be http(s)://<host> with no path, got '${BASE:-unset}'"
else
  BASE="${BASE%/}"
  case "$APP" in
    crm-web)
      retry signin-redirect check_redirect /pipeline "${BASE}/auth/refresh?next=%2Fpipeline"
      ;;
    crm-portal)
      if retry signin-page check_page /tenant-login; then
        retry studio-assets check_asset
      else
        record studio-assets FAIL "not run: the sign-in page did not load"
      fi
      retry signin-redirect check_redirect /portal/dashboard "${BASE}/auth/refresh?next=%2Fportal%2Fdashboard"
      ;;
  esac
fi

write_summary
if [ "$FAILED" = "0" ]; then
  echo "All edge checks passed."
  exit 0
fi
echo "Edge checks FAILED (see the table in the job summary)."
exit 1
