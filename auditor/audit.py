#!/usr/bin/env python3
"""EV_PNC repository auditor: repo-hygiene and CI-hygiene checks (stdlib only).

Runs against the git-tracked files of the current directory. Secrets are the
job of gitleaks (see ci-auditor.yml); this script covers what gitleaks does not:
committed key/state files, oversized blobs, weakened gitleaks config, and unsafe
GitHub Actions workflow patterns.

Exit code 1 when any finding has level "error".
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

FIRST_PARTY_OWNERS = ("copur/",)
LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")

# Names that should never be committed. Public certificates (.pem/.crt) are fine:
# the Plug&Charge PKI repos legitimately hold root certs, so .pem/.key files are
# inspected for a private-key header instead of being banned by name.
FORBIDDEN_NAMES = [
    ".env", ".env.*", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "*.tfstate", "*.tfstate.*", "*.tfvars", "*.auto.tfvars",
    "id_rsa", "id_rsa.*", "id_ed25519", "id_ed25519.*", "id_ecdsa", "id_ecdsa.*",
    "kubeconfig", "kubeconfig.*", "*.kubeconfig",
    "credentials.json", "service-account*.json", "serviceaccount*.json",
]
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".pub")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")

# Expression sources an outside contributor controls. Never put these in `run:`.
INJECTABLE_RE = re.compile(
    r"\$\{\{\s*(?:github\.head_ref"
    r"|github\.event\.(?:issue\.(?:title|body)"
    r"|pull_request\.(?:title|body|head\.(?:ref|label|repo\.default_branch))"
    r"|comment\.body|review\.body|review_comment\.body|discussion\.(?:title|body)"
    r"|head_commit\.(?:message|author\.(?:name|email))"
    r"|commits\[[^\]]*\]\.(?:message|author\.(?:name|email))"
    r"|pages\[[^\]]*\]\.page_name))"
)
USES_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class Finding:
    level: str  # error | warning
    check: str
    path: str
    line: int
    message: str


def git_files(root: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def read_text(root: str, rel: str) -> str:
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def load_allow(root: str) -> list[str]:
    """`.auditor-allow`: one path glob per line, `#` comments. Silences path-based checks."""
    pats = []
    for line in read_text(root, ".auditor-allow").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pats.append(line)
    return pats


def allowed(path: str, pats: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in pats)


def check_required(files, root, public, out):
    fset = set(files)
    if "README.md" not in fset:
        out.append(Finding("error", "required-files", "README.md", 0, "README.md is missing"))
    if public and not any(n in fset for n in LICENSE_NAMES):
        out.append(Finding("error", "required-files", "LICENSE", 0, "public repository has no LICENSE file"))
    if ".gitleaks.toml" not in fset:
        out.append(Finding("error", "required-files", ".gitleaks.toml", 0,
                           "no .gitleaks.toml (copy auditor/templates/gitleaks.toml from EV_PNC_infra-modules)"))
    if ".github/CODEOWNERS" not in fset and "CODEOWNERS" not in fset:
        out.append(Finding("warning", "required-files", ".github/CODEOWNERS", 0, "no CODEOWNERS file"))
    if ".gitignore" not in fset:
        out.append(Finding("warning", "required-files", ".gitignore", 0, "no .gitignore"))


def check_gitleaks_config(files, root, out):
    if ".gitleaks.toml" not in files:
        return
    text = read_text(root, ".gitleaks.toml")
    if not re.search(r"^\s*useDefault\s*=\s*true\b", text, re.M):
        out.append(Finding("error", "gitleaks-config", ".gitleaks.toml", 0,
                           "config does not extend the default rules (`[extend] useDefault = true`)"))
    for n, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\s*paths\s*=", line) and re.search(r"""(?:'''|")\s*\.\*\s*(?:'''|")""", line):
            out.append(Finding("error", "gitleaks-config", ".gitleaks.toml", n,
                               "allowlist path `.*` disables scanning for the whole repository"))


def check_forbidden(files, root, allow, out):
    for path in files:
        base = os.path.basename(path)
        if allowed(path, allow) or base.endswith(TEMPLATE_SUFFIXES):
            continue
        if any(fnmatch.fnmatch(base, pat) for pat in FORBIDDEN_NAMES):
            out.append(Finding("error", "forbidden-file", path, 0,
                               "credential/state file must not be committed (add to .gitignore, rotate if real)"))
        elif base.endswith((".pem", ".key")) and PRIVATE_KEY_RE.search(read_text(root, path)):
            out.append(Finding("error", "forbidden-file", path, 0, "file contains a private key"))


def check_size(files, root, max_mb, allow, out):
    limit = max_mb * 1024 * 1024
    for path in files:
        try:
            size = os.path.getsize(os.path.join(root, path))
        except OSError:
            continue
        if size > limit and not allowed(path, allow):
            out.append(Finding("error", "large-file", path, 0,
                               f"{size / 1048576:.1f} MiB exceeds {max_mb} MiB (use release assets or LFS)"))


def check_workflows(files, root, strict_pinning, allow, out):
    for path in files:
        if not re.match(r"^\.github/workflows/[^/]+\.ya?ml$", path):
            continue
        lines = read_text(root, path).splitlines()
        text = "\n".join(lines)
        if not re.search(r"^permissions:", text, re.M):
            level = "warning" if re.search(r"^\s+permissions:", text, re.M) else "error"
            out.append(Finding(level, "workflow-permissions", path, 0,
                               "no top-level `permissions:`; GITHUB_TOKEN defaults may be write-all"))
        run_indent = None
        for n, line in enumerate(lines, 1):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if re.match(r"^\s*pull_request_target\s*:?", line) and not allowed(path, allow):
                out.append(Finding("error", "workflow-pull-request-target", path, n,
                                   "pull_request_target runs with secrets on fork code; use pull_request "
                                   "or a workflow_run follow-up (allow with .auditor-allow if reviewed)"))
            m = USES_RE.match(line)
            if m:
                ref = m.group(1)
                if not (ref.startswith("./") or ref.startswith("docker://")):
                    name, _, version = ref.partition("@")
                    first_party = name.lower().startswith(FIRST_PARTY_OWNERS)
                    if not SHA_RE.match(version) and not first_party:
                        out.append(Finding("error" if strict_pinning else "warning", "workflow-unpinned-action",
                                           path, n, f"`{ref}` is not pinned to a full commit SHA"))
            # run-block tracking for script injection
            if run_indent is not None and (not stripped or indent > run_indent):
                if INJECTABLE_RE.search(line):
                    out.append(Finding("error", "workflow-script-injection", path, n,
                                       "attacker-controlled expression inside `run:`; pass it through `env:`"))
                continue
            run_indent = None
            rm = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
            if rm:
                run_indent = len(rm.group(1)) + (2 if re.match(r"^\s*-\s+run:", line) else 0)
                if INJECTABLE_RE.search(rm.group(2)):
                    out.append(Finding("error", "workflow-script-injection", path, n,
                                       "attacker-controlled expression inside `run:`; pass it through `env:`"))


def check_dockerfiles(files, root, out):
    for path in files:
        if not re.search(r"(^|/)Dockerfile(\.[\w.-]+)?$|\.dockerfile$", path):
            continue
        last_stage_user = False
        for n, line in enumerate(read_text(root, path).splitlines(), 1):
            m = re.match(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+\S+)?\s*$", line, re.I)
            if m:
                last_stage_user = False
                image = m.group(1)
                if image.lower() != "scratch" and not image.startswith("$") and (
                    ":" not in image.split("/")[-1].split("@")[0] and "@" not in image
                    or image.endswith(":latest")
                ):
                    out.append(Finding("warning", "dockerfile-latest", path, n,
                                       f"`{image}` has no pinned tag/digest"))
            if re.match(r"^\s*USER\s+(?!root\b|0\b)\S+", line, re.I):
                last_stage_user = True
        if not last_stage_user:
            out.append(Finding("warning", "dockerfile-root", path, 0, "final stage never switches to a non-root USER"))


def check_terraform(files, root, out):
    for path in files:
        if not path.endswith(".tf"):
            continue
        lines = read_text(root, path).splitlines()
        for n, line in enumerate(lines, 1):
            if re.search(r"cidr_blocks\s*=\s*\[[^\]]*\"0\.0\.0\.0/0\"", line) and any(
                "ingress" in prev for prev in lines[max(0, n - 12):n]
            ):
                out.append(Finding("warning", "terraform-open-ingress", path, n,
                                   "ingress open to 0.0.0.0/0; confirm this is intentional"))


def write_reports(findings, json_path, summary_path):
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump([asdict(f) for f in findings], fh, indent=2)
    errors = sum(f.level == "error" for f in findings)
    warnings = len(findings) - errors
    md = [f"### Repository audit: {errors} error(s), {warnings} warning(s)", ""]
    if findings:
        md += ["| Level | Check | Location | Message |", "|---|---|---|---|"]
        for f in sorted(findings, key=lambda f: (f.level != "error", f.check, f.path, f.line)):
            loc = f"`{f.path}`" + (f":{f.line}" if f.line else "")
            md.append(f"| {f.level} | {f.check} | {loc} | {f.message.replace('|', '/')} |")
    else:
        md.append("No findings.")
    text = "\n".join(md) + "\n"
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text)
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--public", choices=("true", "false"), default="true",
                    help="public repositories must carry a LICENSE")
    ap.add_argument("--strict-pinning", choices=("true", "false"), default="false")
    ap.add_argument("--max-file-mb", type=float, default=5)
    ap.add_argument("--json", default="auditor-report.json")
    args = ap.parse_args(argv)

    files = git_files(args.root)
    allow = load_allow(args.root)
    findings: list[Finding] = []
    check_required(files, args.root, args.public == "true", findings)
    check_gitleaks_config(files, args.root, findings)
    check_forbidden(files, args.root, allow, findings)
    check_size(files, args.root, args.max_file_mb, allow, findings)
    check_workflows(files, args.root, args.strict_pinning == "true", allow, findings)
    check_dockerfiles(files, args.root, findings)
    check_terraform(files, args.root, findings)

    for f in findings:
        loc = f",line={f.line}" if f.line else ""
        print(f"::{f.level} file={f.path}{loc},title={f.check}::{f.message}")
    text = write_reports(findings, args.json, os.environ.get("GITHUB_STEP_SUMMARY"))
    print(text)
    return 1 if any(f.level == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
