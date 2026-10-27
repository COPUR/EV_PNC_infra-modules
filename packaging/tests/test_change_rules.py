import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import change_rules as cr  # noqa: E402

COMPONENTS = (HERE.parent / "components.csv").read_text()


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class Deps(unittest.TestCase):
    def check(self, repo, files):
        with tempfile.TemporaryDirectory() as d:
            for rel, text in files.items():
                write(Path(d), rel, text)
            return cr.check_deps(COMPONENTS, repo, Path(d))

    def test_a_declared_dependency_passes_in_every_manifest_style(self):
        findings, _ = self.check("EV_PNC_simulator", {
            "contracts.lock": "repo=https://github.com/COPUR/EV_PNC_hub-contracts\ncommit=abc\n",
            "pyproject.toml": 'dependencies = ["ev-pnc-ocpi-model>=0.1"]\n',
        })
        self.assertEqual(findings, [])

    def test_an_undeclared_repo_dependency_is_found_with_file_and_line(self):
        findings, _ = self.check("EV_PNC_telemetry", {"go.mod": "module x\n\nrequire github.com/COPUR/EV_PNC_pricing v0.1.0\n"})
        self.assertEqual([(f.file, f.line, f.target) for f in findings], [("go.mod", 3, "EV_PNC_pricing")])
        self.assertIn("components.csv", str(findings[0]))

    def test_an_undeclared_component_dependency_is_found_for_maven_and_python(self):
        findings, _ = self.check("EV_PNC_pilot-analytics", {
            "build.gradle.kts": 'implementation("com.evpnc:consent-contracts:1.0.0")\nimplementation("com.evpnc:ocpi-model:0.1.0")\n',
            "pyproject.toml": 'dependencies = ["ev_pnc_policy_api"]\n',
        })
        self.assertEqual(sorted(f.target for f in findings), ["consent-contracts", "policy-api"])

    def test_the_repos_own_name_and_comments_are_ignored(self):
        findings, _ = self.check("EV_PNC_ocpi-model", {
            "generated/python/pyproject.toml": 'name = "ev-pnc-ocpi-model"\n',
            "generated/kotlin/build.gradle.kts": '// implementation("com.evpnc:consent-contracts:1")\ngroup = "com.evpnc"\n',
        })
        self.assertEqual(findings, [])

    def test_generated_and_vendored_directories_are_not_scanned(self):
        findings, _ = self.check("EV_PNC_telemetry", {
            ".contracts/pyproject.toml": "COPUR/EV_PNC_pricing\n",
            "node_modules/x/package.json": "COPUR/EV_PNC_pricing\n",
            "build/go.mod": "COPUR/EV_PNC_pricing\n",
        })
        self.assertEqual(findings, [])

    def test_repos_outside_the_metrics_are_skipped_with_a_note(self):
        findings, notes = self.check("EV_PNC_docs", {"pyproject.toml": "COPUR/EV_PNC_pricing\n"})
        self.assertEqual(findings, [])
        self.assertTrue(notes)

    def test_repo_names_match_without_regard_to_case(self):
        findings, _ = self.check("ev_pnc_simulator", {"contracts.lock": "repo=https://github.com/COPUR/EV_PNC_hub-contracts\n"})
        self.assertEqual(findings, [])

    def test_other_files_are_not_manifests(self):
        findings, _ = self.check("EV_PNC_telemetry", {"README.md": "see COPUR/EV_PNC_pricing", ".github/workflows/ci.yml": "uses: COPUR/EV_PNC_pricing/x@y"})
        self.assertEqual(findings, [])


class Classify(unittest.TestCase):
    base = {"$defs": {"Tariff": {"type": "object", "properties": {"id": {"type": "string", "maxLength": 36}}, "required": ["id"]},
                      "Status": {"type": "string", "enum": ["A", "B"]}}}

    def level(self, mutate):
        new = json.loads(json.dumps(self.base))
        mutate(new["$defs"])
        return cr.classify(self.base, new)

    def test_no_change(self):
        self.assertEqual(cr.classify(self.base, self.base), "none")

    def test_added_definition_property_and_enum_value_are_minor(self):
        self.assertEqual(self.level(lambda d: d.update(New={"type": "string"})), "minor")
        self.assertEqual(self.level(lambda d: d["Tariff"]["properties"].update(x={"type": "string"})), "minor")
        self.assertEqual(self.level(lambda d: d["Status"]["enum"].append("C")), "minor")
        self.assertEqual(self.level(lambda d: d["Tariff"]["properties"]["id"].update(maxLength=255)), "minor")

    def test_removed_retyped_or_newly_required_is_major(self):
        self.assertEqual(self.level(lambda d: d.pop("Status")), "major")
        self.assertEqual(self.level(lambda d: d["Tariff"]["properties"].pop("id")), "major")
        self.assertEqual(self.level(lambda d: d["Status"]["enum"].remove("B")), "major")
        self.assertEqual(self.level(lambda d: d["Tariff"]["properties"]["id"].update(type="integer")), "major")
        self.assertEqual(self.level(lambda d: d["Tariff"]["required"].append("x")), "major")
        self.assertEqual(self.level(lambda d: d["Tariff"]["properties"]["id"].update(maxLength=10)), "major")

    def test_descriptions_and_extension_keys_are_patch(self):
        self.assertEqual(self.level(lambda d: d["Tariff"].update(description="now documented")), "patch")
        self.assertEqual(self.level(lambda d: d["Tariff"]["properties"]["id"].update(**{"x-ocpi-note": "n"})), "patch")

    def test_semver_bump_sizes(self):
        self.assertTrue(cr.bumped_enough("0.1.0", "0.1.1", "patch"))
        self.assertFalse(cr.bumped_enough("0.1.0", "0.1.0", "patch"))
        self.assertTrue(cr.bumped_enough("0.1.4", "0.2.0", "minor"))
        self.assertFalse(cr.bumped_enough("0.1.0", "0.1.9", "minor"))
        self.assertTrue(cr.bumped_enough("1.4.0", "2.0.0", "major"))
        self.assertFalse(cr.bumped_enough("1.4.0", "1.5.0", "major"))
        with self.assertRaises(ValueError):
            cr.semver("0.1")


class Model(unittest.TestCase):
    """A real git repository per test: main holds version 0.1.0, the branch holds the change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.set_files("0.1.0", {"$defs": {"Status": {"type": "string", "enum": ["A"]}}}, "## 0.1.0\n\nFirst cut. ADR-0001.\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "change")

    def git(self, *a):
        subprocess.run(["git", "-C", str(self.root), *a], check=True, capture_output=True)

    def set_files(self, version, schema, changelog):
        write(self.root, "schema/ocpi.schema.json", json.dumps(schema))
        write(self.root, "generated/python/pyproject.toml", f'[project]\nname = "ev-pnc-ocpi-model"\nversion = "{version}"\n')
        write(self.root, "generated/kotlin/build.gradle.kts", f'group = "com.evpnc"\nversion = "{version}"\n')
        write(self.root, "CHANGELOG.md", "# Changelog\n\n" + changelog)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")

    def check(self, **kw):
        return cr.check_model(self.root, "main", "HEAD", **kw)

    def additive(self):
        return {"$defs": {"Status": {"type": "string", "enum": ["A", "B"]}}}

    def test_no_model_change_needs_nothing(self):
        write(self.root, "README.md", "x")
        self.commit()
        problems, notes = self.check()
        self.assertEqual(problems, [])
        self.assertIn("no change", notes[0])

    def test_schema_change_without_bump_or_adr_fails(self):
        self.set_files("0.1.0", self.additive(), "## 0.1.0\n\nAdded B.\n")
        self.commit()
        problems, _ = self.check()
        self.assertTrue(any("needs a minor bump" in p for p in problems), problems)
        self.assertTrue(any("names no ADR" in p for p in problems), problems)

    def test_an_additive_change_with_minor_bump_and_adr_passes(self):
        self.set_files("0.2.0", self.additive(), "## 0.2.0\n\nAdded B (ADR-0007).\n\n## 0.1.0\n\nFirst cut.\n")
        self.commit()
        self.assertEqual(self.check()[0], [])

    def test_a_breaking_change_needs_a_major_bump(self):
        self.set_files("0.2.0", {"$defs": {}}, "## 0.2.0\n\nRemoved Status (ADR-0008).\n")
        self.commit()
        problems, _ = self.check()
        self.assertTrue(any("needs a major bump" in p for p in problems), problems)
        self.set_files("1.0.0", {"$defs": {}}, "## 1.0.0\n\nRemoved Status (ADR-0008).\n")
        self.commit()
        self.assertEqual(self.check()[0], [])

    def test_the_two_generated_packages_must_agree(self):
        self.set_files("0.2.0", self.additive(), "## 0.2.0\n\nADR-0007\n")
        write(self.root, "generated/kotlin/build.gradle.kts", 'version = "0.3.0"\n')
        self.commit()
        self.assertTrue(any("disagree" in p for p in self.check()[0]))

    def test_the_named_adr_must_exist_when_a_directory_is_given(self):
        self.set_files("0.2.0", self.additive(), "## 0.2.0\n\nAdded B (ADR-0007).\n")
        self.commit()
        adrs = self.root / "adr-checkout"
        adrs.mkdir()
        self.assertTrue(any("has no 0007-*.md" in p for p in self.check(adr_dir=adrs)[0]))
        (adrs / "0007-model-b.md").write_text("# ADR-0007\n")
        self.assertEqual(self.check(adr_dir=adrs)[0], [])

    def test_moving_the_spec_pin_is_at_least_minor(self):
        write(self.root, "tools/ocpi-spec-pin.json", "{}")
        self.set_files("0.1.1", {"$defs": {"Status": {"type": "string", "enum": ["A"]}}}, "## 0.1.1\n\nPin moved (ADR-0009).\n")
        self.commit()
        self.assertTrue(any("minor bump" in p for p in self.check()[0]))

    def test_a_base_without_a_version_requires_no_bump(self):
        self.git("checkout", "-q", "main")
        self.git("rm", "-q", "-r", "generated", "CHANGELOG.md")
        self.git("commit", "-q", "-m", "no version")
        self.git("checkout", "-q", "-B", "change2")
        self.set_files("0.1.0", self.additive(), "## 0.1.0\n\nFirst (ADR-0001).\n")
        self.commit()
        problems, notes = cr.check_model(self.root, "main", "HEAD")
        self.assertEqual(problems, [])
        self.assertTrue(any("no version yet" in n for n in notes))


class Cli(unittest.TestCase):
    def test_deps_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d), "go.mod", "require github.com/COPUR/EV_PNC_pricing v1\n")
            run = lambda repo: subprocess.run(  # noqa: E731
                [sys.executable, str(HERE.parent / "change_rules.py"), "deps", "--components", str(HERE.parent / "components.csv"), "--repo", repo, "--root", d],
                capture_output=True, text=True)
            bad, ok = run("EV_PNC_telemetry"), run("EV_PNC_planner")
            self.assertEqual(bad.returncode, 1, bad.stdout)
            self.assertIn("error: go.mod:1", bad.stdout)
            self.assertEqual(ok.returncode, 0, ok.stdout)


if __name__ == "__main__":
    unittest.main()
