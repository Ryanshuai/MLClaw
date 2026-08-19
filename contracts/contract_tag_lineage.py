#!/usr/bin/env python3
"""`shared/tag_lineage.py` writes `run.json`, and nothing was checking how.

CLAUDE.md -> "Contracts": a check is earned by *a record written now and read
later by someone who can no longer verify it.* `run.json` is that record --
`/conclude` cites its numbers, `baseline_delta` subtracts them, `/repro` judges
against them -- and this script rewrites the whole file to append one string to a
list.

It did so with a direct `open(path, "w")`, which truncates the record before the
new bytes land, and with `ensure_ascii` left at its default, which re-escaped
every non-ASCII field of a record that was written literally. Both are the
defects `_records.atomic_write_json` exists for, and this was the last writer of
`run.json` that did not go through it.

The propagation itself is the other half. A pipeline tag walks UP the lineage DAG
and writes every ancestor, so the two ways it can quietly be wrong are a cycle
(it must terminate) and an ancestor whose `run.json` it could not read (it must
say the propagation was partial rather than reporting success).
"""
import json
import os
import unittest

from helpers import TempDirCase, load_script, run_script

tag = load_script("shared/tag_lineage.py")
SCRIPT = "shared/tag_lineage.py"


class ProjectCase(TempDirCase):

    def run_rec(self, stage, run_id, parents=(), **extra):
        rec = {"stage": stage, "run_id": run_id,
               "lineage": {"parents": [{"stage": s, "run_id": r} for s, r in parents],
                           "local_tags": [], "pipeline_tags": []}}
        rec.update(extra)
        self.write_json(f"proj/stages/{stage}/runs/{run_id}/run.json", rec)
        return self.path("proj", "stages", stage, "runs", run_id, "run.json")

    def read_run(self, stage, run_id):
        return self.read_json(f"proj/stages/{stage}/runs/{run_id}/run.json")

    @property
    def root(self):
        return self.path("proj")


class TheRecordSurvivesBeingTagged(ProjectCase):
    """`run.json` is the record every downstream verdict is read back from.
    Rewriting it to append a tag must not be able to destroy it."""

    def test_the_write_goes_through_the_one_atomic_writer(self):
        import inspect
        src = inspect.getsource(tag.save_run)
        self.assertIn("atomic_write_json", src,
                      "a direct open(w) truncates run.json before the new bytes "
                      "land; a crash there loses a run record to add a string")

    def test_a_non_ascii_field_is_not_re_escaped(self):
        """The money-ledger bug, one record over. Round-tripping is not the test:
        JSON survives either way, and it is the person reading it who cannot."""
        self.run_rec("training", "run_001", notes="GPU 机房 ‼️")
        run_script(SCRIPT, self.root, "training/run_001", "baseline")
        raw = self.read("proj/stages/training/runs/run_001/run.json")
        self.assertIn("机房", raw)
        self.assertNotIn("\\u673a", raw)

    def test_nothing_else_in_the_record_is_lost(self):
        self.run_rec("training", "run_001", metrics={"map50": 0.842}, mode="production")
        run_script(SCRIPT, self.root, "training/run_001", "baseline")
        out = self.read_run("training", "run_001")
        self.assertEqual(out["metrics"], {"map50": 0.842})
        self.assertEqual(out["mode"], "production")


class OnlyPipelineTagsClimb(ProjectCase):
    """Two tag types with different reach, and the difference is the whole
    feature: a free-form note must not silently mark five ancestor runs
    `production`."""

    def setUp(self):
        super().setUp()
        self.run_rec("training", "run_001")
        self.run_rec("evaluation", "run_002", parents=[("training", "run_001")])

    def test_a_pipeline_tag_reaches_the_ancestor(self):
        rc, out, _ = run_script(SCRIPT, self.root, "evaluation/run_002", "production")
        self.assertEqual(rc, 0)
        self.assertTrue(out["propagated"])
        self.assertEqual(self.read_run("training", "run_001")["lineage"]["pipeline_tags"],
                         ["production"])

    def test_a_local_tag_stays_where_it_was_put(self):
        rc, out, _ = run_script(SCRIPT, self.root, "evaluation/run_002", "batch_size_4")
        self.assertEqual(rc, 0)
        self.assertFalse(out["propagated"])
        self.assertEqual(out["tagged_runs"], ["evaluation/run_002"])
        self.assertEqual(self.read_run("training", "run_001")["lineage"]["pipeline_tags"],
                         [])
        self.assertEqual(self.read_run("evaluation", "run_002")["lineage"]["local_tags"],
                         ["batch_size_4"])

    def test_tagging_twice_changes_nothing_and_says_so(self):
        run_script(SCRIPT, self.root, "evaluation/run_002", "production")
        _rc, out, _ = run_script(SCRIPT, self.root, "evaluation/run_002", "production")
        self.assertEqual(out["tagged_runs"], [])
        self.assertEqual(self.read_run("training", "run_001")["lineage"]["pipeline_tags"],
                         ["production"], "and no duplicate appended")


class TheWalkTerminatesAndSaysWhatItCouldNotReach(ProjectCase):

    def test_a_lineage_cycle_does_not_hang(self):
        """Nothing forbids a cycle in the recorded parents, and a walk that
        trusted the DAG to be acyclic would spin forever on one bad record."""
        self.run_rec("training", "run_001", parents=[("evaluation", "run_002")])
        self.run_rec("evaluation", "run_002", parents=[("training", "run_001")])
        runs, _unreadable = tag.load_all_runs(self.root)
        got = tag.get_ancestors(runs, "evaluation/run_002")
        self.assertEqual(sorted(set(got)), ["evaluation/run_002", "training/run_001"])

    def test_a_parent_entry_naming_no_run_is_skipped_not_crashed_on(self):
        self.write_json("proj/stages/evaluation/runs/run_002/run.json", {
            "stage": "evaluation", "run_id": "run_002",
            "lineage": {"parents": [{"stage": "training"}, None, {"run_id": "x"}],
                        "local_tags": [], "pipeline_tags": []}})
        rc, out, _ = run_script(SCRIPT, self.root, "evaluation/run_002", "production")
        self.assertEqual(rc, 0)
        self.assertEqual(out["tagged_runs"], ["evaluation/run_002"])

    def test_an_unreadable_ancestor_makes_the_propagation_incomplete(self):
        """CLAUDE.md: *Never report data you could not look at.* Tagging the
        reachable half of a lineage and reporting success is how a run nobody
        could read reads as one that was never in production."""
        self.run_rec("training", "run_001")
        self.run_rec("evaluation", "run_002", parents=[("training", "run_001")])
        self.write("proj/stages/training/runs/run_001/run.json", "{truncated")
        rc, out, _ = run_script(SCRIPT, self.root, "evaluation/run_002", "production")
        self.assertEqual(rc, 1, "an incomplete propagation is not a success")
        self.assertFalse(out["complete"])
        self.assertEqual(len(out["unreadable"]), 1)
        self.assertEqual(out["tagged_runs"], ["evaluation/run_002"],
                         "what it COULD tag is still tagged")

    def test_a_clean_run_says_complete(self):
        self.run_rec("training", "run_001")
        _rc, out, _ = run_script(SCRIPT, self.root, "training/run_001", "baseline")
        self.assertTrue(out["complete"])
        self.assertEqual(out["unreadable"], [])


class UsageAndNotFoundAreDifferentAnswers(ProjectCase):
    """CLAUDE.md -> "Script Integration": 2 = the script broke, fall back;
    1 = it worked and the answer is no. Both were exit 1, so "you typed it
    wrong" and "that run does not exist" arrived identically."""

    def test_too_few_arguments_is_exit_2(self):
        rc, out, _ = run_script(SCRIPT, self.root)
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_a_run_that_is_not_there_is_exit_1(self):
        self.run_rec("training", "run_001")
        rc, out, _ = run_script(SCRIPT, self.root, "training/run_999", "baseline")
        self.assertEqual(rc, 1)
        self.assertIn("refused", out)

    def test_not_found_names_what_it_could_not_read(self):
        """A run that exists and is unreadable looks exactly like one that is not
        there, unless the refusal says which files it failed on."""
        self.write("proj/stages/training/runs/run_001/run.json", "{truncated")
        rc, out, _ = run_script(SCRIPT, self.root, "training/run_001", "baseline")
        self.assertEqual(rc, 1)
        self.assertEqual(len(out["unreadable"]), 1)


if __name__ == "__main__":
    unittest.main()
