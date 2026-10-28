import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import approve  # noqa: E402

POLICY = approve.Policy()
GREEN = [{"name": "audit / gitleaks", "status": "completed", "conclusion": "success"},
         {"name": "audit / repository audit", "status": "completed", "conclusion": "success"}]


def doc_file(name="docs/readme.md", patch="+hello", status="modified", add=1, delete=0):
    return {"filename": name, "status": status, "additions": add, "deletions": delete, "patch": patch}


def facts(**kw):
    base = dict(head_repo="COPUR/x", base_repo="COPUR/x", author="COPUR", checks=list(GREEN),
                files=[doc_file()])
    base.update(kw)
    return approve.Facts(**base)


class Decide(unittest.TestCase):
    def verdict(self, **kw):
        return approve.decide(facts(**kw), POLICY)

    def test_clean_pull_request_is_approved(self):
        self.assertEqual(self.verdict().verdict, "approve")

    def test_draft_waits_for_a_human(self):
        d = self.verdict(draft=True)
        self.assertEqual(d.verdict, "hold")
        self.assertIn("draft", d.reasons[0])

    def test_workflow_change_is_held_even_when_everything_is_green(self):
        d = self.verdict(files=[doc_file(".github/workflows/ci.yml")])
        self.assertEqual(d.verdict, "hold")
        self.assertIn(".github/workflows/ci.yml", d.reasons[0])

    def test_gitleaks_config_terraform_and_dockerfile_are_sensitive(self):
        for path in (".gitleaks.toml", ".auditor-allow", "infra/main.tf", "svc/Dockerfile", "Dockerfile.prod",
                     ".github/CODEOWNERS", "auditor/audit.py"):
            with self.subTest(path=path):
                self.assertEqual(self.verdict(files=[doc_file(path)]).verdict, "hold")

    def test_extra_sensitive_paths(self):
        policy = approve.Policy(sensitive=tuple(approve.SENSITIVE_DEFAULT) + ("contracts/*",))
        d = approve.decide(facts(files=[doc_file("contracts/a.json")]), policy)
        self.assertEqual(d.verdict, "hold")

    def test_failed_check_blocks(self):
        bad = GREEN + [{"name": "validate", "status": "completed", "conclusion": "failure"}]
        d = self.verdict(checks=bad)
        self.assertEqual(d.verdict, "block")
        self.assertIn("validate", d.reasons[0])

    def test_running_check_waits(self):
        running = GREEN + [{"name": "validate", "status": "in_progress", "conclusion": None}]
        self.assertEqual(self.verdict(checks=running).verdict, "wait")

    def test_failure_wins_over_wait(self):
        mixed = [{"name": "a", "status": "in_progress"}, {"name": "b", "status": "completed", "conclusion": "failure"}]
        self.assertEqual(self.verdict(checks=mixed).verdict, "block")

    def test_no_checks_yet_waits(self):
        self.assertEqual(self.verdict(checks=[]).verdict, "wait")

    def test_skipped_and_neutral_checks_are_fine(self):
        extra = GREEN + [{"name": "approve", "status": "completed", "conclusion": "skipped"},
                         {"name": "n", "status": "completed", "conclusion": "neutral"}]
        self.assertEqual(self.verdict(checks=extra).verdict, "approve")

    def test_commit_status_failure_blocks_and_own_status_is_ignored(self):
        self.assertEqual(self.verdict(statuses=[{"context": "ci/x", "state": "failure"}]).verdict, "block")
        self.assertEqual(self.verdict(statuses=[{"context": approve.STATUS_CONTEXT, "state": "pending"}]).verdict, "approve")

    def test_conflict_blocks_and_unknown_mergeability_waits(self):
        self.assertEqual(self.verdict(mergeable_state="dirty").verdict, "block")
        self.assertEqual(self.verdict(mergeable_state="unknown").verdict, "wait")

    def test_changes_requested_blocks(self):
        d = self.verdict(review_states={"alice": "CHANGES_REQUESTED"})
        self.assertEqual(d.verdict, "block")

    def test_unresolved_thread_holds(self):
        self.assertEqual(self.verdict(unresolved_threads=2).verdict, "hold")

    def test_stacked_fork_stranger_and_labels_hold(self):
        self.assertEqual(self.verdict(base_ref="feature").verdict, "hold")
        self.assertEqual(self.verdict(head_repo="someone/x").verdict, "hold")
        self.assertEqual(self.verdict(author="mallory").verdict, "hold")
        self.assertEqual(self.verdict(labels=["needs-human"]).verdict, "hold")

    def test_closed_or_merged_is_not_touched(self):
        self.assertEqual(self.verdict(state="closed").verdict, "hold")
        self.assertEqual(self.verdict(merged=True).verdict, "hold")

    def test_big_diff_holds(self):
        many = [doc_file(f"docs/{i}.md") for i in range(101)]
        self.assertEqual(self.verdict(files=many).verdict, "hold")
        self.assertEqual(self.verdict(files=[doc_file(add=2500, delete=600)]).verdict, "hold")

    def test_uninspectable_diff_holds_but_binaries_do_not(self):
        self.assertEqual(self.verdict(files=[doc_file(patch=None)]).verdict, "hold")
        self.assertEqual(self.verdict(files=[doc_file("img/a.png", patch=None)]).verdict, "approve")

    def test_conflict_marker_and_private_key_block(self):
        self.assertEqual(self.verdict(files=[doc_file(patch="+<<<<<<< HEAD")]).verdict, "block")
        self.assertEqual(self.verdict(files=[doc_file(patch="+-----BEGIN PRIVATE KEY-----")]).verdict, "block")

    def test_removed_lines_are_not_scanned(self):
        self.assertEqual(self.verdict(files=[doc_file(patch="-<<<<<<< HEAD", add=0, delete=1)]).verdict, "approve")

    def test_disabling_or_deleting_tests_holds(self):
        skip = doc_file("tests/test_a.py", patch="+@pytest.mark.skip(reason='later')")
        self.assertEqual(self.verdict(files=[skip]).verdict, "hold")
        gone = doc_file("src/foo.test.ts", patch="-x", status="removed", add=0, delete=1)
        self.assertEqual(self.verdict(files=[gone]).verdict, "hold")
        prose = doc_file("docs/skip.md", patch="+@pytest.mark.skip is discouraged")
        self.assertEqual(self.verdict(files=[prose]).verdict, "approve")


class Helpers(unittest.TestCase):
    def test_parse_stream_handles_concatenated_and_pretty_json(self):
        text = '{"a": 1}\n{"a": 2}\n{\n  "a": 3\n}\n'
        self.assertEqual([o["a"] for o in approve.parse_stream(text)], [1, 2, 3])
        self.assertEqual(approve.parse_stream(""), [])

    def test_latest_review_state_per_reviewer(self):
        reviews = [
            {"user": {"login": "a"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-01-01T00:00:00Z"},
            {"user": {"login": "a"}, "state": "APPROVED", "submitted_at": "2026-01-02T00:00:00Z"},
            {"user": {"login": "b"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-01-01T00:00:00Z"},
            {"user": {"login": "b"}, "state": "DISMISSED", "submitted_at": "2026-01-02T00:00:00Z"},
            {"user": {"login": "c"}, "state": "COMMENTED", "submitted_at": "2026-01-01T00:00:00Z"},
        ]
        self.assertEqual(approve.latest_review_states(reviews), {"a": "APPROVED"})


def fake_run(*results):
    """subprocess.run stand-in answering with (returncode, stdout, stderr) in turn."""
    calls = iter(results)
    return lambda *a, **k: subprocess.CompletedProcess(a[0], *next(calls))


class Merge(unittest.TestCase):
    def merge(self, *results, **kw):
        runner = mock.Mock(side_effect=fake_run(*results))
        with mock.patch.object(approve.subprocess, "run", runner), mock.patch.object(approve.time, "sleep") as sleep:
            outcome = approve.merge("COPUR/x", 1, "abc", "squash", **kw)
        return outcome, runner.call_count, sleep.call_count

    def test_merges_first_time(self):
        self.assertEqual(self.merge((0, '{"sha": "m1"}', "")), ((True, "m1"), 1, 0))

    def test_retries_405_until_github_counts_the_status(self):
        refused = (1, '{"message":"Required status check ci-approver is expected."}', "gh: ... (HTTP 405)")
        self.assertEqual(self.merge(refused, refused, (0, '{"sha": "m2"}', "")), ((True, "m2"), 3, 2))

    def test_gives_up_after_the_attempts(self):
        refused = (1, '{"message":"not mergeable"}', "gh: ... (HTTP 405)")
        (ok, info), calls, sleeps = self.merge(*[refused] * 3, attempts=3)
        self.assertFalse(ok)
        self.assertEqual((calls, sleeps), (3, 2))
        self.assertIn("not mergeable", info)

    def test_other_refusals_are_final(self):
        for code in ("409", "422", "403"):
            with self.subTest(code=code):
                (ok, _), calls, sleeps = self.merge((1, "{}", f"gh: ... (HTTP {code})"))
                self.assertEqual((ok, calls, sleeps), (False, 1, 0))


if __name__ == "__main__":
    unittest.main()
