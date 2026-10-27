#!/usr/bin/env bash
# Turn on the merge gate for one repository: protect main so a PR merges only when the
# CI reviewer approved its head commit and every required check is green, and allow
# GitHub auto-merge so an approved PR merges itself.
#
# Usage: reviewer/setup-merge-gate.sh OWNER/REPO [extra required check ...]
#   e.g. reviewer/setup-merge-gate.sh COPUR/EV_PNC_docs "audit / gitleaks" "audit / repository audit"
# Needs: gh logged in as a repository admin.
# APPROVALS=1 (default) also requires the reviewer's approving review; this needs
# Settings > Actions > General > "Allow GitHub Actions to create and approve pull requests".
# APPROVALS=0 relies on the required `review / verdict` check alone.
#
# Admins are not forced through the gate (enforce_admins=false) so that the owner can still
# merge changes to the gate itself, which the reviewer refuses to approve. Claude never uses
# that bypass: it merges only through auto-merge, which waits for the gate.
set -euo pipefail

repo="${1:?usage: $0 OWNER/REPO [extra required check ...]}"
shift
checks=("review / verdict" "$@")

# GitHub Actions app id: the required checks must come from Actions, not from a status
# anyone with write access could post.
actions_app_id=15368
approvals="${APPROVALS:-1}"
if [ "$approvals" -gt 0 ]; then
  reviews_json="{\"required_approving_review_count\": $approvals, \"dismiss_stale_reviews\": true}"
else
  reviews_json=null
fi
contexts_json=$(printf '%s\n' "${checks[@]}" |
  python3 -c 'import json,sys; print(json.dumps([{"context": c, "app_id": int(sys.argv[1])} for c in sys.stdin.read().splitlines() if c]))' "$actions_app_id")

gh api -X PATCH "repos/$repo" -F allow_auto_merge=true -F allow_squash_merge=true >/dev/null

gh api -X PUT "repos/$repo/branches/main/protection" --input - >/dev/null <<EOF
{
  "required_status_checks": {"strict": true, "checks": $contexts_json},
  "enforce_admins": false,
  "required_pull_request_reviews": $reviews_json,
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": false
}
EOF

echo "merge gate on for $repo: required checks: ${checks[*]}"
