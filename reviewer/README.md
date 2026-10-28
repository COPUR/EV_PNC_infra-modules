# EV_PNC CI review agent

A reusable GitHub Actions workflow, `.github/workflows/ci-reviewer.yml`, that reviews every pull request in the
EV_PNC and EV_PnC_Consent repositories and approves it or requests changes. Its job, `review / verdict`, is a
required check on `main`. A PR merges, by GitHub auto-merge, only when that check is green on the PR's head commit
and every other required check (build, tests, contract tests, gitleaks) is green too.

The reviewer runs under the GitHub Actions identity, never as the PR author, so the author cannot approve their own
change (separation of duties). Claude, which writes most PRs here, only turns on auto-merge; it never merges past
the gate.

## How a PR is judged

The first gate that fails decides; the cheap ones run before any model call.

| # | Gate | Verdict |
|---|---|---|
| 1 | PR is a draft | not reviewed yet (check fails until it is marked ready) |
| 2 | Title is not a conventional commit (`type(scope): subject`), or lacks the requirement ID set by `req-id-pattern` | changes requested |
| 3 | PR touches a gate file: `.github/**`, `CODEOWNERS`, `.gitleaks.toml`, `.auditor-allow`, `reviewer/**`, `auditor/**`, plus `protected-paths` | needs a human: the owner reviews and merges |
| 4 | Diff larger than `max-diff-chars` (400k by default) | needs a human (the diff is never truncated) |
| 5 | Claude (`claude-opus-5-5`, effort `high`) reviews the diff: correctness, security and KVKK/PCI, design smells, tests, docs consistency | approve only with no blocker or major finding |

Rules the model cannot override: any blocker or major finding means changes requested, whatever verdict the model
returned; any error (API, refusal, timeout, bad output) means changes requested. The PR text and diff are passed as
untrusted data, and the answer is constrained to a JSON schema.

Each run posts one review on the head commit: `APPROVE`, `REQUEST_CHANGES`, or a comment for "needs a human". A new
push re-runs the review; with `dismiss_stale_reviews` the old approval no longer counts.

## Adding it to a repository

1. Copy `templates/review-caller.yml` to `.github/workflows/review.yml`. Replace `__HUB_SHA__` (both places) with the
   commit of this repository to pin, and `__REQ_ID_PATTERN__` with `RC-[0-9]+` in EV_PnC_Consent_* repositories or an
   empty string elsewhere.
2. Add the repository secret `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions).
3. Settings → Actions → General → Workflow permissions: tick **Allow GitHub Actions to create and approve pull
   requests**, so the reviewer's approval is a real approving review.
4. Merge the caller PR yourself (it touches `.github/**`, so the reviewer will not approve it).
5. Run `reviewer/setup-merge-gate.sh OWNER/REPO [other required checks]` as an admin. It allows auto-merge and
   protects `main`: required checks from GitHub Actions only, one approving review, stale approvals dismissed,
   branch up to date, linear history, no force pushes. Set `APPROVALS=0` if you skipped step 3.

Steps 2, 3 and 5 for several repositories at once: `reviewer/setup-repos.sh [OWNER/REPO ...]` (no arguments:
EV_PNC_docs, EV_PNC_infra-modules and every EV_PnC_Consent_* repository). It reads the key from `ANTHROPIC_API_KEY` or a
hidden prompt and hands it to `gh secret set` on stdin, ticks the Actions approval setting, and protects `main` only
where `review.yml` is already on `main` (re-run it after merging the caller PRs). `DRY_RUN=1` shows what it would do.

## Limits

- On a personal account there are no org-level required workflows, so a PR could edit its own `review.yml`. That is
  why `.github/**` is a gate path, and why Claude does not enable auto-merge on PRs that touch gate files.
- Admins can still bypass protection (`enforce_admins` is off) so the owner can merge gate changes. Claude never does.
- One model call per push to a ready PR; drafts cost nothing.

## Versioning

Callers pin this repository by full commit SHA in `uses:` and `reviewer-ref:` (same commit). To roll out a change,
merge it here and bump the pin in each caller. The Anthropic SDK is pinned with hashes in `requirements.txt`
(regenerate with `pip-compile --generate-hashes requirements.in`).

## Tests

`python3 -m unittest discover -s reviewer/tests`. The `reviewer-tests` workflow runs them and checks that the
requirements install with hashes.
