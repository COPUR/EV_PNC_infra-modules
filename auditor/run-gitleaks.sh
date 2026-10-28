#!/usr/bin/env bash
# Run gitleaks over the current repository with the project rules layered on the repo's own
# .gitleaks.toml. Env: GITLEAKS_LOG_OPTS (git log range; empty = full history), GITLEAKS_BIN.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_cfg="$PWD/.gitleaks.toml"
if [ ! -f "$repo_cfg" ]; then
  echo "::error file=.gitleaks.toml::missing .gitleaks.toml (see auditor/templates/gitleaks.toml)"
  exit 1
fi

cfg="$(mktemp)"
trap 'rm -f "$cfg"' EXIT
{
  printf '[extend]\npath = "%s"\n\n' "$repo_cfg"
  cat "$here/gitleaks-project-rules.toml"
} >"$cfg"

args=(git --config "$cfg" --redact --no-banner --verbose
      --report-format json --report-path gitleaks-report.json --exit-code 1)
if [ -n "${GITLEAKS_LOG_OPTS:-}" ]; then
  args+=(--log-opts "$GITLEAKS_LOG_OPTS")
fi
"${GITLEAKS_BIN:-gitleaks}" "${args[@]}"
