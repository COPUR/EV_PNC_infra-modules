#!/usr/bin/env python3
"""Change rules the reusable packaging workflow enforces on a pull request (card M-02, stdlib only).

  deps   Every cross-repo dependency a repo declares in its manifests must be declared in components.csv.
         Adding a dependency therefore means adding it to components.csv (the caller then moves its pinned
         auditor-ref to the commit that holds it, so the two changes sit in one reviewable diff).
  model  A change to the canonical model's schema needs a semver bump of the right size and an ADR.

Usage:
  change_rules.py deps  --components packaging/components.csv --repo EV_PNC_simulator [--root .]
  change_rules.py model --base origin/main [--root .] [--adr-dir ../docs/docs/adr]

Exit code 0 when the rules hold, 1 when a rule is broken (each finding is one line), 2 on bad input.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from packaging_check import dependencies, read_components  # noqa: E402

# ---- deps ------------------------------------------------------------------------------------------------------

MANIFESTS = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "libs.versions.toml", "pom.xml",
             "go.mod", "pyproject.toml", "requirements*.txt", "package.json", "contracts.lock", "*.lock")
SKIP_DIRS = {"node_modules", "build", ".gradle", ".contracts", ".venv", ".git", "vendor", "dist", ".kotlin"}
REPO_REF = re.compile(r"COPUR/(EV_PN[Cc]_[A-Za-z0-9_-]+)", re.IGNORECASE)
MAVEN_REF = re.compile(r"com\.evpnc:([A-Za-z0-9_.-]+)")
PACKAGE_REF = re.compile(r"\bev[-_]pnc[-_]([A-Za-z0-9_-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    target: str  # the component or repo that is depended on
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.message}"


def manifest_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if any(fnmatch.fnmatch(f, pat) for pat in MANIFESTS) and f != "uv.lock":
                out.append(Path(dirpath) / f)
    return sorted(out)


def check_deps(components_text: str, repo: str, root: Path) -> tuple[list[Finding], list[str]]:
    """(findings, notes). A finding is a dependency used in a manifest but missing from components.csv."""
    comp = read_components(components_text)
    deps = dependencies(comp)
    repo_of = {c: r["repo"] for c, r in comp.items()}
    lower = {r.lower(): r for r in set(repo_of.values())}
    own = [c for c, r in repo_of.items() if r.lower() == repo.lower()]
    if not own:
        return [], [f"{repo} has no component in components.csv (infra, docs and specs-only repos are not measured)"]
    declared = {d for c in own for d in deps[c]}
    declared_repos = {repo_of[d].lower() for d in declared}
    names = {c.lower(): c for c in comp}
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for path in manifest_files(root):
        rel = str(path.relative_to(root))
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue
            for m in REPO_REF.finditer(line):
                target = lower.get(m.group(1).lower())
                if target and target.lower() != repo.lower() and target.lower() not in declared_repos and (rel, target) not in seen:
                    seen.add((rel, target))
                    findings.append(Finding(rel, n, target, f"depends on {target}, but no component of {repo} lists a component of {target} in components.csv"))
            names_found = [(MAVEN_REF, m.group(1)) for m in MAVEN_REF.finditer(line)]
            names_found += [(PACKAGE_REF, m.group(1)) for m in PACKAGE_REF.finditer(line)]
            for _, raw in names_found:
                comp_name = names.get(raw.lower().replace("_", "-"))
                if not comp_name or repo_of[comp_name].lower() == repo.lower() or comp_name in declared or (rel, comp_name) in seen:
                    continue
                seen.add((rel, comp_name))
                findings.append(Finding(rel, n, comp_name, f"depends on component {comp_name}, which no component of {repo} lists in components.csv"))
    return findings, []


# ---- model -----------------------------------------------------------------------------------------------------

LEVELS = {"patch": 0, "minor": 1, "major": 2}
PATCH_ONLY = {"title", "description", "$comment", "examples", "default", "$id", "$schema"}


def classify(old, new) -> str:
    """Smallest semver bump a schema change needs: major (removed or retyped, narrowed), minor (added), patch."""
    if old == new:
        return "none"
    level = "none"

    def bump(to: str) -> None:
        nonlocal level
        if level == "none" or LEVELS[to] > LEVELS[level]:
            level = to

    if isinstance(old, dict) and isinstance(new, dict):
        for key in old.keys() | new.keys():
            o, n = old.get(key), new.get(key)
            if o == n:
                continue
            if key.startswith("x-") or key in PATCH_ONLY:
                bump("patch")
            elif key in ("properties", "$defs"):
                o, n = o or {}, n or {}
                if set(o) - set(n):
                    bump("major")
                if set(n) - set(o):
                    bump("minor")
                for sub in set(o) & set(n):
                    c = classify(o[sub], n[sub])
                    if c != "none":
                        bump(c)
            elif key == "required":
                if set(n or []) - set(o or []):
                    bump("major")
                else:
                    bump("minor")
            elif key == "enum":
                if set(o or []) - set(n or []):
                    bump("major")
                else:
                    bump("minor")
            elif key == "maxLength" and o is not None and n is not None:
                bump("minor" if n > o else "major")
            elif key == "minLength" and o is not None and n is not None:
                bump("minor" if n < o else "major")
            elif key == "maxLength" and n is None:
                bump("minor")  # limit lifted
            elif key in ("items", "additionalProperties", "if", "then", "else") and isinstance(o, dict) and isinstance(n, dict):
                c = classify(o, n)
                if c != "none":
                    bump(c)
            else:
                bump("major")  # a type, $ref, format, pattern, const, oneOf... changed: treat as retyping
    else:
        bump("major")
    return level


def semver(text: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    if not m:
        raise ValueError(f"not a plain semver: {text!r}")
    return int(m[1]), int(m[2]), int(m[3])


def bumped_enough(base: str, head: str, level: str) -> bool:
    b, h = semver(base), semver(head)
    if level == "major":
        return h[0] > b[0]
    if level == "minor":
        return h[:2] > b[:2]
    return h > b


VERSION_FILES = {
    "generated/python/pyproject.toml": re.compile(r'^version\s*=\s*"([^"]+)"', re.M),
    "generated/kotlin/build.gradle.kts": re.compile(r'^version\s*=\s*"([^"]+)"', re.M),
}


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def git_show(root: Path, ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def check_model(root: Path, base: str, head: str = "HEAD", watched: tuple[str, ...] = ("schema/", "tools/ocpi-spec-pin.json"),
                adr_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """(problems, notes)."""
    changed = git(root, "diff", "--name-only", f"{base}...{head}").split()
    hits = [f for f in changed if any(f.startswith(w) for w in watched)]
    if not hits:
        return [], ["no change to the model's schema or its spec pin"]
    problems: list[str] = []
    notes = [f"model change: {', '.join(hits)}"]

    level = "patch"
    for f in hits:
        if not f.endswith(".json") or f.startswith("tools/"):
            if f.startswith("tools/") and LEVELS[level] < 1:
                level = "minor"  # a moved spec pin is at least minor
            continue
        old_text, new_text = git_show(root, base, f), git_show(root, head, f)
        if old_text is None:
            level = level if LEVELS[level] >= 1 else "minor"  # a new schema file
            continue
        if new_text is None:
            level = "major"
            continue
        c = classify(json.loads(old_text), json.loads(new_text))
        if c != "none" and LEVELS[c] > LEVELS[level]:
            level = c
    notes.append(f"the schema change needs at least a {level} version bump")

    heads = {}
    for f, rx in VERSION_FILES.items():
        text = git_show(root, head, f)
        if text is None:
            continue
        m = rx.search(text)
        if not m:
            problems.append(f"{f}: no `version = \"x.y.z\"` line")
            continue
        heads[f] = m.group(1)
    if len(set(heads.values())) > 1:
        problems.append(f"the generated packages disagree on the version: {heads}")
    head_version = next(iter(heads.values()), None)
    if head_version is None:
        problems.append("no version found in generated/python/pyproject.toml or generated/kotlin/build.gradle.kts")
    else:
        base_versions = set()
        for f, rx in VERSION_FILES.items():
            text = git_show(root, base, f)
            m = rx.search(text) if text else None
            if m:
                base_versions.add(m.group(1))
        if base_versions:
            base_version = max(base_versions, key=semver)
            try:
                if not bumped_enough(base_version, head_version, level):
                    problems.append(f"version is {head_version} (base {base_version}); this change needs a {level} bump")
            except ValueError as e:
                problems.append(str(e))
        else:
            notes.append("the base has no version yet, so no bump is required")
        problems += changelog_problems(root, head, head_version, adr_dir)
    return problems, notes


def changelog_problems(root: Path, head: str, version: str, adr_dir: Path | None) -> list[str]:
    text = git_show(root, head, "CHANGELOG.md")
    if text is None:
        return ["CHANGELOG.md is missing"]
    m = re.search(rf"^##\s+{re.escape(version)}\b.*?$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return [f"CHANGELOG.md has no section for {version}"]
    adrs = re.findall(r"ADR-(\d{4})", m.group(1))
    if not adrs:
        return [f"the {version} section of CHANGELOG.md names no ADR (write `ADR-NNNN`)"]
    if adr_dir is not None:
        missing = [a for a in adrs if not list(adr_dir.glob(f"{a}-*.md"))]
        if missing:
            return [f"ADR-{a} is named in CHANGELOG.md but {adr_dir} has no {a}-*.md" for a in missing]
    return []


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deps")
    d.add_argument("--components", required=True)
    d.add_argument("--repo", required=True, help="repository name without owner, as in components.csv")
    d.add_argument("--root", default=".")
    m = sub.add_parser("model")
    m.add_argument("--base", required=True, help="ref of the pull request's base, e.g. origin/main")
    m.add_argument("--head", default="HEAD")
    m.add_argument("--root", default=".")
    m.add_argument("--adr-dir", help="directory with the ADR files (a checkout of EV_PNC_docs docs/adr)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "deps":
            findings, notes = check_deps(Path(args.components).read_text(), args.repo, Path(args.root))
            for n in notes:
                print(f"note: {n}")
            for f in findings:
                print(f"error: {f}")
            if findings:
                print("fix: add the dependency to depends_on in packaging/components.csv (EV_PNC_infra-modules), then move this repo's auditor-ref to that commit")
            else:
                print(f"deps: every cross-repo dependency of {args.repo} is declared")
            return 1 if findings else 0
        problems, notes = check_model(Path(args.root), args.base, args.head, adr_dir=Path(args.adr_dir) if args.adr_dir else None)
        for n in notes:
            print(f"note: {n}")
        for p in problems:
            print(f"error: {p}")
        if not problems:
            print("model: version bump and ADR rules hold")
        return 1 if problems else 0
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"change_rules: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
