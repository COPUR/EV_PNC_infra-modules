#!/usr/bin/env bash
# Tests auditor/setup-repos.sh against a stub `gh` that serves canned answers and logs every call.
# No network, no real repository is touched.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/../setup-repos.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/bin"
log="$work/log"

cat > "$work/bin/gh" <<'STUB'
#!/usr/bin/env bash
# stub gh: answers by repository; logs "METHOD path" and any stdin body
args="$*"
method=GET; path=""; jq=""; input=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    auth) exit 0;;
    repo) printf '%s\n' COPUR/EV_PNC_docs COPUR/EV_PnC_Consent_registry COPUR/EV_PNC_infra COPUR/EV_PNC_telemetry COPUR/EV_PNC_simulator COPUR/EV_PNC_hub-gateway; exit 0;;
    label)
      if [ "$2" = list ]; then echo needs-human; exit 0; fi
      echo "LABEL $args" >> "$STUB_LOG"; exit 0;;
    api) shift; continue;;
    -X) method="$2"; shift;;
    -H|-f|-F) shift;;
    --jq) jq="$2"; shift;;
    --input) input=1; shift;;
    --silent) ;;
    repos/*) path="$1";;
  esac
  shift
done
body=""; [ "$input" = 1 ] && body="$(cat)"
echo "$method $path" >> "$STUB_LOG"
[ -z "$body" ] || echo "BODY $body" >> "$STUB_LOG"
repo="${path#repos/}"; repo="${repo%%/contents*}"; repo="${repo%%/branches*}"; repo="${repo%%/actions*}"; repo="${repo%%/pulls*}"; repo="${repo%%/commits*}"
[ "$method" = GET ] || exit 0
auditor_yml='jobs: {audit: {uses: COPUR/EV_PNC_infra-modules/.github/workflows/ci-auditor.yml@x}, approve: {uses: COPUR/EV_PNC_infra-modules/.github/workflows/ci-approver.yml@x}}'
case "$path" in
  repos/*/contents/.github/workflows/auditor.yml*)
    case "$repo" in COPUR/EV_PNC_simulator) exit 1;; esac
    echo "$auditor_yml"; exit 0;;
  repos/*/contents/.github/workflows/review.yml*)
    [ "$repo" = COPUR/EV_PNC_hub-gateway ] && { echo review; exit 0; }; exit 1;;
  repos/*/branches/main/protection/required_status_checks)
    if grep -q "^PUT repos/$repo/branches/main/protection$" "$STUB_LOG"; then # protection was just created: serve it back
      case "$jq" in .strict) echo false;; *) printf 'audit / gitleaks\naudit / repository audit\nci-approver\n';; esac; exit 0
    fi
    [ "$repo" = COPUR/EV_PnC_Consent_registry ] || exit 1
    case "$jq" in
      .strict) echo true;;
      *'.context'*'.app_id'*) printf 'lint\t15368\nci-approver\t\n';;
      *) printf 'lint\naudit / gitleaks\naudit / repository audit\nci-approver\nreview / verdict\n';;
    esac; exit 0;;
  repos/*/branches/main/protection)
    [ "$repo" = COPUR/EV_PnC_Consent_registry ] && exit 0; exit 1;;
  repos/*/pulls\?state=open*)
    [ "$repo" = COPUR/EV_PNC_docs ] && printf '7\tsha7\tmain\n8\tsha8\tmain\n9\tsha9\tfeature\n'; exit 0;;
  repos/*/commits/*/check-runs)
    case "$path" in */sha7/*) echo true;; *) echo false;; esac; exit 0;;
  repos/*/actions/permissions/workflow)
    case "$jq" in *default*) echo write;; *) echo false;; esac; exit 0;;
  repos/*)
    case "$repo" in
      COPUR/EV_PNC_telemetry) echo "main false false true false";;
      COPUR/EV_PNC_infra) echo "main true true true false";;
      COPUR/EV_PnC_Consent_registry) echo "main false true false false";;
      *) echo "main false true true false";;
    esac; exit 0;;
esac
exit 1
STUB
chmod +x "$work/bin/gh"
export PATH="$work/bin:$PATH" STUB_LOG="$log"

fail=0
check() { # description, pattern, file
  if grep -Eq -- "$2" "$3"; then echo "ok   $1"; else echo "FAIL $1 (no match: $2)"; fail=1; fi
}
absent() {
  if grep -Eq -- "$2" "$3"; then echo "FAIL $1 (unexpected: $2)"; fail=1; else echo "ok   $1"; fi
}

# --- real run
: > "$log"
set +e; YES=1 bash "$script" > "$work/out" 2>&1; status=$?; set -e
if [ "$status" = 1 ]; then echo "ok   exit 1 when one repository is not set up"; else echo "FAIL exit status $status"; fail=1; fi
check "lists the repositories found" "COPUR/EV_PnC_Consent_registry" "$work/out"
check "new protection: PUT on a public repo with the callers" "^PUT repos/COPUR/EV_PNC_docs/branches/main/protection$" "$log"
check "new protection: all three checks, audit pinned to Actions" '"checks":\[\{"context":"audit / gitleaks","app_id":15368\},\{"context":"audit / repository audit","app_id":15368\},\{"context":"ci-approver"\}\]' "$log"
check "new protection: no linear history, not strict, no reviews, admins not enforced" '"strict":false.*"enforce_admins":false,"required_pull_request_reviews":null.*"required_linear_history":false' "$log"
absent "ci-approver status is not pinned to an app" '"context":"ci-approver","app_id"' "$log"
absent "the job check approve / ci-approver is never required" '"context":"approve' "$log"
check "existing protection is extended, not replaced" "^PATCH repos/COPUR/EV_PnC_Consent_registry/branches/main/protection/required_status_checks$" "$log"
absent "existing protection is never PUT" "^PUT repos/COPUR/EV_PnC_Consent_registry/branches/main/protection$" "$log"
check "existing checks are kept, strict stays, new ones added once" '"strict":true,"checks":\[\{"context":"lint","app_id":15368\},\{"context":"ci-approver"\},\{"context":"audit / gitleaks","app_id":15368\},\{"context":"audit / repository audit","app_id":15368\}\]' "$log"
check "review agent's check is added where its caller is on main" '"context":"review / verdict","app_id":15368' "$log"
absent "private repository: no protection call" "EV_PNC_infra/branches" "$log"
check "private repository: says why" "private repository" "$work/out"
absent "repository without the caller: no protection call" "EV_PNC_simulator/branches" "$log"
check "repository without the caller: says so" "no .github/workflows/auditor.yml on main yet" "$work/out"
check "not an admin: skipped and reported" "not set up:.*EV_PNC_telemetry" "$work/out"
absent "not an admin: no writes" "(PUT|PATCH|POST) repos/COPUR/EV_PNC_telemetry" "$log"
check "token default write -> read" "^PUT repos/COPUR/EV_PNC_docs/actions/permissions/workflow$" "$log"
check "squash merge enabled where it was off" "^PATCH repos/COPUR/EV_PnC_Consent_registry$" "$log"
check "missing labels are created, existing one is not" "LABEL label create hold" "$log"
check "labels are reported in one line" "labels created: hold do-not-merge" "$work/out"
check "stale PR (no audit check) is listed, not touched" "open PRs without the audit checks: #8 \(UPDATE_STALE=1" "$work/out"
absent "stale PR check ignores PRs with the check and other bases" "#7|#9" "$work/out"
absent "no branch update without UPDATE_STALE" "update-branch" "$log"
check "results table is printed" "^Result:" "$work/out"
check "results table: row per repository with the verdicts" "^EV_PNC_docs +changed +added +created +#8 \(not updated\)" "$work/out"
check "results table: private repository row" "^EV_PNC_infra +changed +added +skipped: private" "$work/out"
absent "existing label is not created again" "LABEL label create needs-human --repo COPUR/EV_PNC_docs" "$log"
absent "no secret is ever handled" "secret" "$log"

# --- dry run: reads only
: > "$log"
set +e; DRY_RUN=1 bash "$script" > "$work/dry" 2>&1; set -e
absent "dry run: no PUT/PATCH/POST" "^(PUT|PATCH|POST|DELETE) " "$log"
absent "dry run: no label created" "^LABEL" "$log"
check "dry run: prints the payload" "\[dry-run\] protection on main" "$work/dry"
check "dry run: prints the required checks" "payload: .*audit / gitleaks" "$work/dry"
check "dry run: says plan, not result" "^Plan \(nothing was changed\):" "$work/dry"
check "dry run: table says would" "^EV_PNC_docs +would change +would add +would create" "$work/dry"
absent "dry run: never claims it changed something" "(: done|^Result:|^done$)" "$work/dry"
set +e; DRY_RUN=1 bash "$script" COPUR/EV_PNC_docs COPUR/EV_PNC_docs > "$work/dry2" 2>&1; set -e
check "dry run: repeated payload is not printed twice" "payload: same as above" "$work/dry2"

# --- confirmation: a real run needs a terminal or YES=1
: > "$log"
set +e; bash "$script" COPUR/EV_PNC_docs < /dev/null > "$work/noyes" 2>&1; status=$?; set -e
if [ "$status" = 1 ]; then echo "ok   no tty and no YES: refuses"; else echo "FAIL confirmation exit status $status"; fail=1; fi
check "refusal says how to proceed" "without confirmation.*YES=1" "$work/noyes"
absent "refusal writes nothing" "^(PUT|PATCH|POST|LABEL)" "$log"
: > "$log"
set +e; DRY_RUN=1 bash "$script" COPUR/EV_PNC_docs < /dev/null > "$work/dryno" 2>&1; status=$?; set -e
if [ "$status" = 0 ]; then echo "ok   dry run needs no confirmation"; else echo "FAIL dry run exit status $status"; fail=1; fi

# --- UPDATE_STALE
: > "$log"
set +e; YES=1 UPDATE_STALE=1 SKIP_SETTINGS=1 SKIP_LABELS=1 bash "$script" COPUR/EV_PNC_docs > "$work/stale" 2>&1; set -e
check "UPDATE_STALE updates the stale PR's branch" "^PUT repos/COPUR/EV_PNC_docs/pulls/8/update-branch$" "$log"
absent "UPDATE_STALE leaves PRs that already have the checks" "pulls/7/update-branch" "$log"
absent "UPDATE_STALE leaves stacked PRs alone" "pulls/9/update-branch" "$log"
check "UPDATE_STALE reports it" "branch updated: #8" "$work/stale"
: > "$log"
set +e; DRY_RUN=1 UPDATE_STALE=1 SKIP_SETTINGS=1 SKIP_LABELS=1 bash "$script" COPUR/EV_PNC_docs > "$work/staledry" 2>&1; set -e
absent "UPDATE_STALE in a dry run changes nothing" "update-branch" "$log"
check "UPDATE_STALE dry run lists what it would update" "\[dry-run\] update branch of PR #8" "$work/staledry"

# --- options
: > "$log"
set +e; YES=1 SKIP_PROTECTION=1 SKIP_SETTINGS=1 SKIP_LABELS=1 bash "$script" COPUR/EV_PNC_docs > "$work/skip" 2>&1; set -e
check "options: single repository argument works" "^== COPUR/EV_PNC_docs$" "$work/skip"
absent "options: nothing written when everything is skipped" "^(PUT|PATCH|POST|LABEL)" "$log"
: > "$log"
set +e; YES=1 INCLUDE_PRIVATE=1 STRICT=1 APPROVALS=1 ENFORCE_ADMINS=1 bash "$script" COPUR/EV_PNC_infra > "$work/priv" 2>&1; set -e
check "INCLUDE_PRIVATE protects a private repository" "^PUT repos/COPUR/EV_PNC_infra/branches/main/protection$" "$log"
check "STRICT/APPROVALS/ENFORCE_ADMINS are honoured on new protection" '"strict":true.*"enforce_admins":true,"required_pull_request_reviews":\{"required_approving_review_count":1,"dismiss_stale_reviews":true\}' "$log"

if [ "$fail" = 0 ]; then echo "all setup-repos tests passed"; else echo "setup-repos tests FAILED"; exit 1; fi
