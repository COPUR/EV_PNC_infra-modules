# EV_PNC CI auditor

One reusable GitHub Actions workflow, `.github/workflows/ci-auditor.yml`, audits every EV_PNC and
EV_PnC_Consent repository on **every push and pull request**, plus a weekly full-history rescan.
Repositories call it with the short caller in `templates/auditor-caller.yml`; the logic lives only here.
The same caller also runs the **ci-approver** gate (below), which merges a pull request once everything is green.

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

## The approver: automatic merge behind a second gate

`.github/workflows/ci-approver.yml` (policy in `approve.py`) runs on every pull request after the auditor. It reads the
PR through the API (it never checks out or runs the PR's code), publishes its verdict as the **`ci-approver` commit status**
and, on approve, merges with the chosen method (`squash` by default), pinned to the head commit it judged.

| Verdict | Status | When |
|---|---|---|
| approve | success, then merge | all checks green; no sensitive path; within size limits; trusted author; no open review thread |
| wait | pending | other checks still running; the job polls up to `wait-minutes` (max 20), then holds |
| hold | pending | a human must look: draft, stacked PR (base is not the default branch), fork, label `hold` / `do-not-merge` / `needs-human`, touches a sensitive path, more than 100 files or 3000 lines, a diff too large to inspect, deletes or disables a test, unresolved review thread |
| block | failure (job red) | a failed check (the latest run of each check counts, so an older cancelled or re-run attempt does not) or status, merge conflict, changes requested, conflict marker or private key in the diff |

Sensitive paths (always a human): `.github/workflows/*`, `.github/actions/*`, `CODEOWNERS`, `.github/dependabot.yml`,
`.gitleaks.toml`, `.auditor-allow`, `auditor/*`, `*.tf`, `*.tfvars`, `*Dockerfile*`, plus the caller's `extra-sensitive-paths`.
They are the control plane: a PR that edits CI or the secret scanner cannot approve itself.

**Content review.** The approver judges metadata and patterns, not what the change says. The CI review agent
(`reviewer/`, check `review / verdict`) reads the diff with Claude and approves or requests changes. Pass
`required-checks: "review / verdict"` (the caller template does) so the approver waits until that check has reported and
passed; without it, a review that has not started yet could be missed. Together: the reviewer approves, the approver merges.

GitHub does not let a PR author approve their own PR, and these PRs are authored by the repository owner, so the approval is a
status check, not a review. The caller job grants `contents: write`, `pull-requests: write`, `statuses: write`, `checks: read`.

What a workflow cannot do (repository owner, Settings > Branches): require the `ci-approver` and `audit / ...` checks on `main`
and CODEOWNERS review for `.github/`. Without branch protection a collaborator can still merge by hand, or edit the caller to
drop the gate; the approver only decides what merges *automatically*. Merges made with `GITHUB_TOKEN` do not start new workflow
runs on `main`, so the checks that ran on the PR are the ones that count.

## Adding it to a repository

Copy `templates/gitleaks.toml` to `.gitleaks.toml` and `templates/auditor-caller.yml` to `.github/workflows/auditor.yml`
(replace `__PUBLIC__` with `true` or `false`, and `__HUB_SHA__` with the commit of this repository to pin, in both places). Reviewed false positives go into the repository's own `.gitleaks.toml` as a narrow
`[[allowlists]]` (path and/or regex, with a reason). Path-based audit findings are silenced with glob lines in
`.auditor-allow`. Both files are reviewed in the PR like any other change.

## Versioning

Callers pin this repository by full commit SHA, in `uses:` and in `auditor-ref:` (the same commit; the first picks the
workflow file, the second the scripts it runs; the `approve` job does the same with `approver-ref`). To roll out a change, merge it here and bump the pin in each caller.
A moving `v1` tag would let callers drop `auditor-ref`; tags cannot be pushed from the agent environment, so that is a manual step.
Gitleaks (`v8.30.1`) and actionlint (`v1.7.12`) are built with `go install` at a pinned version, so the module
checksums are verified by the Go sumdb. Third-party actions are pinned to full SHAs.

## Triage

A red `gitleaks` job means: treat the secret as leaked and rotate it first. Removing it from history is a second step.
Then use the `ev-pnc-squad-ci-auditor` agent (COPUR/agents, plugin `ev-pnc-squad`) to triage findings across repositories.

## Tests

`python3 -m unittest discover -s auditor/tests` (auditor and approver policy) and `bash auditor/tests/test_gitleaks_rules.sh` (needs `GITLEAKS_BIN`). CI runs both.
