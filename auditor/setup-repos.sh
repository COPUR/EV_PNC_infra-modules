#!/usr/bin/env bash
# One-command repository setup for the CI auditor and ci-approver gates, run locally by a repository admin.
# For each repository it:
#   1. makes sure squash merge is allowed (the approver merges with squash) and that the default workflow
#      token is read-only (the callers grant their own job permissions),
#   2. creates the labels the approver honours: hold, do-not-merge, needs-human,
#   3. protects the default branch so a PR merges only with the required checks green: `audit / gitleaks`,
#      `audit / repository audit` (both from GitHub Actions) and the `ci-approver` commit status, plus
#      `review / verdict` where the CI review agent's caller is on the branch. It only does this where
#      `.github/workflows/auditor.yml` is already on the default branch, so the branch never waits for a
#      check that cannot run, and it skips private repositories unless INCLUDE_PRIVATE=1 (their Actions may
#      be blocked by billing),
#   4. lists the open PRs that predate the callers (their heads have no audit checks, so a required check would
#      show as "expected" forever) and, with UPDATE_STALE=1, brings their branches up to date with the base.
# It ends with one table: what each repository got.
#
# No secret is needed: both gates use the workflow's own GITHUB_TOKEN. (The review agent's ANTHROPIC_API_KEY is
# set by reviewer/setup-repos.sh.)
#
# An existing protection is never replaced: only the required-checks list is extended (union), everything else
# is left as it is. A missing protection is created without linear-history, without "up to date" (STRICT=1 adds
# it) and without required reviews (APPROVALS=1 adds one), because the approver merges with squash or merge
# commits and does not update branches.
#
# Usage: auditor/setup-repos.sh [OWNER/REPO ...]
#   No arguments: every non-fork, non-archived EV_PNC_* and EV_PnC_Consent_* repository of $OWNER (COPUR).
# Options (environment):
#   DRY_RUN=1          print every change with its payload, change nothing (reads still need `gh auth login`)
#   YES=1              do not ask for confirmation before a real run (needed when stdin is not a terminal)
#   SKIP_SETTINGS=1    leave merge and workflow-token settings alone
#   SKIP_LABELS=1      do not create labels
#   SKIP_PROTECTION=1  do not touch branch protection
#   INCLUDE_PRIVATE=1  also protect private repositories
#   UPDATE_STALE=1     update the branch of each open same-repo PR that predates the callers (merges the base
#                      into it; a ready, non-sensitive PR may then merge itself once its checks pass)
#   STRICT=1           new protection: require the branch to be up to date before merging
#   APPROVALS=N        new protection: require N approving reviews (needs the review agent; default 0)
#   ENFORCE_ADMINS=1   new protection: apply it to admins too (then gate-file PRs need a temporary exception)
#   OWNER=...          account to list when no repository is given (default COPUR)
# Needs: gh logged in as an admin of every listed repository (gh auth status). Runs on bash 3.2 (macOS).
set -euo pipefail

owner="${OWNER:-COPUR}"
actions_app_id=15368 # GitHub Actions: the audit checks are check runs created by it
dry="${DRY_RUN:-}"
last_payload=""

die() { echo "error: $*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "gh (GitHub CLI) is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not logged in; run: gh auth login"

repos=""
if [ "$#" -gt 0 ]; then
  for r in "$@"; do
    case "$r" in */*) ;; *) die "'$r' is not OWNER/REPO";; esac
    repos="$repos$r"$'\n'
  done
else
  listed="$(gh repo list "$owner" --limit 300 --source --no-archived --json nameWithOwner --jq '.[].nameWithOwner')" ||
    die "could not list the repositories of $owner"
  repos="$(printf '%s\n' "$listed" | grep -E "^[^/]+/(EV_PNC_|EV_PnC_Consent_)" | sort -u || true)"
fi
repos="$(printf '%s\n' "$repos" | sed '/^$/d')"
[ -n "$repos" ] || die "no repositories found"
count="$(printf '%s\n' "$repos" | wc -l | tr -d ' ')"

echo "repositories ($count):"; printf '%s\n' "$repos" | sed 's/^/  /'
if [ -n "$dry" ]; then
  echo "(dry run: nothing is changed)"
elif [ -z "${YES:-}" ]; then
  [ -t 0 ] || die "refusing to change $count repositories without confirmation: run it in a terminal, or set YES=1"
  echo "This changes merge settings, labels and branch protection on $count repositories."
  echo "Run it with DRY_RUN=1 first to read every change."
  read -r -p "Continue? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) die "cancelled; nothing was changed";; esac
fi

# apply "what" cmd args... : run a write, or only print it in a dry run. The payload, if any, comes on stdin.
apply() {
  local what="$1"; shift
  if [ -n "$dry" ]; then
    echo "  [dry-run] $what"
    return 0
  fi
  if "$@" >/dev/null; then echo "  $what: done"; else echo "  $what: FAILED" >&2; return 1; fi
}

# show_payload json : in a dry run print the payload, but only when it differs from the one printed last
show_payload() {
  [ -n "$dry" ] || return 0
  if [ "$1" = "$last_payload" ]; then echo "    payload: same as above"; else echo "    payload: $1"; last_payload="$1"; fi
}

# gh_get repo path jq-expression : read, quiet on failure (empty output and status 1)
gh_get() { gh api "repos/$1${2:+/$2}" --jq "$3" 2>/dev/null; }

# checks_json : stdin lines "context<TAB>app_id" (app_id may be empty) -> JSON array for the API
checks_json() {
  local out="" ctx app sep=""
  while IFS=$'\t' read -r ctx app; do
    [ -n "$ctx" ] || continue
    case "$ctx" in *[!A-Za-z0-9\ ./_:-]*) echo "refusing odd check name: $ctx" >&2; return 1;; esac
    case "$app" in ""|null) out="$out$sep{\"context\":\"$ctx\"}";; *[!0-9]*) return 1;; *) out="$out$sep{\"context\":\"$ctx\",\"app_id\":$app}";; esac
    sep=","
  done
  printf '[%s]' "$out"
}

setup_labels() { # repo
  local repo="$1" existing missing="" name color desc spec
  existing="$(gh label list --repo "$repo" --limit 200 --json name --jq '.[].name' 2>/dev/null || true)"
  for spec in "hold|d73a4a|Stops the ci-approver from merging this PR" \
              "do-not-merge|b60205|Stops the ci-approver from merging this PR" \
              "needs-human|fbca04|A person has to look before this merges"; do
    name="${spec%%|*}"
    printf '%s\n' "$existing" | grep -Fxq "$name" || missing="$missing $name"
  done
  if [ -z "$missing" ]; then r_labels="ok"; return 0; fi
  if [ -n "$dry" ]; then echo "  [dry-run] labels to create:${missing}"; r_labels="would add"; return 0; fi
  for spec in "hold|d73a4a|Stops the ci-approver from merging this PR" \
              "do-not-merge|b60205|Stops the ci-approver from merging this PR" \
              "needs-human|fbca04|A person has to look before this merges"; do
    name="${spec%%|*}"; color="${spec#*|}"; desc="${color#*|}"; color="${color%%|*}"
    case " $missing " in *" $name "*) gh label create "$name" --repo "$repo" --color "$color" --description "$desc" >/dev/null || { echo "  label '$name': FAILED" >&2; r_labels="FAILED"; return 1; };; esac
  done
  echo "  labels created:${missing}"; r_labels="added"
}

# stale_prs repo branch : open same-repo PRs on the base whose head has no audit check yet
stale_prs() {
  local repo="$1" br="$2" lines n sha has stale="" updated="" manual="" base
  r_stale="-"
  lines="$(gh_get "$repo" "pulls?state=open&per_page=100" '.[] | select(.head.repo.full_name == .base.repo.full_name) | "\(.number)\t\(.head.sha)\t\(.base.ref)"' || true)"
  while IFS=$'\t' read -r n sha base; do
    [ -n "$n" ] || continue
    [ "$base" = "$br" ] || continue
    has="$(gh_get "$repo" "commits/$sha/check-runs" '[.check_runs[].name] | any(. == "audit / gitleaks")' || true)"
    [ "$has" = true ] && continue
    stale="$stale #$n"
  done <<EOF
$lines
EOF
  [ -n "$stale" ] || return 0
  if [ -z "${UPDATE_STALE:-}" ]; then
    echo "  open PRs without the audit checks:${stale} (UPDATE_STALE=1 updates their branches)"
    r_stale="${stale# } (not updated)"; return 0
  fi
  for n in $stale; do
    n="${n#\#}"
    if [ -n "$dry" ]; then echo "  [dry-run] update branch of PR #$n"; updated="$updated #$n"; continue; fi
    if gh api -X PUT "repos/$repo/pulls/$n/update-branch" --silent >/dev/null 2>&1; then updated="$updated #$n"
    else manual="$manual #$n"; fi
  done
  [ -z "$updated" ] || echo "  branch updated:${updated}"
  [ -z "$manual" ] || echo "  could not update (conflict or no access), do it by hand:${manual}" >&2
  r_stale="$([ -n "$dry" ] && echo "would update" || echo "updated")${updated}${manual:+; by hand:$manual}"
}

setup_protection() { # repo branch
  local repo="$1" br="$2" file want="" existing="" strict="" combined="" ctx app payload reviews verified
  file="$(gh api -H "Accept: application/vnd.github.raw+json" "repos/$repo/contents/.github/workflows/auditor.yml?ref=$br" 2>/dev/null || true)"
  if [ -z "$file" ]; then
    echo "  branch protection skipped: no .github/workflows/auditor.yml on $br yet (merge the caller PR, then re-run)"
    r_prot="skipped: no caller"; return 0
  fi
  case "$file" in *ci-auditor.yml*) want="${want}audit / gitleaks"$'\t'"$actions_app_id"$'\n'"audit / repository audit"$'\t'"$actions_app_id"$'\n';; esac
  # `ci-approver` is a commit status (the API gives statuses no app id), so it is required from any source.
  # The job check `approve / ci-approver` is NOT required: it passes even when the verdict is hold.
  case "$file" in *ci-approver.yml*) want="${want}ci-approver"$'\t\n';; esac
  if gh_get "$repo" "contents/.github/workflows/review.yml?ref=$br" .name >/dev/null; then
    want="${want}review / verdict"$'\t'"$actions_app_id"$'\n'
  fi
  if [ -z "$want" ]; then
    echo "  branch protection skipped: auditor.yml calls neither the auditor nor the approver"
    r_prot="skipped: no gates"; return 0
  fi

  if gh api "repos/$repo/branches/$br/protection" >/dev/null 2>&1; then
    if ! strict="$(gh_get "$repo" "branches/$br/protection/required_status_checks" .strict)"; then
      echo "  $br is protected but has no required status checks; add these by hand (PATCH cannot create them):" >&2
      printf '%s' "$want" | tr '\t' ' ' | sed 's/ *$//; s/^/    /' >&2
      r_prot="FAILED: add by hand"; return 1
    fi
    existing="$(gh_get "$repo" "branches/$br/protection/required_status_checks" '.checks[] | "\(.context)\t\(.app_id // "")"')"
    combined="$existing"$'\n'
    while IFS=$'\t' read -r ctx app; do
      [ -n "$ctx" ] || continue
      if ! printf '%s\n' "$existing" | cut -f1 | grep -Fxq "$ctx"; then combined="$combined$ctx"$'\t'"$app"$'\n'; fi
    done <<EOF
$want
EOF
    payload="{\"strict\":$strict,\"checks\":$(printf '%s' "$combined" | checks_json)}"
    echo "  $br already protected: extending its required checks only (strict stays $strict)"
    echo "    required checks now: $(printf '%s' "$combined" | cut -f1 | sed '/^$/d' | paste -sd, - | sed 's/,/, /g')"
    show_payload "$payload"
    apply "required checks on $br" gh api -X PATCH "repos/$repo/branches/$br/protection/required_status_checks" --input - <<<"$payload" || { r_prot="FAILED"; return 1; }
    r_prot="$([ -n "$dry" ] && echo "would extend" || echo "extended")"
  else
    if [ "${APPROVALS:-0}" -gt 0 ] 2>/dev/null; then
      reviews="{\"required_approving_review_count\":${APPROVALS},\"dismiss_stale_reviews\":true}"
    else
      reviews=null
    fi
    payload="{\"required_status_checks\":{\"strict\":$([ -n "${STRICT:-}" ] && echo true || echo false),\"checks\":$(printf '%s' "$want" | checks_json)},\"enforce_admins\":$([ -n "${ENFORCE_ADMINS:-}" ] && echo true || echo false),\"required_pull_request_reviews\":$reviews,\"restrictions\":null,\"required_linear_history\":false,\"allow_force_pushes\":false,\"allow_deletions\":false,\"required_conversation_resolution\":false}"
    echo "  $br is not protected: creating protection (required checks: $(printf '%s' "$want" | cut -f1 | sed '/^$/d' | paste -sd, - | sed 's/,/, /g'))"
    show_payload "$payload"
    apply "protection on $br" gh api -X PUT "repos/$repo/branches/$br/protection" --input - <<<"$payload" || { r_prot="FAILED"; return 1; }
    r_prot="$([ -n "$dry" ] && echo "would create" || echo "created")"
  fi

  if [ -z "$dry" ]; then
    verified="$(gh_get "$repo" "branches/$br/protection/required_status_checks" '.checks[].context' || true)"
    while IFS=$'\t' read -r ctx app; do
      [ -n "$ctx" ] || continue
      printf '%s\n' "$verified" | grep -Fxq "$ctx" || { echo "  verify FAILED: '$ctx' is not a required check now" >&2; r_prot="FAILED: verify"; return 1; }
    done <<EOF
$want
EOF
    echo "  verified: every wanted check is required on $br"
  fi
}

setup_repo() {
  local repo="$1" meta br priv admin squash archived tokperm canapprove changed=""
  r_settings="-"; r_labels="-"; r_prot="-"; r_stale="-"
  meta="$(gh_get "$repo" "" '[.default_branch, (.private|tostring), (.permissions.admin|tostring), (.allow_squash_merge|tostring), (.archived|tostring)] | join(" ")')" ||
    { echo "  cannot read the repository (missing, or no access); skipped" >&2; r_settings="FAILED: no access"; return 1; }
  read -r br priv admin squash archived <<<"$meta"
  [ "$archived" != true ] || { echo "  archived; skipped"; r_settings="archived"; return 0; }
  [ "$admin" = true ] || { echo "  not an admin here; skipped" >&2; r_settings="FAILED: not admin"; return 1; }

  if [ -z "${SKIP_SETTINGS:-}" ]; then
    if [ "$squash" != true ]; then
      apply "allow squash merge" gh api -X PATCH "repos/$repo" -F allow_squash_merge=true --silent || { r_settings="FAILED"; return 1; }
      changed=1
    fi
    tokperm="$(gh_get "$repo" actions/permissions/workflow .default_workflow_permissions || true)"
    canapprove="$(gh_get "$repo" actions/permissions/workflow .can_approve_pull_request_reviews || true)"
    if [ "$tokperm" = write ]; then
      apply "default workflow token -> read" gh api -X PUT "repos/$repo/actions/permissions/workflow" -f default_workflow_permissions=read --silent || { r_settings="FAILED"; return 1; }
      changed=1
    elif [ -z "$tokperm" ]; then
      echo "  workflow token setting not readable; left alone"
    fi
    echo "  Actions may approve PRs: ${canapprove:-unknown} (only the review agent needs it; these gates do not)"
    r_settings="$([ -z "$changed" ] && echo ok || { [ -n "$dry" ] && echo "would change" || echo changed; })"
  else
    r_settings="skipped"
  fi

  if [ -z "${SKIP_LABELS:-}" ]; then setup_labels "$repo" || return 1; else r_labels="skipped"; fi

  if [ -n "${SKIP_PROTECTION:-}" ]; then
    echo "  branch protection skipped (SKIP_PROTECTION)"; r_prot="skipped"
  elif [ "$priv" = true ] && [ -z "${INCLUDE_PRIVATE:-}" ]; then
    echo "  branch protection skipped: private repository (set INCLUDE_PRIVATE=1 once its Actions can run)"
    r_prot="skipped: private"
  else
    setup_protection "$repo" "$br" || return 1
    stale_prs "$repo" "$br"
  fi
}

ok=""; failed=""; rows=""
while IFS= read -r repo <&3; do
  echo "== $repo"
  if setup_repo "$repo"; then ok="$ok $repo"; else failed="$failed $repo"; fi
  rows="$rows$(printf '%-36s %-14s %-10s %-22s %s' "${repo#*/}" "$r_settings" "$r_labels" "$r_prot" "$r_stale")"$'\n'
done 3<<EOF
$repos
EOF

echo
if [ -n "$dry" ]; then echo "Plan (nothing was changed):"; else echo "Result:"; fi
printf '%-36s %-14s %-10s %-22s %s\n' REPOSITORY SETTINGS LABELS PROTECTION "OPEN PRS WITHOUT CHECKS"
printf '%s' "$rows"
echo
if [ -n "$failed" ]; then echo "not set up:${failed}" >&2; exit 1; fi
if [ -n "$dry" ]; then echo "dry run done: nothing was changed"; else echo "done"; fi
