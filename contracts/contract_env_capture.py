#!/usr/bin/env python3
"""CLAUDE.md -> "Never silently": *Never pass a param the code ignores* -- and
`references/run-mechanics.md` -> "Env + deps (Step 2 detail)", which is where the
two calls this file guards are now spelled once.

`/train-run` Step 2 runs four helpers in one code block. Two of them take
`<code_dir> <RUN_DIR>`; the other two take neither, and the block passed a run
directory to all four.

  capture_env.py <RUN_DIR>              the run dir became the sole entry of the
                                        package list, so the record read
                                        `"packages": {"/…/run_007": null}` and every
                                        ML version went uncaptured. Nothing raised
  check_deps.py <code_dir> <RUN_DIR>    `open()` on a directory, traceback, exit 1 --
                                        the code reserved for *worked, the answer is
                                        no*, so a usage error read as "the
                                        dependencies are bad"

The first is the expensive one, because its damage is invisible at every later step:
`/repro`'s env axis answers `unverifiable` for a run whose env was silently not
captured AND for one that predates env capture, so nothing downstream can tell a
broken pipeline from an old record.

Both scripts now refuse those shapes. This checks the refusals AND the documents,
because the scripts were never wrong -- the call sites were, and a script that
refuses correctly while every document still spells the refused form has fixed
nothing a reader will see.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(REPO_ROOT, "scripts", "shared")
CAPTURE = os.path.join(SHARED, "capture_env.py")
DEPS = os.path.join(SHARED, "check_deps.py")


def run(script, *args):
    p = subprocess.run([sys.executable, "-X", "utf8", script, *args],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=REPO_ROOT, timeout=180)
    try:
        return p.returncode, json.loads(p.stdout)
    except ValueError:
        return p.returncode, {"_stdout": p.stdout, "_stderr": p.stderr}


def tracked_docs():
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=REPO_ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    return [p for p in out.stdout.split() if os.path.isfile(os.path.join(REPO_ROOT, p))]


class CaptureEnvTakesAPackageListAndSaysSo(unittest.TestCase):
    """`run-mechanics.md` -> "Env + deps (Step 2 detail)": the argument is a package
    list, and a path-shaped one is refused rather than recorded as a package name."""

    def test_a_path_argument_is_exit_2_not_a_one_package_capture(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = run(CAPTURE, d)
        self.assertEqual(rc, 2, "a run dir must not be accepted as a package list")
        self.assertIn("error", out)
        self.assertIn("fix", out)

    def test_a_relative_path_is_refused_too(self):
        rc, out = run(CAPTURE, "./stages/training/runs/run_007")
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_a_package_list_still_works_and_records_exactly_those(self):
        rc, out = run(CAPTURE, "numpy,torch")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(out["packages"]), ["numpy", "torch"])

    def test_no_argument_captures_the_default_set(self):
        rc, out = run(CAPTURE)
        self.assertEqual(rc, 0)
        self.assertGreater(len(out["packages"]), 1,
                           "the default ML package set is what a run record is for")


class CheckDepsSeparatesUsageFromARefusal(unittest.TestCase):
    """CLAUDE.md -> "Script Integration": 1 = worked and the answer is no, 2 = the
    script broke. A usage error answering 1 is the one misreading this script must
    not produce -- it reads as "the dependencies are bad"."""

    def _project(self, required, installed):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"required_packages": required}, f)
        os.makedirs(os.path.join(d, "run"), exist_ok=True)
        with open(os.path.join(d, "run", "run.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {"packages": installed}}, f)
        return d

    def test_a_directory_in_the_config_slot_is_exit_2(self):
        d = self._project({"torch": ">=2.0"}, {"torch": "2.4.1"})
        rc, out = run(DEPS, d, os.path.join(d, "run"))
        self.assertEqual(rc, 2, "a usage error must not read as a failed dep check")
        self.assertIn("error", out)
        self.assertIn("config.json", out["fix"])

    def test_too_few_arguments_is_exit_2(self):
        rc, out = run(DEPS, "one")
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_satisfied_deps_are_exit_0(self):
        d = self._project({"torch": ">=2.0"}, {"torch": "2.4.1"})
        rc, out = run(DEPS, os.path.join(d, "config.json"), os.path.join(d, "run"))
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])

    def test_a_missing_package_is_exit_1_and_that_is_the_answer(self):
        d = self._project({"timm": ">=1.0"}, {"torch": "2.4.1"})
        rc, out = run(DEPS, os.path.join(d, "config.json"), os.path.join(d, "run"))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["errors"])

    def test_a_run_directory_and_its_run_json_give_the_same_answer(self):
        d = self._project({"timm": ">=1.0"}, {"torch": "2.4.1"})
        cfg = os.path.join(d, "config.json")
        a = run(DEPS, cfg, os.path.join(d, "run"))
        b = run(DEPS, cfg, os.path.join(d, "run", "run.json"))
        self.assertEqual(a, b)


class NoDocumentStillSpellsTheRefusedCall(unittest.TestCase):
    """The scripts were never wrong; the call sites were. A refusal that every
    document still tells the reader to trip is a fix nobody reaches."""

    def test_no_md_passes_a_run_dir_to_capture_env(self):
        offenders = []
        for rel in tracked_docs():
            path = os.path.join(REPO_ROOT, rel)
            for i, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
                if "capture_env.py" not in line:
                    continue
                after = line.split("capture_env.py", 1)[1].split("#")[0].strip()
                if after.startswith(("<RUN_DIR>", "<run_dir>", "$RUN_DIR", "/", "./", "~")):
                    offenders.append(f"{rel}:{i}")
        self.assertEqual(sorted(offenders), [],
                         "capture_env.py takes a package list or nothing; it now exits 2 "
                         "on a path. run-mechanics.md -> 'Env + deps (Step 2 detail)'")

    def test_no_md_passes_a_code_dir_to_check_deps(self):
        offenders = []
        for rel in tracked_docs():
            path = os.path.join(REPO_ROOT, rel)
            for i, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
                if "check_deps.py" not in line:
                    continue
                after = line.split("check_deps.py", 1)[1].split("#")[0].strip()
                if after.startswith(("<code_dir>", "<CODE_DIR>")):
                    offenders.append(f"{rel}:{i}")
        self.assertEqual(sorted(offenders), [],
                         "check_deps.py's first argument is the stage's config.json")


if __name__ == "__main__":
    unittest.main()
