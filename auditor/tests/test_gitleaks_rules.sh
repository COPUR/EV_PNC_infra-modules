#!/usr/bin/env bash
# Asserts the project gitleaks rules fire on synthetic leaks and stay quiet on placeholders.
# The "secrets" are assembled at run time so this file itself never trips a scan.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runner="$here/../run-gitleaks.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
git init -q
git config user.email ci@example.invalid
git config user.name ci
cp "$here/../templates/gitleaks.toml" .gitleaks.toml
echo "# demo" > README.md
git add -A && git commit -qm "base"

fail=0
expect_rules() { # expected rule ids (space separated), sorted compare
  local got
  got="$(python3 -c 'import json;print(" ".join(sorted({r["RuleID"] for r in json.load(open("gitleaks-report.json"))})))')"
  if [ "$got" != "$1" ]; then echo "FAIL: expected [$1] got [$got]"; fail=1; else echo "ok: [$got]"; fi
}

echo "== placeholders and published test values must pass"
{
  echo "card_number = \"4111$(printf '1111')11111111\""
  echo "Authorization: Token <your-token>"
  echo "Authorization: Token \${OCPI_TOKEN}"
  echo "TCKN is an 11 digit number"
} > ok.txt
git add -A && git commit -qm "placeholders"
GITLEAKS_LOG_OPTS="HEAD~1..HEAD" "$runner" >/dev/null 2>&1 || { echo "FAIL: placeholders flagged"; fail=1; }

echo "== synthetic leaks must be found"
{
  echo "tckn: 1234567$(printf '8901')"
  echo "card_number = \"4929$(printf '1234')12341234\""
  echo "Authorization: Token Zm9vYmFy$(printf 'YmF6cXV4MTIzNDU2')Nzg5MA=="
  echo "sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username=\"a\" password=\"hunter2$(printf 'hunter2')\";"
} > bad.txt
git add -A && git commit -qm "leaks"
if GITLEAKS_LOG_OPTS="HEAD~1..HEAD" "$runner" >/dev/null 2>&1; then echo "FAIL: leaks not detected"; fail=1; fi
expect_rules "kafka-jaas-password ocpi-authorization-token payment-card-pan tr-national-id"

echo "== repo allowlist still applies to project rules"
printf '\n[[allowlists]]\ndescription = "test"\npaths = [%s]\n' "'''bad\\.txt'''" >> .gitleaks.toml
git add -A && git commit -qm "allow"
GITLEAKS_LOG_OPTS="HEAD~1..HEAD" "$runner" >/dev/null 2>&1 || { echo "FAIL: allowlist ignored"; fail=1; }
exit $fail
