#!/usr/bin/env bash
# Turn on the merge gate for one repository: main accepts a PR only when the CI reviewer approved
# its head commit and every required check is green, and GitHub auto-merge is allowed.
#
# Usage: reviewer/setup-merge-gate.sh OWNER/REPO [extra required check ...]
#   e.g. reviewer/setup-merge-gate.sh COPUR/EV_PNC_docs "audit / gitleaks" "audit / repository audit"
# Needs: gh logged in as a repository admin.
#
# Same rules as auditor/setup-repos.sh, so either can run in any order:
# - An existing protection is never replaced. Only its required-checks list is extended (union with
#   `review / verdict` and the extra checks); strict, reviews, admins and history settings stay as they are.
# - A missing protection is created without "up to date" (STRICT=1 adds it: ci-approver does not update
#   branches), without linear history (the approver may merge with a merge commit), with APPROVALS approving
#   reviews (default 1: the reviewer's approval; needs Settings > Actions > General > "Allow GitHub Actions
#   to create and approve pull requests"), stale approvals dismissed, admins not enforced (so the owner
#   can merge gate changes, which the reviewer refuses to approve; Claude never uses that bypass).
# - Required checks must come from GitHub Actions (app id 15368), not from a status anyone with write access
#   could post.
# DRY_RUN=1 prints the payload and changes nothing.
set -Eeuo pipefail
if [ -n "${DEBUG:-}" ]; then PS4='+ ${BASH_SOURCE##*/}:${LINENO}: '; set -x; fi
trap 'rc=$?; echo "error: setup-merge-gate.sh line $LINENO exited $rc: $BASH_COMMAND" >&2' ERR

repo="${1:?usage: $0 OWNER/REPO [extra required check ...]}"
shift
actions_app_id=15368
dry="${DRY_RUN:-}"

wanted=$(printf '%s\n' "review / verdict" "$@")

write() { # method path payload
  if [ -n "$dry" ]; then echo "  [dry-run] gh api -X $1 $2 <<< $3"; else gh api -X "$1" "$2" --input - >/dev/null <<<"$3"; fi
}

if [ -n "$dry" ]; then echo "  [dry-run] gh api -X PATCH repos/$repo -F allow_auto_merge=true -F allow_squash_merge=true"
else gh api -X PATCH "repos/$repo" -F allow_auto_merge=true -F allow_squash_merge=true >/dev/null; fi

if gh api "repos/$repo/branches/main/protection" >/dev/null 2>&1; then
  current=$(gh api "repos/$repo/branches/main/protection/required_status_checks" 2>/dev/null) || {
    echo "main is protected but has no required status checks; add these by hand (PATCH cannot create them):" >&2
    printf '  %s\n' "review / verdict" "$@" >&2; exit 1; }
  payload=$(python3 -c '
import json, sys
cur = json.loads(sys.argv[1] or "{}"); app = int(sys.argv[2])
checks = [{"context": c["context"], **({"app_id": c["app_id"]} if c.get("app_id") else {})} for c in cur.get("checks", [])]
have = {c["context"] for c in checks}
checks += [{"context": w, "app_id": app} for w in sys.argv[3].splitlines() if w and w not in have]
print(json.dumps({"strict": cur.get("strict", False), "checks": checks}))' "$current" "$actions_app_id" "$wanted")
  write PATCH "repos/$repo/branches/main/protection/required_status_checks" "$payload"
  echo "merge gate on for $repo: main already protected, required checks extended"
else
  approvals="${APPROVALS:-1}"
  payload=$(python3 -c '
import json, sys
app, approvals, strict = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1"
checks = [{"context": w, "app_id": app} for w in sys.argv[4].splitlines() if w]
print(json.dumps({
  "required_status_checks": {"strict": strict, "checks": checks},
  "enforce_admins": False,
  "required_pull_request_reviews": {"required_approving_review_count": approvals, "dismiss_stale_reviews": True} if approvals > 0 else None,
  "restrictions": None, "required_linear_history": False,
  "allow_force_pushes": False, "allow_deletions": False, "required_conversation_resolution": False}))' \
    "$actions_app_id" "$approvals" "${STRICT:-}" "$wanted")
  write PUT "repos/$repo/branches/main/protection" "$payload"
  echo "merge gate on for $repo: main protected, required checks: $(printf '%s' "$wanted" | paste -sd, -)"
fi
