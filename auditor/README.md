# EV_PNC CI auditor

One reusable GitHub Actions workflow, `.github/workflows/ci-auditor.yml`, audits every EV_PNC and
EV_PnC_Consent repository on **every push and pull request**, plus a weekly full-history rescan.
Repositories call it with the three-line caller in `templates/auditor-caller.yml`; the logic lives only here.

## What it checks

| Job | Check | Fails the build |
|---|---|---|
| `gitleaks` | Default gitleaks rules + project rules (`gitleaks-project-rules.toml`) on the pushed range, the PR's commits, the branch's own history (first push of a branch), or every branch (schedule, dispatch). Reports are redacted. | any finding |
| `repository audit` | `README.md`, `LICENSE` (public repos), `.gitleaks.toml` present; `CODEOWNERS`, `.gitignore` (warning) | missing required file |
| | `.gitleaks.toml` keeps the default rules on and has no repo-wide `.*` allowlist | yes |
| | Committed `.env`, `*.tfstate`, `*.tfvars`, keystores (`.p12 .pfx .jks`), SSH keys, kubeconfig, service-account JSON, `.pem`/`.key` holding a private key | yes |
| | Tracked file larger than `max-file-mb` (default 5) | yes |
| | Workflows: no `permissions:` (error), `pull_request_target` (error), attacker-controlled `${{ }}` inside `run:` (error), actions not pinned to a full SHA (warning, error with `strict-pinning: true`; `COPUR/*` callers are exempt) | see left |
| | Dockerfile without a pinned tag or non-root `USER`, Terraform ingress open to `0.0.0.0/0` | warning |
| | `actionlint` over `.github/workflows` | yes |

Project gitleaks rules are keyword-gated: Turkish national ID (TCKN, KVKK), payment card PAN (PCI-DSS, published test PANs allowed),
OCPI `Authorization: Token ...`, Kafka SASL JAAS password.

## Adding it to a repository

Copy `templates/gitleaks.toml` to `.gitleaks.toml` and `templates/auditor-caller.yml` to `.github/workflows/auditor.yml`
(replace `__PUBLIC__` with `true` or `false`, and `__HUB_SHA__` with the commit of this repository to pin, in both places). Reviewed false positives go into the repository's own `.gitleaks.toml` as a narrow
`[[allowlists]]` (path and/or regex, with a reason). Path-based audit findings are silenced with glob lines in
`.auditor-allow`. Both files are reviewed in the PR like any other change.

## Versioning

Callers pin this repository by full commit SHA, in `uses:` and in `auditor-ref:` (the same commit; the first picks the
workflow file, the second the scripts it runs). To roll out a change, merge it here and bump the pin in each caller.
A moving `v1` tag would let callers drop `auditor-ref`; tags cannot be pushed from the agent environment, so that is a manual step.
Gitleaks (`v8.30.1`) and actionlint (`v1.7.12`) are built with `go install` at a pinned version, so the module
checksums are verified by the Go sumdb. Third-party actions are pinned to full SHAs.

## Triage

A red `gitleaks` job means: treat the secret as leaked and rotate it first. Removing it from history is a second step.
Then use the `ev-pnc-squad-ci-auditor` agent (COPUR/agents, plugin `ev-pnc-squad`) to triage findings across repositories.

## Tests

`python3 -m unittest discover -s auditor/tests` and `bash auditor/tests/test_gitleaks_rules.sh` (needs `GITLEAKS_BIN`). CI runs both.
