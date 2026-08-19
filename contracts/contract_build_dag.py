#!/usr/bin/env python3
"""`shared/build_dag.py` renders the lineage board, and one thing it renders is a
correctness rule.

CLAUDE.md -> "Never silently": *Never compare metrics across different `mode` or
non-equivalent `scope`.* Elsewhere that rule is enforced by making people query
through `list_runs.py`. Here it is enforced by a suffix: every metric from a run
that was not full-scale production is drawn as `map50=0.92 [debug]`, and the
script's own comment says why -- *"an unlabeled debug number sitting next to
production ones on the same graph invites exactly the false comparison"*.

Deleting that suffix is a one-line edit, it breaks nothing, no test noticed, and
the result is a board where a debug 0.92 sits beside a production 0.84 and reads
as better. That is the whole reason this file exists.

The second half is the board's completeness. A run record it could not read used
to crash the render outright; dropping it silently instead would be worse, because
a board missing a node looks exactly like a board of everything.
"""
import os
import unittest

from helpers import TempDirCase, load_script, run_script

dag = load_script("shared/build_dag.py")
SCRIPT = "shared/build_dag.py"


class DagCase(TempDirCase):

    def run_rec(self, stage="training", run_id="run_001", **extra):
        rec = {"stage": stage, "run_id": run_id, "status": "done"}
        rec.update(extra)
        self.write_json(f"proj/stages/{stage}/runs/{run_id}/run.json", rec)

    def nodes(self):
        runs, unreadable = dag.load_runs(self.path("proj"))
        n, cross, fork = dag.build_graph(runs)
        return n, cross, fork, unreadable

    @property
    def root(self):
        return self.path("proj")


class ANumberOffAnotherScaleIsLabelled(DagCase):
    """CLAUDE.md -> "Never silently": *Never compare metrics across different
    `mode` or non-equivalent `scope`.*"""

    def test_a_debug_metric_carries_its_mode(self):
        self.run_rec(mode="debug", metrics={"mAP": 0.92})
        n, _c, _f, _u = self.nodes()
        self.assertIn("[debug]", n["training/run_001"]["key_metric"])

    def test_a_production_metric_is_the_only_unlabelled_one(self):
        """The label only means anything if production carries none."""
        self.run_rec(mode="production", metrics={"mAP": 0.84})
        n, _c, _f, _u = self.nodes()
        self.assertEqual(n["training/run_001"]["key_metric"], "mAP=0.84")

    def test_a_run_with_no_recorded_mode_says_scale_unknown(self):
        """Three facts, not two: unrecorded is not production, and it is not
        debug either. Rendering it bare would read as production."""
        self.run_rec(metrics={"mAP": 0.91})
        n, _c, _f, _u = self.nodes()
        self.assertIn("[scale?]", n["training/run_001"]["key_metric"])

    def test_the_two_appear_differently_on_one_board(self):
        """The single assertion that fails if the suffix is ever dropped: the
        same number from two scales must not render identically."""
        self.run_rec(run_id="run_001", mode="production", metrics={"mAP": 0.9})
        self.run_rec(run_id="run_002", mode="debug", metrics={"mAP": 0.9})
        n, _c, _f, _u = self.nodes()
        self.assertNotEqual(n["training/run_001"]["key_metric"],
                            n["training/run_002"]["key_metric"])

    def test_a_run_with_no_metrics_shows_none_rather_than_a_zero(self):
        self.run_rec(mode="production", metrics={})
        n, _c, _f, _u = self.nodes()
        self.assertEqual(n["training/run_001"]["key_metric"], "")


class TheBoardSaysWhatIsNotOnIt(DagCase):
    """CLAUDE.md -> "Never silently": *Never report data you could not look at.*
    A board missing a node is indistinguishable from a board of everything."""

    def test_an_unreadable_record_is_reported_not_dropped(self):
        self.run_rec(run_id="run_001", mode="production", metrics={"mAP": 0.8})
        self.write("proj/stages/training/runs/run_002/run.json", "{truncated")
        n, _c, _f, unreadable = self.nodes()
        self.assertEqual(sorted(n), ["training/run_001"])
        self.assertEqual(len(unreadable), 1)

    def test_an_unreadable_record_does_not_crash_the_render(self):
        self.run_rec(run_id="run_001", mode="production", metrics={"mAP": 0.8})
        self.write("proj/stages/training/runs/run_002/run.json", "{truncated")
        rc, _out, _err = run_script(SCRIPT, self.root,
                                    "--output", self.path("dag.html"))
        self.assertTrue(os.path.isfile(self.path("dag.html")),
                        "one bad record must not cost the whole board")
        self.assertEqual(rc, 1, "and the exit code must not call it complete")

    def test_a_clean_project_is_exit_0(self):
        self.run_rec(mode="production", metrics={"mAP": 0.8})
        rc, _out, _err = run_script(SCRIPT, self.root, "--output", self.path("dag.html"))
        self.assertEqual(rc, 0)


class EdgesComeFromBothRecordedShapes(DagCase):
    """`lineage.parents` occurs as bare strings in older records and as
    `{stage, run_id}` objects in current ones. Reading only one shape drops
    real edges from the board without anything raising."""

    def test_a_dict_parent_becomes_an_edge(self):
        self.run_rec(stage="training", run_id="run_001")
        self.run_rec(stage="evaluation", run_id="run_002",
                     lineage={"parents": [{"stage": "training", "run_id": "run_001"}]})
        _n, cross, _f, _u = self.nodes()
        self.assertIn({"from": "training/run_001", "to": "evaluation/run_002"}, cross)

    def test_a_string_parent_becomes_an_edge_too(self):
        self.run_rec(stage="training", run_id="run_001")
        self.run_rec(stage="evaluation", run_id="run_002",
                     lineage={"parents": ["training/run_001"]})
        _n, cross, _f, _u = self.nodes()
        self.assertIn({"from": "training/run_001", "to": "evaluation/run_002"}, cross)

    def test_a_fork_is_a_different_edge_kind(self):
        self.run_rec(run_id="run_001")
        self.run_rec(run_id="run_002", lineage={"fork_of": "training/run_001"})
        _n, cross, fork, _u = self.nodes()
        self.assertIn({"from": "training/run_001", "to": "training/run_002"}, fork)
        self.assertEqual(cross, [])

    def test_a_manifest_json_is_read_when_there_is_no_run_json(self):
        self.write_json("proj/stages/inference/runs/run_007/manifest.json",
                        {"status": "done"})
        n, _c, _f, _u = self.nodes()
        self.assertIn("inference/run_007", n,
                      "stage and run_id fall back to the directory names")


class NothingFromARecordReachesTheHtmlUnescaped(DagCase):
    """The output is a self-contained HTML file the user opens in a browser, and
    `alias` / `description` are free text somebody typed. An unescaped `<script>`
    in a run description is a live injection into a file that gets shared."""

    def test_a_description_is_escaped(self):
        self.run_rec(mode="production",
                     description="<script>alert('x')</script>",
                     metrics={"mAP": 0.8})
        runs, _u = dag.load_runs(self.root)
        n, c, f = dag.build_graph(runs)
        out = dag.generate_html(n, c, f, "proj")
        self.assertNotIn("<script>alert", out)
        self.assertIn("&lt;script&gt;", out)

    def test_an_alias_is_escaped(self):
        self.run_rec(mode="production", alias="<img onerror=1>", metrics={"mAP": 0.8})
        runs, _u = dag.load_runs(self.root)
        n, c, f = dag.build_graph(runs)
        self.assertNotIn("<img onerror", dag.generate_html(n, c, f, "proj"))


if __name__ == "__main__":
    unittest.main()
