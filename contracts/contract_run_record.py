"""Regression tests for run creation and finalization.

Two silent-record defects under test:

  - Naive local timestamps. CLAUDE.md's canonical query sorts with
    `sort_by(.created_at)`; across machines in different zones that sort is
    wrong and looks fine. And `finished_at - started_at` across a zone change
    silently misreports duration.
  - `run_id` collisions. One-second resolution plus `makedirs(exist_ok=True)`
    meant two runs launched in the same second shared a directory and the
    second `run.json` write destroyed the first run's record.
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import REPO_ROOT, TempDirCase, run_script


TEMPLATE = os.path.join(REPO_ROOT, "lifecycle", "run.json")


class Timestamps(TempDirCase):
    """run-mechanics.md -> "Record integrity": run timestamps are UTC with an explicit
    offset. The canonical query sorts with `sort_by(.created_at)`; on naive local
    strings from machines in different zones that sort is wrong and looks fine.
    """
    def test_created_at_carries_an_offset(self):
        stage = self.path("stages", "evaluation")
        os.makedirs(stage)
        rc, out, _ = run_script("shared/create_run.py", stage, TEMPLATE)
        self.assertEqual(rc, 0)

        parsed = datetime.fromisoformat(out["created_at"])
        self.assertIsNotNone(parsed.tzinfo, "created_at must not be naive local time")
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None),
                         "created_at must be UTC so string sort is chronological")

class RunIdCollision(TempDirCase):
    """run-mechanics.md -> "Record integrity": a run_id identifies exactly one run.
    One-second resolution plus `makedirs(exist_ok=True)` let two runs launched in the
    same second share a directory, and the second run.json write destroyed the first
    run's record.
    """
    def test_same_second_runs_get_separate_directories(self):
        stage = self.path("stages", "training")
        os.makedirs(stage)

        ids = []
        for _ in range(3):
            rc, out, _ = run_script("shared/create_run.py", stage, TEMPLATE)
            self.assertEqual(rc, 0)
            ids.append(out["run_id"])

        self.assertEqual(len(set(ids)), 3, f"run_ids collided: {ids}")
        for run_id in ids:
            self.assertTrue(os.path.isfile(os.path.join(stage, "runs", run_id, "run.json")))

    def test_collision_is_reported_not_silent(self):
        """Occupy every id `create_run.py` could pick in the next few seconds,
        rather than launching it twice and hoping both land in the same one.

        The two-launch version failed roughly one full-suite run in fifty: when
        the pair straddled a second boundary there was no collision to report and
        the check failed for being right. A flaky contract is worse than a
        missing one — it teaches people that red means "run it again", which is
        the opposite of what this suite is for.
        """
        stage = self.path("stages", "training")
        os.makedirs(stage)

        # Local time, because `create_run.py` builds `run_id` from `datetime.now()`
        # — a human-facing label, per its own docstring.
        now = datetime.now()
        for delta in range(4):
            rid = "run_" + (now + timedelta(seconds=delta)).strftime("%Y%m%d_%H%M%S")
            d = os.path.join(stage, "runs", rid)
            os.makedirs(d)
            with open(os.path.join(d, "run.json"), "w") as fh:
                fh.write("{}")

        rc, out, err = run_script("shared/create_run.py", stage, TEMPLATE)

        self.assertIn("id_collision", out,
                      "the base id was taken and the reallocation went unreported")
        self.assertIn("id_collision", err.replace("warning: ", "id_collision"))

class Finalize(TempDirCase):
    """run-mechanics.md -> "Record integrity": a duration that could not be computed is
    reported as such, never left null with no explanation.
    """
    def make_run(self, **fields):
        with open(TEMPLATE) as f:
            run = json.load(f)
        run.update(fields)
        return self.write_json("run.json", run)

    def test_duration_from_aware_timestamps(self):
        started = datetime.now(timezone.utc).isoformat()
        path = self.make_run(started_at=started)

        rc, out, _ = run_script("shared/finalize_run.py", path, "completed")

        self.assertEqual(rc, 0)
        self.assertIsNotNone(out["duration_s"])
        self.assertGreaterEqual(out["duration_s"], 0)
        self.assertEqual(out["warnings"], [])

    def test_naive_started_at_warns_instead_of_swallowing(self):
        """Previously: naive minus aware raised TypeError, was caught by a bare
        `except: pass`, and duration_s stayed null with no explanation."""
        path = self.make_run(started_at="2026-07-30T08:00:00")

        rc, out, err = run_script("shared/finalize_run.py", path, "completed")

        self.assertEqual(rc, 0)
        self.assertIsNotNone(out["duration_s"], "duration must still be computed")
        self.assertTrue(any("no timezone" in w for w in out["warnings"]))
        self.assertIn("warning", err)

    def test_missing_started_at_says_why(self):
        path = self.make_run(started_at=None)
        rc, out, _ = run_script("shared/finalize_run.py", path, "completed")

        self.assertIsNone(out["duration_s"])
        self.assertTrue(any("never marked started" in w for w in out["warnings"]))

    def test_unparseable_started_at_says_why(self):
        path = self.make_run(started_at="yesterday afternoon")
        rc, out, _ = run_script("shared/finalize_run.py", path, "completed")

        self.assertIsNone(out["duration_s"])
        self.assertTrue(any("could not parse" in w for w in out["warnings"]))

    def test_negative_duration_is_surfaced_not_clamped(self):
        future = "2099-01-01T00:00:00+00:00"
        path = self.make_run(started_at=future)
        rc, out, _ = run_script("shared/finalize_run.py", path, "completed")

        self.assertLess(out["duration_s"], 0)
        self.assertTrue(any("negative" in w for w in out["warnings"]))

    def test_status_written_to_disk(self):
        path = self.make_run(started_at=datetime.now(timezone.utc).isoformat())
        run_script("shared/finalize_run.py", path, "failed")

        with open(path) as f:
            self.assertEqual(json.load(f)["status"], "failed")

    def test_unknown_status_is_rejected(self):
        path = self.make_run()
        rc, _, err = run_script("shared/finalize_run.py", path, "finished")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", err)

    def test_failed_without_stderr_log_says_so(self):
        path = self.make_run(started_at=datetime.now(timezone.utc).isoformat())
        rc, out, _ = run_script("shared/finalize_run.py", path, "failed")
        self.assertTrue(any("no " in w and "stderr.log" in w for w in out["warnings"]))


class Template(unittest.TestCase):
    """skill-graph.md -> "Workflow State Protocol": resume reads `run.json -> steps` to
    skip completed steps, so a skill must write exactly the keys the template defines.
    A key the template lacks is never recognized as completed and the step re-runs
    forever — this is how `check_sources` vs `resolve_assets` went unnoticed.
    """

    def setUp(self):
        with open(TEMPLATE) as f:
            self.t = json.load(f)

    def test_canonical_step_names(self):
        """Skills write these keys; resume reads them back. A mismatch means a
        completed step is never recognized and re-runs forever."""
        for key in ("fork_check", "resolve_assets", "create_run", "execute", "collect_results"):
            self.assertIn(key, self.t["steps"], f"missing canonical step `{key}`")

    def test_check_sources_is_gone(self):
        self.assertNotIn("check_sources", self.t["steps"])

    def test_update_index_is_gone(self):
        """CLAUDE.md abolished the index file; run.json is the source of truth."""
        self.assertNotIn("update_index", self.t["steps"].get("collect_results", {}))

    def test_monitor_step_exists(self):
        """CLAUDE.md states train-run adds a monitor step between execute and
        collect_results."""
        self.assertIn("monitor", self.t["steps"])

    def test_skills_only_reference_steps_the_template_defines(self):
        """The check_sources/resolve_assets split, caught mechanically so it
        cannot come back."""
        import re
        skills_dir = os.path.join(REPO_ROOT, ".claude", "skills")
        defined = set(self.t["steps"])
        for sub in self.t["steps"].values():
            if isinstance(sub, dict):
                defined |= {k for k, v in sub.items() if isinstance(v, dict)}

        offenders = []
        for skill in ("infer-run", "eval-run", "train-run"):
            md = os.path.join(skills_dir, skill, "SKILL.md")
            if not os.path.isfile(md):
                continue
            with open(md) as f:
                for name in re.findall(r"\(step `([a-z_]+)`\)", f.read()):
                    if name not in defined:
                        offenders.append(f"{skill}: `{name}`")
        self.assertEqual(offenders, [], f"step names absent from run.json: {offenders}")


if __name__ == "__main__":
    unittest.main()
