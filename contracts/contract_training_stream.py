"""Regression tests for the three training-stream reconciliations.

These cover the P0 items whose common shape is: a number gets recorded, nothing
errors, and the number is wrong. Specifically —

  reconcile_metrics   the config claims a field the stream does not emit, or
                      emits under a different name, or on the wrong split
  select_checkpoint   the ranking that picks the canonical model
  retention           the only irreversible operation in the system
"""
import json
import os
import sys
import unittest

from helpers import SCRIPTS, TempDirCase, load_script, run_script

sys.path.insert(0, os.path.join(SCRIPTS, "train-run"))

rm = load_script("train-run/reconcile_metrics.py")
sc = load_script("train-run/select_checkpoint.py")
rt = load_script("train-run/retention.py")

# val_acc peaks at epoch 3, so "best" is never "last" and never "first".
ACCS = {1: 0.80, 2: 0.88, 3: 0.94, 4: 0.91, 5: 0.90}


def stream_records(accs=None, done=True):
    accs = accs or ACCS
    out = []
    for e, a in accs.items():
        out.append({"type": "train_step", "step": e * 100, "loss": 1.0 / e, "lr": 1e-3})
        out.append({"type": "val_epoch", "epoch": e, "val_loss": 1.5 - a, "val_acc": a})
    if done:
        out.append({"type": "done", "epoch": max(accs)})
    return out


def output_json(**over):
    cfg = {
        "checkpoints": {
            "path_pattern": "<output_dir>/checkpoint-{epoch}.pt",
            "selection": {"best_by": "val_acc", "direction": "max"},
            "retention": "keep_best_and_last",
        },
        "metrics": {
            "log_path": "train_log.jsonl",
            "record_types": {
                "train_step": {"fields": ["step", "loss", "lr"]},
                "val_epoch": {"fields": ["epoch", "val_loss", "val_acc"]},
                "done": {"fields": ["epoch"]},
            },
            "watch_step": ["loss", "lr"],
            "watch_epoch": ["val_loss", "val_acc"],
            "primary_metric": "val_acc",
            "direction": "max",
            "done_signal": {"type": "record", "record_type": "done"},
        },
    }
    for path, value in over.items():
        node, key = cfg, None
        for key in path.split("."):
            if key == path.split(".")[-1]:
                break
            node = node[key]
        node[key] = value
    return cfg


class Fixture(TempDirCase):
    def setUp(self):
        super().setUp()
        self.out_dir = self.path("out")
        os.makedirs(self.out_dir)

    def make_stream(self, records=None, name="train_log.jsonl"):
        recs = stream_records() if records is None else records
        self.write(name, "".join(json.dumps(r) + "\n" for r in recs))
        return self.path(name)

    def make_ckpts(self, epochs=ACCS, size=1000):
        for e in epochs:
            with open(os.path.join(self.out_dir, f"checkpoint-{e}.pt"), "w") as f:
                f.write("w" * size)

    def codes(self, findings):
        return {f["code"] for f in findings}


class ReconcileMetrics(Fixture):
    """run-mechanics.md -> "Record integrity": the metric schema in output.json must
    describe the stream the code actually emits. A schema naming a field the code
    never writes, or naming a training-split metric for checkpoint selection, yields
    a run that completes and records the wrong number.
    """
    def test_matching_schema_is_ok(self):
        r = rm.reconcile(output_json(), stream_records(), [])
        self.assertEqual(r["verdict"], "ok", r["findings"])

    def test_declared_metric_absent_from_stream(self):
        """The named failure: config says one thing, the code emits another."""
        r = rm.reconcile(output_json(**{"metrics.primary_metric": "val_f1"}),
                         stream_records(), [])
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("primary_metric_absent", self.codes(r["findings"]))

    def test_absent_metric_suggests_the_near_miss(self):
        """`val_loss` declared against a stream that only has `loss` — the
        suggestion is what turns a puzzling failure into an obvious one."""
        recs = [{"type": "val_epoch", "epoch": e, "loss": 1 - a} for e, a in ACCS.items()]
        r = rm.reconcile(output_json(**{"metrics.primary_metric": "val_loss",
                                        "metrics.direction": "min"}), recs, [])
        finding = next(f for f in r["findings"] if f["code"] == "primary_metric_absent")
        self.assertIn("loss", finding["did_you_mean"])

    def test_train_split_metric_used_for_selection(self):
        """train_loss-as-val_loss: present, numeric, plausible — and the wrong split."""
        recs = [{"type": "e", "epoch": e, "train_loss": 1 - a, "val_loss": 1.2 - a}
                for e, a in ACCS.items()]
        r = rm.reconcile(output_json(**{"metrics.primary_metric": "train_loss",
                                        "metrics.direction": "min",
                                        "metrics.record_types": {"e": {"fields": ["epoch"]}},
                                        "metrics.watch_step": [], "metrics.watch_epoch": [],
                                        "metrics.done_signal": {}}), recs, [])
        self.assertEqual(r["verdict"], "fail")
        f = next(f for f in r["findings"] if f["code"] == "primary_metric_is_train_split")
        self.assertIn("val_loss", f["held_out_alternatives"])

    def test_unprefixed_metric_with_a_held_out_sibling_warns(self):
        r = rm.reconcile(output_json(**{"metrics.primary_metric": "loss",
                                        "metrics.direction": "min"}), stream_records(), [])
        self.assertIn("primary_metric_split_ambiguous", self.codes(r["findings"]))

    def test_direction_contradicting_the_name(self):
        """Rank a loss by `max` and selection returns the worst checkpoint."""
        r = rm.reconcile(output_json(**{"metrics.primary_metric": "val_loss",
                                        "metrics.direction": "max"}), stream_records(), [])
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("direction_contradicts_name", self.codes(r["findings"]))

    def test_declared_record_type_never_emitted(self):
        r = rm.reconcile(output_json(), stream_records(done=False), [])
        self.assertIn("record_type_never_emitted", self.codes(r["findings"]))

    def test_declared_field_never_emitted(self):
        cfg = output_json()
        cfg["metrics"]["record_types"]["val_epoch"]["fields"].append("val_map")
        r = rm.reconcile(cfg, stream_records(), [])
        self.assertIn("field_never_emitted", self.codes(r["findings"]))

    def test_watched_field_absent(self):
        r = rm.reconcile(output_json(**{"metrics.watch_epoch": ["val_acc", "val_mae"]}),
                         stream_records(), [])
        self.assertIn("watched_field_absent", self.codes(r["findings"]))

    def test_empty_stream_fails(self):
        r = rm.reconcile(output_json(), [], [])
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("empty_stream", self.codes(r["findings"]))

    def test_type_key_is_inferred_not_assumed(self):
        """A codebase tagging records with `event` instead of `type` must still
        classify — guessing wrong would misattribute every record."""
        recs = [{"event": "val_epoch", "epoch": e, "val_loss": 1.5 - a, "val_acc": a}
                for e, a in ACCS.items()]
        cfg = output_json(**{"metrics.record_types": {"val_epoch": {"fields": ["epoch"]}},
                             "metrics.watch_step": [], "metrics.done_signal": {}})
        r = rm.reconcile(cfg, recs, [])
        self.assertEqual(r["type_key"], "event")
        self.assertEqual(r["record_types"]["val_epoch"], 5)

    def test_type_key_ties_resolve_to_the_first_candidate(self):
        """Two fields both naming declared types. The winner must not depend on
        which one the scan happened to reach first, or the same stream classifies
        one way today and another way tomorrow."""
        recs = [{"type": "val_epoch", "phase": "val_epoch", "epoch": e, "val_acc": a}
                for e, a in ACCS.items()]
        cfg = output_json(**{"metrics.record_types": {"val_epoch": {"fields": ["epoch"]}},
                             "metrics.watch_step": [], "metrics.watch_epoch": [],
                             "metrics.done_signal": {}})
        r = rm.reconcile(cfg, recs, [])
        self.assertEqual(r["type_key"], "type")
        self.assertEqual(r["record_types"]["val_epoch"], 5)

    def test_direction_alias_is_read_not_rejected(self):
        """`higher_is_better` is a direction /eval-run reads happily. One config
        key must not be legal there and fatal here — `shared/compare.py` is the
        single vocabulary, and a `fail` here would be exit 1, which no skill may
        fall back around."""
        r = rm.reconcile(output_json(**{"metrics.direction": "higher_is_better"}),
                         stream_records(), [])
        self.assertEqual(r["verdict"], "ok", r["findings"])
        self.assertEqual(r["direction"], "max")

    def test_unrecognized_direction_still_fails(self):
        r = rm.reconcile(output_json(**{"metrics.direction": "sideways"}),
                         stream_records(), [])
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("direction_invalid", self.codes(r["findings"]))

    def test_cli_fail_verdict_exits_1_without_traceback(self):
        self.write_json("output.json", output_json(**{"metrics.primary_metric": "nope"}))
        stream = self.make_stream()
        rc, out, err = run_script("train-run/reconcile_metrics.py",
                                  self.path("output.json"), stream)
        self.assertEqual(rc, 1)
        self.assertEqual(out["verdict"], "fail")
        self.assertNotIn("Traceback", err)


class SelectCheckpoint(Fixture):
    """run-mechanics.md -> "Record integrity": the chosen checkpoint and the recorded
    metric must describe the same artifact. Ranking is reported with the values as
    they appear in the stream, because a path alone is not reviewable.
    """
    def test_picks_the_peak_not_the_last(self):
        self.make_ckpts()
        r = sc.select(output_json(), stream_records(), self.out_dir)
        self.assertEqual(r["verdict"], "ok", r["findings"])
        self.assertTrue(r["chosen"]["path"].endswith("checkpoint-3.pt"))
        self.assertEqual(r["chosen"]["value"], 0.94)

    def test_ranking_reports_the_actual_jsonl_values(self):
        """The reconciliation itself: the ranking is reviewable against the log."""
        self.make_ckpts()
        r = sc.select(output_json(), stream_records(), self.out_dir)
        self.assertEqual([(e["epoch"], e["value"]) for e in r["ranking"]],
                         [(3, 0.94), (4, 0.91), (5, 0.90), (2, 0.88), (1, 0.80)])

    def test_evidence_record_is_the_raw_jsonl_line(self):
        self.make_ckpts()
        r = sc.select(output_json(), stream_records(), self.out_dir)
        self.assertEqual(r["chosen"]["evidence_record"]["epoch"], 3)
        self.assertEqual(r["chosen"]["evidence_record"]["val_acc"], 0.94)

    def test_min_direction_picks_the_other_end(self):
        self.make_ckpts()
        cfg = output_json(**{"checkpoints.selection": {"best_by": "val_loss", "direction": "min"}})
        r = sc.select(cfg, stream_records(), self.out_dir)
        self.assertTrue(r["chosen"]["path"].endswith("checkpoint-3.pt"))

    def test_falling_through_to_second_best_names_the_value_to_record(self):
        """`save_every=2` legitimately skips the peak epoch. Falling through is
        fine; recording the stream's peak next to the surviving artifact is not."""
        self.make_ckpts(epochs=[1, 2, 4, 5])          # epoch 3, the best, never saved
        r = sc.select(output_json(), stream_records(), self.out_dir)

        self.assertEqual(r["verdict"], "warn")
        self.assertTrue(r["chosen"]["path"].endswith("checkpoint-4.pt"))
        f = next(f for f in r["findings"] if f["code"] == "best_record_skipped")
        self.assertEqual(f["record_this_value"], 0.91)
        self.assertNotEqual(f["record_this_value"], f["top_value"])

    def test_no_ranked_record_has_a_file_is_a_failure(self):
        self.make_ckpts(epochs=[97, 98])              # files exist, none of them logged
        r = sc.select(output_json(), stream_records(), self.out_dir)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("best_record_has_no_file", {f["code"] for f in r["findings"]})

    def test_files_without_a_metric_are_reported(self):
        self.make_ckpts()
        self.make_ckpts(epochs=[9])                   # saved but never logged
        r = sc.select(output_json(), stream_records(), self.out_dir)
        self.assertIn("checkpoints_without_a_metric", {f["code"] for f in r["findings"]})

    def test_best_by_differing_from_primary_metric_warns(self):
        self.make_ckpts()
        cfg = output_json(**{"checkpoints.selection": {"best_by": "val_loss", "direction": "min"}})
        r = sc.select(cfg, stream_records(), self.out_dir)
        self.assertIn("best_by_differs_from_primary", {f["code"] for f in r["findings"]})

    def test_script_saved_best_that_disagrees_is_surfaced(self):
        self.make_ckpts()
        with open(os.path.join(self.out_dir, "best.pt"), "w") as f:
            f.write("w")
        r = sc.select(output_json(), stream_records(), self.out_dir)
        self.assertIn("script_saved_best_differs", {f["code"] for f in r["findings"]})

    def test_no_checkpoints_on_disk_fails(self):
        r = sc.select(output_json(), stream_records(), self.out_dir)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("no_checkpoints_on_disk", {f["code"] for f in r["findings"]})

    def test_direction_alias_ranks_the_same_as_max(self):
        """A direction spelled the way an eval config spells it still ranks."""
        self.make_ckpts()
        cfg = output_json(**{"checkpoints.selection": {"best_by": "val_acc",
                                                       "direction": "higher_is_better"}})
        r = sc.select(cfg, stream_records(), self.out_dir)
        self.assertNotIn("direction_invalid", {f["code"] for f in r["findings"]})
        self.assertEqual(r["direction"], "max")
        self.assertTrue(r["chosen"]["path"].endswith("checkpoint-3.pt"))

    def test_tie_at_the_top_is_flagged(self):
        self.make_ckpts(epochs={1: 0.9, 2: 0.9})
        r = sc.select(output_json(), stream_records({1: 0.9, 2: 0.9}), self.out_dir)
        self.assertIn("tie_at_top", {f["code"] for f in r["findings"]})


class RetentionPlan(Fixture):
    """run-mechanics.md -> "Record integrity": retention is the only irreversible
    operation in MLClaw. Planning never deletes, and refuses rather than guesses —
    above all, never delete a file that cannot be ranked.
    """

    def plan(self, cfg=None, records=None):
        self.make_ckpts()
        return rt.build_plan(cfg or output_json(), records or stream_records(), self.out_dir)

    def test_plan_deletes_nothing(self):
        before = sorted(os.listdir(self.out_dir))
        self.plan()
        self.assertEqual(sorted(os.listdir(self.out_dir)), before or sorted(os.listdir(self.out_dir)))
        self.assertEqual(len(os.listdir(self.out_dir)), 5)

    def test_every_decision_carries_the_number_that_decided_it(self):
        """A filename list is not reviewable; a filename plus its metric is."""
        plan = self.plan()
        for d in plan["decisions"]:
            self.assertEqual(d["metric_name"], "val_acc")
            self.assertIsNotNone(d["metric"], d)
            self.assertIn(d["fate"], ("keep", "delete"))
            self.assertTrue(d["reason"])

    def test_keep_best_and_last(self):
        plan = self.plan()
        kept = {os.path.basename(d["path"]) for d in plan["decisions"] if d["fate"] == "keep"}
        self.assertEqual(kept, {"checkpoint-3.pt", "checkpoint-5.pt"})

    def test_keep_best_only(self):
        plan = self.plan(output_json(**{"checkpoints.retention": "keep_best_only"}))
        kept = {os.path.basename(d["path"]) for d in plan["decisions"] if d["fate"] == "keep"}
        self.assertEqual(kept, {"checkpoint-3.pt"})

    def test_keep_all_deletes_nothing(self):
        plan = self.plan(output_json(**{"checkpoints.retention": "keep_all"}))
        self.assertEqual(plan["summary"]["delete"], 0)

    def test_keep_last_n_always_keeps_the_best(self):
        plan = self.plan(output_json(**{"checkpoints.retention": {"policy": "keep_last_n", "n": 2}}))
        kept = {os.path.basename(d["path"]) for d in plan["decisions"] if d["fate"] == "keep"}
        self.assertIn("checkpoint-3.pt", kept, "the best must survive every policy")
        self.assertEqual(kept, {"checkpoint-3.pt", "checkpoint-4.pt", "checkpoint-5.pt"})

    def test_best_is_the_best_checkpoint_that_exists(self):
        """`save_every=N` skipped the peak epoch. The keeper is the best file that
        was actually written — the same one `select_checkpoint` falls through to,
        because both read one join, not two."""
        self.make_ckpts(epochs=[1, 2, 4, 5])          # epoch 3, the peak, never saved
        plan = rt.build_plan(output_json(), stream_records(), self.out_dir)
        self.assertEqual(os.path.basename(plan["best"]["path"]), "checkpoint-4.pt")
        self.assertEqual(plan["best"]["value"], 0.91)
        kept = {os.path.basename(d["path"]) for d in plan["decisions"] if d["fate"] == "keep"}
        self.assertEqual(kept, {"checkpoint-4.pt", "checkpoint-5.pt"})

    # --- the five abort checks -------------------------------------------
    def test_abort_when_ranking_has_a_failure(self):
        plan = self.plan(output_json(
            **{"checkpoints.selection": {"best_by": "val_acc", "direction": "min"}}))
        self.assertEqual(plan["verdict"], "refused")
        self.assertIn("ranking_unreliable", {f["code"] for f in plan["findings"]})
        self.assertNotIn("confirm_token", plan)

    def test_abort_when_a_deletion_target_has_no_metric(self):
        """Never delete what you cannot rank."""
        self.make_ckpts()
        self.make_ckpts(epochs=[0])          # epoch 0: unranked, and would be deleted
        plan = rt.build_plan(output_json(), stream_records(), self.out_dir)
        self.assertEqual(plan["verdict"], "refused")
        self.assertIn("deleting_unranked_files", {f["code"] for f in plan["findings"]})
        self.assertEqual(len(os.listdir(self.out_dir)), 6, "nothing may be deleted")

    def test_abort_when_no_best_can_be_identified(self):
        self.make_ckpts()
        recs = [{"type": "val_epoch", "epoch": e} for e in ACCS]   # no val_acc anywhere
        plan = rt.build_plan(output_json(), recs, self.out_dir)
        self.assertEqual(plan["verdict"], "refused")

    def test_unknown_policy_is_refused(self):
        plan = self.plan(output_json(**{"checkpoints.retention": "keep_the_good_ones"}))
        self.assertEqual(plan["verdict"], "refused")
        self.assertIn("policy_unknown", {f["code"] for f in plan["findings"]})

    def test_refused_plan_carries_no_token(self):
        plan = self.plan(output_json(**{"checkpoints.retention": "nonsense"}))
        self.assertIsNone(plan.get("confirm_token"))


class RetentionApply(Fixture):
    """run-mechanics.md -> "Record integrity": deletion re-verifies the directory against
    the plan and aborts wholly on any drift. Partial deletion against a stale ranking
    is the failure mode this split exists to prevent.
    """
    def ready_plan(self):
        self.make_ckpts()
        plan = rt.build_plan(output_json(), stream_records(), self.out_dir)
        self.assertEqual(plan["verdict"], "ready")
        return plan

    def test_correct_token_deletes_exactly_the_planned_files(self):
        plan = self.ready_plan()
        report, code = rt.apply_plan(plan, plan["confirm_token"])
        self.assertEqual(code, 0)
        self.assertTrue(report["applied"])
        self.assertEqual(sorted(os.listdir(self.out_dir)),
                         ["checkpoint-3.pt", "checkpoint-5.pt"])

    def test_wrong_token_deletes_nothing(self):
        plan = self.ready_plan()
        report, code = rt.apply_plan(plan, "deadbeefdeadbeef")
        self.assertFalse(report["applied"])
        self.assertEqual(code, 1)
        self.assertEqual(len(os.listdir(self.out_dir)), 5)

    def test_modified_file_blocks_the_whole_apply(self):
        """Drift means the ranking may no longer describe what is on disk."""
        plan = self.ready_plan()
        with open(os.path.join(self.out_dir, "checkpoint-2.pt"), "a") as f:
            f.write("more")

        report, code = rt.apply_plan(plan, plan["confirm_token"])

        self.assertFalse(report["applied"])
        self.assertTrue(any("changed" in d for d in report["drift"]))
        self.assertEqual(len(os.listdir(self.out_dir)), 5, "no partial deletion")

    def test_new_checkpoint_after_planning_blocks_apply(self):
        plan = self.ready_plan()
        self.make_ckpts(epochs=[6])
        report, code = rt.apply_plan(plan, plan["confirm_token"])
        self.assertFalse(report["applied"])
        self.assertEqual(len(os.listdir(self.out_dir)), 6)

    def test_vanished_file_blocks_apply(self):
        plan = self.ready_plan()
        os.remove(os.path.join(self.out_dir, "checkpoint-1.pt"))
        report, code = rt.apply_plan(plan, plan["confirm_token"])
        self.assertFalse(report["applied"])
        self.assertTrue(any("gone" in d for d in report["drift"]))

    def test_refused_plan_cannot_be_applied(self):
        self.make_ckpts()
        plan = rt.build_plan(output_json(**{"checkpoints.retention": "nonsense"}),
                             stream_records(), self.out_dir)
        report, code = rt.apply_plan(plan, "whatever")
        self.assertFalse(report["applied"])
        self.assertEqual(len(os.listdir(self.out_dir)), 5)


if __name__ == "__main__":
    unittest.main()
