import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import audit  # noqa: E402

CLEAN_WORKFLOW = """name: x
on: push
permissions:
  contents: read
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0
      - run: echo hi
"""


def make_repo(files: dict[str, str]) -> str:
    root = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", root], check=True)
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    return root


def run_audit(files, **kw):
    root = make_repo(files)
    out: list[audit.Finding] = []
    tracked = audit.git_files(root)
    allow = audit.load_allow(root)
    audit.check_required(tracked, root, kw.get("public", True), out)
    audit.check_gitleaks_config(tracked, root, out)
    audit.check_forbidden(tracked, root, allow, out)
    audit.check_workflows(tracked, root, kw.get("strict", False), allow, out)
    audit.check_dockerfiles(tracked, root, out)
    return {(f.level, f.check) for f in out}


BASE = {
    "README.md": "x",
    "LICENSE": "x",
    ".gitignore": "x",
    ".github/CODEOWNERS": "* @COPUR",
    ".gitleaks.toml": "[extend]\nuseDefault = true\n",
}


class AuditTests(unittest.TestCase):
    def test_clean_repo(self):
        self.assertEqual(run_audit({**BASE, ".github/workflows/x.yml": CLEAN_WORKFLOW}), set())

    def test_missing_files(self):
        found = run_audit({"README.md": "x"})
        self.assertIn(("error", "required-files"), found)

    def test_private_repo_needs_no_license(self):
        files = {k: v for k, v in BASE.items() if k != "LICENSE"}
        self.assertEqual(run_audit(files, public=False), set())
        self.assertIn(("error", "required-files"), run_audit(files, public=True))

    def test_forbidden_files(self):
        found = run_audit({**BASE, ".env": "A=1", "infra/terraform.tfstate": "{}", "keys/x.pem": "-----BEGIN PRIVATE KEY-----\n"})
        self.assertEqual({c for _, c in found}, {"forbidden-file"})

    def test_templates_and_public_certs_pass(self):
        self.assertEqual(run_audit({**BASE, ".env.example": "A=", "certs/root.pem": "-----BEGIN CERTIFICATE-----\n"}), set())

    def test_allow_file_silences_path(self):
        self.assertEqual(run_audit({**BASE, "fixtures/dev.p12": "x", ".auditor-allow": "fixtures/*\n"}), set())

    def test_weak_gitleaks_config(self):
        found = run_audit({**BASE, ".gitleaks.toml": "[[allowlists]]\npaths = ['''.*''']\n"})
        self.assertIn(("error", "gitleaks-config"), found)

    def test_workflow_findings(self):
        wf = """name: x
on:
  pull_request_target:
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "${{ github.event.pull_request.title }}"
      - run: |
          echo ok
          echo "${{ github.head_ref }}"
      - name: safe
        env:
          T: ${{ github.event.pull_request.title }}
        run: echo "$T"
"""
        found = run_audit({**BASE, ".github/workflows/x.yml": wf})
        self.assertIn(("error", "workflow-permissions"), found)
        self.assertIn(("error", "workflow-pull-request-target"), found)
        self.assertIn(("warning", "workflow-unpinned-action"), found)
        self.assertIn(("error", "workflow-script-injection"), found)

    def test_injection_counts(self):
        wf = CLEAN_WORKFLOW + """      - run: echo "${{ github.event.issue.title }}"
      - run: |
          echo "${{ github.head_ref }}"
      - env:
          T: ${{ github.head_ref }}
        run: echo "$T"
"""
        root = make_repo({**BASE, ".github/workflows/x.yml": wf})
        out: list[audit.Finding] = []
        audit.check_workflows(audit.git_files(root), root, False, [], out)
        self.assertEqual(sum(f.check == "workflow-script-injection" for f in out), 2)

    def test_strict_pinning_and_first_party(self):
        wf = CLEAN_WORKFLOW.replace("d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0", "v4") + (
            "  b:\n    uses: COPUR/EV_PNC_infra-modules/.github/workflows/ci-auditor.yml@v1\n"
        )
        found = run_audit({**BASE, ".github/workflows/x.yml": wf}, strict=True)
        self.assertEqual(found, {("error", "workflow-unpinned-action")})

    def test_dockerfile(self):
        found = run_audit({**BASE, "Dockerfile": "FROM node\nRUN true\n"})
        self.assertEqual({c for _, c in found}, {"dockerfile-latest", "dockerfile-root"})
        ok = run_audit({**BASE, "Dockerfile": "FROM node:22-alpine AS b\nFROM scratch\nUSER 10001\n"})
        self.assertEqual(ok, set())


if __name__ == "__main__":
    unittest.main()
