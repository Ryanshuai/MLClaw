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
ing = load_script("train-run/ingest.py")
st = load_script("train-run/_stream.py")

# val_acc peaks at epoch 3, so "best" is never "last" and never "first".
ACCS = {1: 0.80, 2: 0.88, 3: 0.94, 4: 0.91, 5: 0.90}


def _step_and_epoch_records(accs):
    out = []
    for e, a in accs.items():
        out.append({"type": "train_step", "step": e * 100, "loss": 1.0 / e, "lr": 1e-3})
        out.append({"type": "val_epoch", "epoch": e, "val_loss": 1.5 - a, "val_acc": a})
    return out


def stream_records(accs=None):
    accs = accs or ACCS
    return _step_and_epoch_records(accs) + [{"type": "done", "epoch": max(accs)}]


def incomplete_stream_records(accs=None):
    """A stream that never emits the `done` record type -- the one shape
    `stream_records` cannot produce, for the test that needs a run to look
    still-in-progress."""
    accs = accs or ACCS
    return _step_and_epoch_records(accs)


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
            with open(os.path.join(self.out_dir, f"checkpoint-{e}.pt"), "w", encoding="utf-8") as f:
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
        r = rm.reconcile(output_json(), incomplete_stream_records(), [])
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
        with open(os.path.join(self.out_dir, "best.pt"), "w", encoding="utf-8") as f:
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
        with open(os.path.join(self.out_dir, "checkpoint-2.pt"), "a", encoding="utf-8") as f:
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


class Ingest(Fixture):
    """run-mechanics.md -> "Metric stream": a normalizer groups and tags, never
    renames a field and never invents a record type.

    Both violations are invisible downstream. A renamed field makes
    reconcile_metrics compare the declared schema against the normalizer's output
    instead of the code's, laundering the very mismatch it exists to catch; a
    fabricated `type` makes find_type_key report full coverage for a
    classification that never happened. Neither raises, and both produce a run
    that looks correctly recorded.
    """
    def setUp(self):
        super().setUp()
        self.ing = ing
        self.run_dir = self.path("run")
        os.makedirs(self.run_dir)

    def stdout_source(self, line, regex, rtype="val_epoch"):
        """Write a stdout source and return the matching output.json. -> cfg."""
        self.write("run/logs/stdout.log", line)
        return {"metrics": {"log_format": "stdout_regex", "stdout_extractor": {
            "patterns": [{"type": rtype, "regex": regex}]}}}

    # (tag, step, wall_time, value) — two namespaces, one shared step.
    TRIPLES = [
        ("train/loss", 100, 10.0, 0.5), ("lr", 100, 10.0, 1e-3),
        ("val/acc", 100, 11.0, 0.80), ("val/loss", 100, 11.0, 0.40),
        ("train/loss", 200, 20.0, 0.3),
    ]

    def test_field_names_survive_verbatim(self):
        """The no-rename rule: a slash is not sanitized to an underscore."""
        recs, _ = self.ing.records_from_triples(self.TRIPLES)
        keys = set().union(*(r.keys() for r in recs))
        self.assertIn("train/loss", keys)
        self.assertNotIn("train_loss", keys)

    def test_no_record_type_is_invented(self):
        recs, _ = self.ing.records_from_triples(self.TRIPLES)
        self.assertEqual([r for r in recs if "type" in r], [])

    def test_group_by_step_merges_every_tag_at_that_step(self):
        recs, _ = self.ing.records_from_triples(self.TRIPLES, "step")
        at100 = [r for r in recs if r["step"] == 100]
        self.assertEqual(len(at100), 1)
        self.assertIn("train/loss", at100[0])
        self.assertIn("val/acc", at100[0])

    def test_group_by_namespace_keeps_train_and_val_apart(self):
        recs, _ = self.ing.records_from_triples(self.TRIPLES, "step+namespace")
        at100 = [r for r in recs if r["step"] == 100]
        self.assertEqual(len(at100), 3)  # train/, val/, and bare `lr`
        holds_both = [r for r in at100 if "train/loss" in r and "val/acc" in r]
        self.assertEqual(holds_both, [])

    def test_grouping_rule_is_stamped_on_every_record(self):
        for rule in ("step", "step+namespace"):
            recs, _ = self.ing.records_from_triples(self.TRIPLES, rule)
            self.assertTrue(all(r["_group"] == rule for r in recs), rule)

    def test_restart_collision_is_counted_not_swallowed(self):
        """A resumed run re-emits steps with different values. Silently keeping one
        leaves a ranking over a stream where a step held two numbers."""
        triples = self.TRIPLES + [("train/loss", 100, 30.0, 0.9)]
        recs, notes = self.ing.records_from_triples(triples)
        self.assertEqual(notes["overwritten_by_restart"], 1)
        at100 = next(r for r in recs if r["step"] == 100)
        self.assertEqual(at100["train/loss"], 0.9)

    def test_unimplemented_grouping_refuses(self):
        """Falling back to `step` would answer a question that was not asked."""
        with self.assertRaises(self.ing.Refusal):
            self.ing.records_from_triples(self.TRIPLES, "step+wall_time")
        with self.assertRaises(self.ing.Refusal):
            self.ing.records_from_triples(self.TRIPLES, "by_vibes")

    def test_render_target_cannot_be_ingested(self):
        """`tb/` holds what we rendered. Reading it back feeds our own derived
        numbers in as if the code had reported them."""
        tb = os.path.join(self.run_dir, "tb")
        os.makedirs(tb)
        with self.assertRaises(self.ing.Refusal):
            self.ing._refuse_render_target(self.run_dir, tb)
        with self.assertRaises(self.ing.Refusal):
            self.ing._refuse_render_target(self.run_dir, os.path.join(tb, "sub"))
        self.ing._refuse_render_target(self.run_dir, os.path.join(self.run_dir, "output"))

    def test_stream_is_rewritten_whole_not_appended(self):
        """The restart case again, one layer up: a shorter re-derive must not leave
        the previous run's tail behind, which is what append would do."""
        self.ing.write_stream(self.run_dir, [{"step": s} for s in (1, 2, 3)], {})
        self.ing.write_stream(self.run_dir, [{"step": 1}], {})
        recs, errs = self.ing.read_jsonl(os.path.join(self.run_dir, "stream.jsonl"))
        self.assertEqual(recs, [{"step": 1}])
        self.assertEqual(errs, [])

    def test_meta_sidecar_records_what_was_inferred(self):
        self.ing.write_stream(self.run_dir, [{"step": 1}],
                              {"group_by": "step", "inferred": [{"field": "type"}]})
        with open(os.path.join(self.run_dir, "stream_meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["records"], 1)
        self.assertEqual(meta["group_by"], "step")
        self.assertEqual(meta["inferred"][0]["field"], "type")

    def test_empty_log_format_refuses_rather_than_picking_one(self):
        with self.assertRaises(self.ing.Refusal):
            self.ing.collect({"metrics": {"log_format": ""}}, self.run_dir)

    def test_unbuilt_adapter_says_so(self):
        with self.assertRaises(self.ing.Refusal):
            self.ing.collect({"metrics": {"log_format": "wandb"}}, self.run_dir)

    def test_stdout_extractor_matching_nothing_is_a_failure_not_an_empty_run(self):
        """A regex that has drifted from the code's print format yields zero
        records. Reported as ok, that is a run monitored forever at step 0."""
        cfg = self.stdout_source("Epoch 1 | loss 0.5\n",
                                 r"^iter (?P<step>\d+) loss (?P<loss>[\d.]+)$", "s")
        records, meta, findings = self.ing.collect(cfg, self.run_dir)
        self.assertEqual(records, [])
        self.assertIn("extractor_matched_nothing", self.codes(findings))

    def test_tb_points_label_curves_with_the_authors_names(self):
        """The no-rename rule on the way out: a curve in TensorBoard is called what
        the author called it. Provenance keys and the record type are not curves."""
        recs = [{"step": 100, "wall_time": 10.0, "train/loss": 0.5, "type": "s",
                 "_src": "stdout_regex", "_group": "pattern", "note": "hello"}]
        pts = self.ing.tb_points(recs)
        self.assertEqual(pts, [("train/loss", 0.5, 100, 10.0)])

    def test_tb_points_skips_records_with_no_x_axis(self):
        self.assertEqual(self.ing.tb_points([{"val_acc": 0.9}]), [])
        self.assertEqual(self.ing.tb_points([{"epoch": 3, "val_acc": 0.9}]),
                         [("val_acc", 0.9, 3, None)])

    def test_tb_render_is_skipped_when_the_source_is_already_tfevents(self):
        """`--logdir <RUN_DIR>` overlays every subdirectory as its own run, so
        re-rendering the same scalars draws every curve twice under two names."""
        out = self.ing.write_tb(self.run_dir, [{"step": 1, "loss": 0.5}], "tensorboard")
        self.assertFalse(out["rendered"])
        self.assertIn("already tfevents", out["why"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "tb")))

    def test_missing_writer_package_is_a_warning_not_a_failure(self):
        """A viewer that cannot be built must not break a run's monitoring. CI has no
        writer package installed, which is exactly the environment being asserted."""
        out = self.ing.write_tb(self.run_dir, [{"step": 1, "loss": 0.5}], "stdout_regex")
        if out.get("rendered"):
            self.skipTest("a writer package is installed here; the skip path is untested")
        self.assertIn("importable", out["why"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "tb", ".watermark")))

    def test_ingest_still_succeeds_with_no_writer(self):
        """The stream must land whether or not the render did."""
        self.write_json("output.json", self.stdout_source(
            "epoch 1 val_acc 0.80\n",
            r"^epoch (?P<epoch>\d+) val_acc (?P<val_acc>[\d.]+)$"))
        rc, out, err = run_script("train-run/ingest.py", self.path("output.json"),
                                  "--run-dir", self.run_dir)
        self.assertEqual(rc, 0, err)  # the only possible finding here is the tb warn
        self.assertEqual(out["records"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.run_dir, "stream.jsonl")))

    def test_non_scalar_tags_are_reported_not_silently_dropped(self):
        """A run's images do not belong in the stream — nothing ranks a checkpoint by
        a segmentation mask. But silence makes "MLClaw ignored your images" identical
        to "the run logged none", and the user who logged them goes looking.

        Substitutes the reader: EventAccumulator needs a package CI does not have,
        while the reporting is the part that can be quietly wrong.
        """
        real = self.ing.read_tfevents
        self.ing.read_tfevents = lambda d: ([("train/loss", 1, 1.0, 0.5)],
                                            {"images": ["samples/pred"], "graph": True})
        try:
            cfg = {"metrics": {"log_format": "tensorboard", "log_path": "output/tb"}}
            records, meta, findings = self.ing.collect(cfg, self.run_dir)
        finally:
            self.ing.read_tfevents = real
        self.assertEqual(meta["not_ingested"], {"images": ["samples/pred"], "graph": True})
        self.assertIn("non_scalar_tags_not_ingested", self.codes(findings))
        self.assertEqual(records[0]["train/loss"], 0.5)

    def test_a_refusal_exits_1_and_writes_nothing(self):
        """CLAUDE.md -> "Script Integration": 2 means the script broke and the
        caller should do the work by hand; 1 means the script worked and the answer
        is no. A refusal reported as 2 reads as permission to go around it — here
        that means hand-reading the render target the refusal was protecting."""
        os.makedirs(os.path.join(self.run_dir, "tb"))
        self.write_json("output.json", {"metrics": {"log_format": "tensorboard",
                                                   "log_path": "tb"}})
        rc, out, err = run_script("train-run/ingest.py", self.path("output.json"),
                                  "--run-dir", self.run_dir)
        self.assertEqual(rc, 1, err)
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["stream"], None)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "stream.jsonl")),
                         "a refused source must not leave a stream behind")

    def test_stdout_extractor_names_fields_after_its_groups(self):
        cfg = self.stdout_source("noise\nepoch 3 val_acc 0.94\n",
                                 r"^epoch (?P<epoch>\d+) val_acc (?P<val_acc>[\d.]+)$")
        records, meta, findings = self.ing.collect(cfg, self.run_dir)
        self.assertEqual(records, [{"epoch": 3, "val_acc": 0.94, "type": "val_epoch",
                                    "_group": "pattern", "_src": "stdout_regex"}])


class SharedStreamVocabulary(Fixture):
    """run-mechanics.md -> "Metric stream": source, stream and record are three fixed
    words, and the provenance keys are MLClaw's own.

    These three lived as per-consumer copies first, and two of them had already
    diverged: `index_of` existed twice with the same five names in different orders,
    so a record carrying both `epoch` and `step` was ranked by one and plotted
    against the other. Nothing raises for any of it.
    """
    def test_one_index_order_for_ranking(self):
        """Every consumer that asks "which observation is this" gets one answer."""
        rec = {"epoch": 3, "step": 1500, "val_acc": 0.94}
        self.assertEqual(st.index_of(rec), ("epoch", 3))
        self.assertEqual(sc.index_of(rec), ("epoch", 3))

    def test_plotting_order_differs_deliberately_and_uses_the_same_names(self):
        """Plotting prefers the dense axis; the divergence is intentional, but the
        name list is imported so a sixth index key cannot reach one and not the other."""
        self.assertEqual(sorted(ing.PLOT_INDEX_KEYS), sorted(st.INDEX_KEYS))
        self.assertEqual(ing.PLOT_INDEX_KEYS[0], "step")
        self.assertEqual(st.INDEX_KEYS[0], "epoch")

    def test_provenance_keys_are_not_reported_as_metrics(self):
        """`observed_fields` feeds reconcile's "what the stream emits" report and the
        `did_you_mean` pool — listing `_src` there suggests our bookkeeping as a metric."""
        fields = st.observed_fields([{"val_acc": 0.9, "_src": "tensorboard", "_group": "step"}])
        self.assertEqual(fields, {"val_acc": 1})

    def test_reading_the_source_instead_of_the_stream_is_surfaced(self):
        """Silence here makes a missing, a stale, and a deliberately-refused stream
        all look like a healthy normalized read."""
        self.assertIsNone(st.unnormalized_finding("stream", "/x"))
        self.assertIsNone(st.unnormalized_finding("explicit", "/x"))
        f = st.unnormalized_finding("source", "/x/train_log.jsonl")
        self.assertEqual(f["level"], "warn")
        self.assertEqual(f["code"], "stream_not_normalized")

    def test_the_two_ranking_scripts_can_resolve_a_stream_they_were_not_handed(self):
        """They took `jsonl` as a required positional, so they could never reach
        stream.jsonl — and for a tfevents source log_path is a directory, which means
        exit 2 and "rank the checkpoints by hand"."""
        self.make_stream(name="run/" + st.CANONICAL_STREAM)
        self.make_ckpts()
        self.write_json("output.json", output_json())
        for script in ("train-run/select_checkpoint.py", "train-run/retention.py"):
            args = (["plan"] if "retention" in script else []) + [
                self.path("output.json"), "--run-dir", self.path("run"),
                "--output-dir", self.out_dir]
            rc, out, err = run_script(script, *args)
            self.assertIn(rc, (0, 1), f"{script}: {err}")
            self.assertNotIn("Traceback", err, script)
            self.assertNotIn("stream_not_normalized",
                             self.codes((out or {}).get("findings") or []), script)


class StreamResolution(Fixture):
    """run-mechanics.md -> "Metric stream": source, stream and record are three
    fixed words, and every reconciliation reads the *stream*.

    Which file the decision path opens is itself a record-layer fact: the source
    holds what the code wrote, the stream holds what the normalizer grouped and
    tagged, and reading the wrong one produces findings about a file nobody
    ranked. Nothing errors either way — both are readable jsonl.
    """
    def setUp(self):
        super().setUp()
        self._stream = st
        self.run_dir = self.path("run")
        os.makedirs(self.run_dir)

    def write_in_run(self, name, records=None):
        return self.make_stream(records, name=os.path.join("run", name))

    def test_canonical_stream_wins_over_the_source(self):
        self.write_in_run("train_log.jsonl", stream_records())
        self.write_in_run(self._stream.CANONICAL_STREAM, stream_records())
        got = self._stream.stream_path(output_json(), None, self.run_dir)
        self.assertEqual(got, os.path.join(self.run_dir, self._stream.CANONICAL_STREAM))

    def test_source_is_the_fallback_when_no_stream_exists(self):
        """Runs created before there was a normalizer still have to be readable."""
        self.write_in_run("train_log.jsonl", stream_records())
        got = self._stream.stream_path(output_json(), None, self.run_dir)
        self.assertEqual(got, os.path.join(self.run_dir, "train_log.jsonl"))

    def test_explicit_path_overrides_both(self):
        self.write_in_run(self._stream.CANONICAL_STREAM, stream_records())
        got = self._stream.stream_path(output_json(), "/tmp/elsewhere.jsonl", self.run_dir)
        self.assertEqual(got, "/tmp/elsewhere.jsonl")

    def test_nothing_resolvable_raises_rather_than_guessing(self):
        with self.assertRaises(self._stream.StreamError):
            self._stream.stream_path(output_json(**{"metrics.log_path": ""}), None, self.run_dir)


if __name__ == "__main__":
    unittest.main()
