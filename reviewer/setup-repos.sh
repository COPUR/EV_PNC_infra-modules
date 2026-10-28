#!/usr/bin/env bash
# One-time repository setup for the CI reviewer, run locally by a repository admin.
# For each repository it:
#   1. sets the Actions secret ANTHROPIC_API_KEY (read from the environment or a hidden prompt,
#      passed to gh on stdin; never written to disk, never on a command line),
#   2. turns on Settings > Actions > General > "Allow GitHub Actions to create and approve pull
#      requests" (workflow token stays read-only by default),
#   3. hands branch protection to one writer: auditor/setup-repos.sh when it is in this checkout
#      (it never replaces a protection, only extends its required checks, and requires
#      `review / verdict` where `.github/workflows/review.yml` is on the default branch), else
#      setup-merge-gate.sh for repositories whose review caller is on main. Both skip a
#      repository without the caller, so main never waits for a check that cannot run.
#
# Usage: reviewer/setup-repos.sh [OWNER/REPO ...]
#   No arguments: COPUR/EV_PNC_docs, COPUR/EV_PNC_infra-modules and every COPUR/EV_PnC_Consent_* repo.
# Options (environment):
#   ANTHROPIC_API_KEY=...  use this key instead of prompting
#   SKIP_SECRET=1          leave the secret as it is
#   SKIP_PROTECTION=1      do not touch branch protection
#   DRY_RUN=1              print what would change, change nothing
#   APPROVALS=0|1          approving reviews for a new protection (default 1: the reviewer approval)
#   YES=1, DRY_RUN=1       also apply to auditor/setup-repos.sh and setup-merge-gate.sh (inherited)
# Needs: gh logged in as an admin of every listed repository (gh auth status).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# One writer for branch protection: auditor/setup-repos.sh when it is here (it extends an existing protection
# and adds `review / verdict` itself); otherwise setup-merge-gate.sh, which also only extends.
auditor_setup=""
[ -x "$here/../auditor/setup-repos.sh" ] && auditor_setup="$here/../auditor/setup-repos.sh"
owner="${OWNER:-COPUR}"

# Portable to the bash 3.2 that ships with macOS: no mapfile, no empty-array expansion under set -u.
repos=()
if [ "$#" -gt 0 ]; then
  repos=("$@")
else
  while IFS= read -r r; do [ -n "$r" ] && repos+=("$r"); done < <(
    { echo "$owner/EV_PNC_docs"; echo "$owner/EV_PNC_infra-modules"
      gh repo list "$owner" --limit 200 --json nameWithOwner --jq '.[].nameWithOwner' | grep "/EV_PnC_Consent_" || true
    } | sort -u)
fi
[ "${#repos[@]}" -gt 0 ] || { echo "no repositories found" >&2; exit 1; }

echo "repositories:"; printf '  %s\n' "${repos[@]}"

run() { if [ -n "${DRY_RUN:-}" ]; then echo "  [dry-run] $*"; else "$@"; fi; }

if [ -z "${SKIP_SECRET:-}" ] && [ -z "${DRY_RUN:-}" ]; then
  key="${ANTHROPIC_API_KEY:-}"
  if [ -z "$key" ]; then
    read -r -s -p "ANTHROPIC_API_KEY (input hidden): " key; echo
  fi
  [ -n "$key" ] || { echo "empty key" >&2; exit 1; }
fi

on_main() { gh api "repos/$1/contents/$2?ref=main" --silent >/dev/null 2>&1; }

failed=()
for repo in "${repos[@]}"; do
  echo "== $repo"
  if ! gh api "repos/$repo" --jq .permissions.admin 2>/dev/null | grep -qx true; then
    echo "  not an admin here (or repository missing); skipped"; failed+=("$repo"); continue
  fi

  if [ -z "${SKIP_SECRET:-}" ]; then
    if [ -n "${DRY_RUN:-}" ]; then echo "  [dry-run] gh secret set ANTHROPIC_API_KEY --repo $repo"
    else printf '%s' "$key" | gh secret set ANTHROPIC_API_KEY --repo "$repo" && echo "  secret ANTHROPIC_API_KEY set"; fi
  fi

  run gh api -X PUT "repos/$repo/actions/permissions/workflow" \
    -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true --silent \
    && echo "  Actions may approve pull requests (default token read-only)"

  if [ -n "${SKIP_PROTECTION:-}" ]; then
    echo "  branch protection skipped (SKIP_PROTECTION)"
  elif [ -n "$auditor_setup" ]; then
    : # done once for all repositories by auditor/setup-repos.sh after this loop
  elif on_main "$repo" .github/workflows/review.yml; then
    extra=()
    if on_main "$repo" .github/workflows/auditor.yml; then extra=("audit / gitleaks" "audit / repository audit"); fi
    APPROVALS="${APPROVALS:-1}" "$here/setup-merge-gate.sh" "$repo" ${extra[@]+"${extra[@]}"}
  else
    echo "  no .github/workflows/review.yml on main yet: branch protection skipped; merge the caller PR, then re-run"
  fi
done

unset key
if [ -z "${SKIP_PROTECTION:-}" ] && [ -n "$auditor_setup" ]; then
  echo "== branch protection: auditor/setup-repos.sh (adds review / verdict where review.yml is on main)"
  APPROVALS="${APPROVALS:-1}" "$auditor_setup" "${repos[@]}" || failed+=("branch protection")
fi
if [ "${#failed[@]}" -gt 0 ]; then echo "not set up: ${failed[*]}" >&2; exit 1; fi
echo "done"
