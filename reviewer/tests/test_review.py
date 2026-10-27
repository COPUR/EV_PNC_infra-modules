import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review  # noqa: E402


def args(**overrides):
    base = dict(repo="COPUR/EV_PNC_docs", model="claude-opus-5-5", effort="high",
                req_id_pattern="", protected_paths="", max_diff_chars=400_000)
    base.update(overrides)
    return argparse.Namespace(**base)


def pr(**overrides):
    base = {"number": 3, "draft": False, "title": "docs(architecture): add Kafka ADRs",
            "body": "", "base": {"sha": "b" * 40}, "head": {"sha": "h" * 40}}
    base.update(overrides)
    return base


class TitleTest(unittest.TestCase):
    def test_conventional_titles(self):
        for t in ["docs: add ADR-0008", "feat(registry)!: drop v1 grants", "ci(review): add agent",
                  "fix(decision,audit): RC-12 keep hash chain"]:
            self.assertEqual(review.check_title(t, ""), [], t)

    def test_rejects_plain_title(self):
        self.assertEqual(len(review.check_title("Add Kafka backend principles", "")), 1)

    def test_requirement_id(self):
        self.assertEqual(review.check_title("feat(registry): RC-12 revoke grant", r"RC-[0-9]+"), [])
        self.assertEqual(len(review.check_title("feat(registry): revoke grant", r"RC-[0-9]+")), 1)


class ProtectedPathTest(unittest.TestCase):
    def test_gate_files_are_protected(self):
        paths = [".github/workflows/review.yml", "docs/adr/0008.md", "CODEOWNERS",
                 ".gitleaks.toml", "reviewer/review.py", "auditor/audit.py", "docs/CODEOWNERS"]
        got = review.protected_changes(paths, review.DEFAULT_PROTECTED)
        self.assertNotIn("docs/adr/0008.md", got)
        self.assertEqual(len(got), 6)

    def test_extra_patterns(self):
        self.assertEqual(review.protected_changes(["charts/x/values.yaml"], ["charts/**"]),
                         ["charts/x/values.yaml"])


class PolicyTest(unittest.TestCase):
    def test_model_cannot_approve_over_major(self):
        self.assertEqual(review.enforce_policy("approve", [{"severity": "major"}]), "request_changes")
        self.assertEqual(review.enforce_policy("approve", [{"severity": "blocker"}]), "request_changes")

    def test_minor_findings_can_be_approved(self):
        self.assertEqual(review.enforce_policy("approve", [{"severity": "minor"}, {"severity": "nit"}]), "approve")

    def test_request_changes_stays(self):
        self.assertEqual(review.enforce_policy("request_changes", []), "request_changes")


class DecideTest(unittest.TestCase):
    def test_draft_is_pending_without_model_call(self):
        with mock.patch.object(review, "ask_claude") as ask:
            v, _ = review.decide(args(), pr(draft=True))
        self.assertEqual(v.state, "pending")
        ask.assert_not_called()

    def test_bad_title_stops_before_model(self):
        with mock.patch.object(review, "ask_claude") as ask:
            v, _ = review.decide(args(), pr(title="Add stuff"))
        self.assertEqual(v.state, "request_changes")
        ask.assert_not_called()

    def test_gate_change_needs_human(self):
        with mock.patch.object(review, "changed_paths", return_value=[".github/workflows/x.yml"]), \
                mock.patch.object(review, "ask_claude") as ask:
            v, _ = review.decide(args(), pr())
        self.assertEqual(v.state, "needs_human")
        ask.assert_not_called()

    def test_large_diff_needs_human(self):
        with mock.patch.object(review, "changed_paths", return_value=["docs/a.md"]), \
                mock.patch.object(review, "diff_text", return_value="x" * 11), \
                mock.patch.object(review, "ask_claude") as ask:
            v, _ = review.decide(args(max_diff_chars=10), pr())
        self.assertEqual(v.state, "needs_human")
        ask.assert_not_called()

    def test_model_verdict_with_policy(self):
        result = {"verdict": "approve", "summary": "ok",
                  "findings": [{"severity": "major", "path": "a.md", "line": 3, "title": "t",
                                "evidence": "e", "fix": "f"}]}
        with mock.patch.object(review, "changed_paths", return_value=["docs/a.md"]), \
                mock.patch.object(review, "diff_text", return_value="diff"), \
                mock.patch.object(review, "commit_subjects", return_value=["docs: a"]), \
                mock.patch.object(review, "ask_claude", return_value=result):
            v, model = review.decide(args(), pr())
        self.assertEqual(v.state, "request_changes")
        self.assertEqual(model, "claude-opus-5-5")

    def test_clean_review_approves(self):
        result = {"verdict": "approve", "summary": "ok", "findings": []}
        with mock.patch.object(review, "changed_paths", return_value=["docs/a.md"]), \
                mock.patch.object(review, "diff_text", return_value="diff"), \
                mock.patch.object(review, "commit_subjects", return_value=[]), \
                mock.patch.object(review, "ask_claude", return_value=result):
            v, _ = review.decide(args(), pr())
        self.assertTrue(v.approved)


class MainTest(unittest.TestCase):
    def _event(self, d, **kw):
        p = Path(d) / "event.json"
        p.write_text(json.dumps({"pull_request": pr(**kw)}))
        return str(p)

    def test_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(review, "changed_paths", side_effect=RuntimeError("boom")):
            code = review.main(["--event", self._event(d), "--repo", "COPUR/x", "--no-post"])
        self.assertEqual(code, 1)

    def test_approval_exits_zero_and_posts(self):
        result = {"verdict": "approve", "summary": "ok", "findings": []}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("os.environ", {"GITHUB_TOKEN": "t"}), \
                mock.patch.object(review, "github", return_value=pr()) as gh, \
                mock.patch.object(review, "changed_paths", return_value=["docs/a.md"]), \
                mock.patch.object(review, "diff_text", return_value="diff"), \
                mock.patch.object(review, "commit_subjects", return_value=[]), \
                mock.patch.object(review, "ask_claude", return_value=result):
            code = review.main(["--event", self._event(d), "--repo", "COPUR/x"])
        self.assertEqual(code, 0)
        post = gh.call_args_list[-1]
        self.assertEqual(post.args[0], "POST")
        self.assertEqual(post.args[2]["event"], "APPROVE")
        self.assertEqual(post.args[2]["commit_id"], "h" * 40)

    def test_draft_posts_nothing(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(review, "github", return_value=pr(draft=True)) as gh:
            code = review.main(["--event", self._event(d, draft=True), "--repo", "COPUR/x"])
        self.assertEqual(code, 1)
        self.assertEqual([c.args[0] for c in gh.call_args_list], ["GET"])


if __name__ == "__main__":
    unittest.main()
