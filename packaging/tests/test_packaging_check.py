import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import packaging_check as pc  # noqa: E402

COMPONENTS = HERE.parent / "components.csv"
HEADER = "component,repo,kind,abstractness,depends_on\n"


class Analyze(unittest.TestCase):
    def test_the_shipped_components_pass_strict(self):
        res = pc.analyze(COMPONENTS.read_text())
        self.assertFalse(res.failed, res.report)
        self.assertEqual(res.cycles, [])
        self.assertIn("33 components, 19 repos", res.report)

    def test_a_cycle_is_found_and_fails(self):
        res = pc.analyze(HEADER + "a,R1,x,0.5,b\nb,R2,x,0.5,a\n")
        self.assertEqual(res.cycles, [["a", "b"]])
        self.assertTrue(res.failed)
        self.assertIn("CYCLE: a <-> b", res.report)

    def test_depending_on_a_less_stable_component_is_an_sdp_violation(self):
        # x is depended on twice and depends on y once (I=0.33); y depends on two (I=0.67): x -> y points at the less stable
        csv_text = HEADER + "p,R1,x,0.5,x\nq,R2,x,0.5,x\nx,R3,x,0.5,y\ny,R4,x,0.5,s1;s2\ns1,R5,x,0.5,\ns2,R6,x,0.5,\n"
        res = pc.analyze(csv_text)
        self.assertEqual(res.sdp_violations, [("x", "y")])
        self.assertTrue(res.failed)

    def test_zone_of_pain_fails_the_distance_limit(self):
        # concrete (A=0) and stable (I=0): D' = 1
        res = pc.analyze(HEADER + "lib,R1,x,0.0,\napp,R2,x,0.0,lib\n")
        self.assertEqual(res.metrics["lib"][4], 1.0)
        self.assertTrue(res.failed)
        self.assertIn("zone of pain", res.report)

    def test_unknown_dependency_and_duplicates_are_input_errors(self):
        with self.assertRaises(pc.ComponentsError):
            pc.analyze(HEADER + "a,R1,x,0.5,ghost\n")
        with self.assertRaises(pc.ComponentsError):
            pc.analyze(HEADER + "a,R1,x,0.5,\na,R1,x,0.5,\n")


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(HERE.parent / "packaging_check.py"), *args], capture_output=True, text=True)

    def test_exit_codes(self):
        self.assertEqual(self.run_cli(str(COMPONENTS), "--strict").returncode, 0)
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d, "bad.csv")
            bad.write_text(HEADER + "a,R1,x,0.5,b\nb,R2,x,0.5,a\n")
            self.assertEqual(self.run_cli(str(bad)).returncode, 0)  # report only
            self.assertEqual(self.run_cli(str(bad), "--strict").returncode, 1)
            self.assertEqual(self.run_cli(str(Path(d, "missing.csv"))).returncode, 2)
            self.assertEqual(self.run_cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()
