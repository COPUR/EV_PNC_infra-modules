#!/usr/bin/env python3
"""EV_PNC CI approver: decides whether a pull request may be merged without a human (stdlib + gh CLI).

The approver is the second gate after the auditor (gitleaks + repository audit). It reads the pull
request through the GitHub API (it never checks out or runs the PR's code), applies the merge policy
in `decide()`, publishes the verdict as the `ci-approver` commit status and, on APPROVE, merges.

Verdicts:
  approve  every gate is green and nothing needs a human; merge (status: success)
  wait     other checks are still running; poll, then decide again (status: pending)
  hold     a human must look: draft, sensitive path, big diff, unresolved thread (status: pending)
  block    something is wrong: failed check, conflict, changes requested (status: failure)

Exit code is 0 for approve, wait and hold; 1 for block, so a blocked pull request shows red.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

STATUS_CONTEXT = "ci-approver"

# Paths that change what CI, secret scanning, ownership or infrastructure does. A pull request
# that touches one of them always waits for a human, whoever wrote it.
SENSITIVE_DEFAULT = [
    ".github/workflows/*", ".github/actions/*", ".github/CODEOWNERS", "CODEOWNERS",
    ".github/dependabot.yml", ".gitleaks.toml", ".auditor-allow", "auditor/*",
    "*.tf", "*.tfvars", "*Dockerfile*",
]
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2")
TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)(test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(test|spec)\.[^/]+)$")
SKIP_RE = re.compile(
    r"@pytest\.mark\.(skip|xfail)|@unittest\.skip|\bxit\(|\bxdescribe\(|\bit\.skip\(|\bdescribe\.skip\("
    r"|@Disabled|@Ignore\b|\bt\.Skip\(|\bskipTest\("
)
CONFLICT_RE = re.compile(r"^(<{7}|>{7}) ")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")


@dataclass
class Policy:
    trusted_authors: tuple[str, ...] = ("COPUR",)
    max_files: int = 100
    max_lines: int = 3000
    sensitive: tuple[str, ...] = tuple(SENSITIVE_DEFAULT)
    hold_labels: tuple[str, ...] = ("hold", "do-not-merge", "needs-human")


@dataclass
class Facts:
    state: str = "open"
    merged: bool = False
    draft: bool = False
    head_sha: str = ""
    base_ref: str = "main"
    default_branch: str = "main"
    head_repo: str = ""
    base_repo: str = ""
    author: str = ""
    labels: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)  # filename, status, additions, deletions, patch
    review_states: dict[str, str] = field(default_factory=dict)  # reviewer -> latest meaningful state
    unresolved_threads: int = 0
    mergeable_state: str = "clean"
    checks: list[dict] = field(default_factory=list)  # name, status, conclusion
    statuses: list[dict] = field(default_factory=list)  # context, state


@dataclass
class Decision:
    verdict: str  # approve | wait | hold | block
    reasons: list[str]


def is_sensitive(path: str, patterns) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def inspect_files(files: list[dict], policy: Policy) -> tuple[list[str], list[str]]:
    """Return (hold reasons, block reasons) from the diff itself."""
    hold: list[str] = []
    block: list[str] = []
    sensitive = sorted(f["filename"] for f in files if is_sensitive(f["filename"], policy.sensitive))
    if sensitive:
        shown = ", ".join(f"`{p}`" for p in sensitive[:5]) + (f" and {len(sensitive) - 5} more" if len(sensitive) > 5 else "")
        hold.append(f"touches CI, secret-scanning, ownership or infrastructure files: {shown}")
    changed = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    if len(files) > policy.max_files:
        hold.append(f"{len(files)} files changed (limit {policy.max_files})")
    if changed > policy.max_lines:
        hold.append(f"{changed} lines changed (limit {policy.max_lines})")
    uninspected = [
        f["filename"] for f in files
        if f.get("patch") is None and f.get("additions", 0) + f.get("deletions", 0) > 0
        and not f["filename"].lower().endswith(BINARY_SUFFIXES)
    ]
    if uninspected:
        hold.append(f"{len(uninspected)} file(s) too large to inspect, for example `{uninspected[0]}`")
    for f in files:
        name = f["filename"]
        if f.get("status") == "removed" and TEST_PATH_RE.search(name):
            hold.append(f"removes test file `{name}`")
        for line in (f.get("patch") or "").splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            added = line[1:]
            if CONFLICT_RE.match(added):
                block.append(f"conflict marker left in `{name}`")
                break
            if PRIVATE_KEY_RE.search(added):
                block.append(f"private key added in `{name}`")
                break
            if TEST_PATH_RE.search(name) and SKIP_RE.search(added):
                hold.append(f"disables a test in `{name}`")
                break
    return hold, block


def decide(facts: Facts, policy: Policy) -> Decision:
    block: list[str] = []
    hold: list[str] = []
    wait: list[str] = []

    if facts.state != "open" or facts.merged:
        return Decision("hold", ["pull request is not open"])

    if facts.draft:
        hold.append("pull request is a draft (mark it ready for review so the approver can merge it)")
    if facts.base_ref != facts.default_branch:
        hold.append(f"base is `{facts.base_ref}`, not the default branch `{facts.default_branch}` (stacked pull request)")
    if facts.head_repo != facts.base_repo:
        hold.append("pull request comes from a fork")
    if facts.author not in policy.trusted_authors:
        hold.append(f"author `{facts.author}` is not in the trusted list")
    for label in facts.labels:
        if label in policy.hold_labels:
            hold.append(f"label `{label}` asks for a human")

    d_hold, d_block = inspect_files(facts.files, policy)
    hold += d_hold
    block += d_block

    for reviewer, state in sorted(facts.review_states.items()):
        if state == "CHANGES_REQUESTED":
            block.append(f"changes requested by `{reviewer}`")
    if facts.unresolved_threads:
        hold.append(f"{facts.unresolved_threads} unresolved review thread(s)")

    if facts.mergeable_state == "dirty":
        block.append("merge conflict with the base branch")
    elif facts.mergeable_state == "unknown":
        wait.append("GitHub has not computed mergeability yet")

    for c in facts.checks:
        if c.get("status") != "completed":
            wait.append(f"check `{c['name']}` is {c.get('status')}")
        elif c.get("conclusion") == "action_required":
            hold.append(f"check `{c['name']}` needs approval")
        elif c.get("conclusion") not in OK_CONCLUSIONS:
            block.append(f"check `{c['name']}` {c.get('conclusion')}")
    for s in facts.statuses:
        if s["context"] == STATUS_CONTEXT:
            continue
        if s["state"] in ("failure", "error"):
            block.append(f"status `{s['context']}` {s['state']}")
        elif s["state"] == "pending":
            wait.append(f"status `{s['context']}` is pending")
    if not facts.checks and not facts.statuses:
        wait.append("no checks reported on the head commit yet")

    if block:
        return Decision("block", block + hold)
    if hold:
        return Decision("hold", hold)
    if wait:
        return Decision("wait", wait)
    return Decision("approve", ["all gates green, no sensitive paths, within size limits"])


# ---- GitHub access (gh CLI) -------------------------------------------------------------------

def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def parse_stream(text: str) -> list:
    """Parse concatenated JSON values (what `gh api --paginate --jq` prints)."""
    dec = json.JSONDecoder()
    out, i = [], 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        out.append(obj)
    return out


def latest_review_states(reviews: list[dict]) -> dict[str, str]:
    states: dict[str, str] = {}
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        user = (r.get("user") or {}).get("login", "?")
        state = r.get("state", "")
        if state == "DISMISSED":
            states.pop(user, None)
        elif state in ("APPROVED", "CHANGES_REQUESTED"):
            states[user] = state
    return states


def gather(repo: str, pr: int, run_id: str) -> Facts:
    owner, name = repo.split("/", 1)
    p = json.loads(gh("api", f"repos/{repo}/pulls/{pr}"))
    sha = p["head"]["sha"]
    repo_meta = json.loads(gh("api", f"repos/{repo}"))
    files = parse_stream(gh("api", "--paginate", f"repos/{repo}/pulls/{pr}/files?per_page=100",
                            "--jq", ".[] | {filename,status,additions,deletions,patch}"))
    reviews = parse_stream(gh("api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews?per_page=100",
                              "--jq", ".[] | {user:{login:.user.login},state,submitted_at}"))
    runs = parse_stream(gh("api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                           "--jq", ".check_runs[] | {name,status,conclusion,details_url}"))
    # The approver's own job is still running while it decides; ignore unfinished checks of this run.
    own = f"/actions/runs/{run_id}/" if run_id else None
    checks = [c for c in runs if not (own and own in (c.get("details_url") or "") and c["status"] != "completed")]
    statuses = json.loads(gh("api", f"repos/{repo}/commits/{sha}/status")).get("statuses", [])
    query = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
             "{reviewThreads(first:100){nodes{isResolved isOutdated}}}}}")
    gql = json.loads(gh("api", "graphql", "-f", f"query={query}", "-f", f"o={owner}", "-f", f"r={name}", "-F", f"n={pr}"))
    threads = gql["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    return Facts(
        state=p["state"], merged=bool(p.get("merged")), draft=bool(p.get("draft")), head_sha=sha,
        base_ref=p["base"]["ref"], default_branch=repo_meta["default_branch"],
        head_repo=(p["head"].get("repo") or {}).get("full_name", ""), base_repo=p["base"]["repo"]["full_name"],
        author=p["user"]["login"], labels=[l["name"] for l in p.get("labels", [])],
        files=files, review_states=latest_review_states(reviews),
        unresolved_threads=sum(1 for t in threads if not t["isResolved"] and not t["isOutdated"]),
        mergeable_state=p.get("mergeable_state", "unknown"), checks=checks, statuses=statuses,
    )


def set_status(repo: str, sha: str, state: str, description: str, target_url: str) -> None:
    try:
        gh("api", "-X", "POST", f"repos/{repo}/statuses/{sha}", "-f", f"state={state}",
           "-f", f"context={STATUS_CONTEXT}", "-f", f"description={description[:140]}",
           *(["-f", f"target_url={target_url}"] if target_url else []))
    except subprocess.CalledProcessError as exc:
        print(f"::warning::could not set the {STATUS_CONTEXT} status: {exc.stderr.strip()[:200]}")


def merge(repo: str, pr: int, sha: str, method: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{pr}/merge", "-f", f"sha={sha}", "-f", f"merge_method={method}"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True, json.loads(proc.stdout).get("sha", "")
    return False, (proc.stdout or proc.stderr).strip()[:300]


def write_summary(decision: Decision, merged: str, note: str) -> None:
    lines = [f"### ci-approver: {decision.verdict.upper()}", ""]
    lines += [f"- {r}" for r in decision.reasons]
    if merged:
        lines += ["", f"Merged as `{merged}`."]
    if note:
        lines += ["", note]
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--sha", required=True, help="head commit this run was started for")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--merge", choices=("true", "false"), default="true")
    ap.add_argument("--merge-method", choices=("merge", "squash", "rebase"), default="squash")
    ap.add_argument("--wait-minutes", type=float, default=15)
    ap.add_argument("--trusted-authors", default="COPUR")
    ap.add_argument("--max-files", type=int, default=100)
    ap.add_argument("--max-lines", type=int, default=3000)
    ap.add_argument("--extra-sensitive", default="", help="space or comma separated path globs")
    ap.add_argument("--target-url", default="")
    args = ap.parse_args(argv)

    extra = tuple(p for p in re.split(r"[\s,]+", args.extra_sensitive) if p)
    policy = Policy(
        trusted_authors=tuple(a for a in re.split(r"[\s,]+", args.trusted_authors) if a),
        max_files=args.max_files, max_lines=args.max_lines, sensitive=tuple(SENSITIVE_DEFAULT) + extra,
    )
    deadline = time.monotonic() + min(args.wait_minutes, 20) * 60
    set_status(args.repo, args.sha, "pending", "evaluating", args.target_url)
    while True:
        facts = gather(args.repo, args.pr, args.run_id)
        if facts.head_sha != args.sha:
            decision = Decision("hold", ["head commit moved; a newer run will decide"])
            break
        decision = decide(facts, policy)
        if decision.verdict != "wait" or time.monotonic() >= deadline:
            break
        print("waiting: " + "; ".join(decision.reasons))
        time.sleep(30)
    if decision.verdict == "wait":
        decision = Decision("hold", ["timed out waiting: " + "; ".join(decision.reasons)])

    merged, note = "", ""
    if decision.verdict == "approve":
        set_status(args.repo, args.sha, "success", "approved by ci-approver", args.target_url)
        if args.merge == "true":
            ok, info = merge(args.repo, args.pr, args.sha, args.merge_method)
            if ok:
                merged = info
            else:
                decision = Decision("hold", [f"approved, but GitHub refused the merge: {info}"])
                set_status(args.repo, args.sha, "pending", "approved, merge refused", args.target_url)
        else:
            note = "Merging is switched off for this repository (`merge: false`)."
    elif decision.verdict == "block":
        set_status(args.repo, args.sha, "failure", decision.reasons[0], args.target_url)
    else:
        set_status(args.repo, args.sha, "pending", "held: " + decision.reasons[0], args.target_url)

    for r in decision.reasons:
        print(f"::{'error' if decision.verdict == 'block' else 'notice'} title=ci-approver {decision.verdict}::{r}")
    write_summary(decision, merged, note)
    return 1 if decision.verdict == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
