"""A reproduction verdict is the one record nobody can ever re-check.

Every other record in MLClaw can be re-derived by going and looking: a census
can be retaken, a metric re-extracted, a snapshot re-resolved. A repro verdict
cannot. It says "as of August, this run's number came back and these axes had
drifted" — and by the time anyone reads it, the env has moved again, the commit
may be gone, and the trials that produced the band cost GPU hours nobody will
spend twice. That is exactly the bar in CLAUDE.md "Contracts": a record written
now and read later by someone who can no longer verify it.

So the checks below are not about whether `repro.py` computes tidily. They are
about the four ways a verdict can be wrong in a way that reads as right:

  * the worst cell reported as a mild one -- the actual bug this file was
    written after finding, where ranking axis verdicts by their enum position
    put `unverifiable` above `gone` and reported a run whose training data had
    been DELETED as merely unverifiable;
  * a reproduction claimed while the evidence for it was never gathered (no
    band, or a declared probe nobody ran) -- the second bug found, where the
    probe guard hung on `reproduced` alone and `reproduced_with_drift` closed
    silently without it;
  * a probe that could not run collapsing into a pass, which is CLAUDE.md
    "Never record a metric you did not read" one domain over;
  * a trial compared against a target it is not comparable to, which produces a
    wrong conclusion from correctly-recorded numbers.
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone

from helpers import TempDirCase, load_script, run_script

SCRIPT = "repro/repro.py"
# Most checks here drive the CLI. The code axis is probed in-process because the
# thing under test is one function's verdict on a hand-built record, and routing
# it through `check` would need a whole project fixture to assert one field.
repro = load_script(SCRIPT)
# The integrity check is a shared module both /repro and /discover read;
# `repro.framework_integrity` is the name the axis calls, so that is the
# one the fakes below replace.
framework_integrity_mod = sys.modules[repro.framework_integrity.__module__]


class ReproCase(TempDirCase):
    """Builds the run tree by hand rather than by driving the run skills: these
    checks need states a real run cannot be talked into producing (a snapshot
    stamped retired, a commit that never existed, a null mode next to a real
    metric), and building them by hand is the only way to set them exactly."""

    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        self.write("proj/project.json", json.dumps({"project_name": "t"}))
        self.write("proj/stages/training/config.json", json.dumps(
            {"param_injection": {"items": {
                "lr": {"via": "cli", "overridable": True, "evidence": "t.py:1"}}}}))
        self.sha = self.make_code_repo()
        self.snapshot()
        self.target()

    def make_code_repo(self):
        """A real git tree at stages/training/code, because the code axis probe
        is about real git: without one it can only ever answer `unverifiable`
        (code dir absent), which would let the `gone` and `drifted` cases pass
        for the wrong reason."""
        cdir = os.path.join(self.project, "stages", "training", "code")
        os.makedirs(cdir, exist_ok=True)
        self.write("proj/stages/training/code/train.py", "print('v1')\n")
        for cmd in (("init", "-q", "."),
                    ("config", "user.email", "test@mlclaw.local"),
                    ("config", "user.name", "MLClaw Test"),
                    ("config", "commit.gpgsign", "false"),
                    ("add", "-A"), ("commit", "-qm", "init")):
            subprocess.run(["git", *cmd], cwd=cdir, capture_output=True, text=True, encoding="utf-8")
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cdir,
                           capture_output=True, text=True, encoding="utf-8")
        return p.stdout.strip()

    def snapshot(self, retired=False, retired_at="2026-07-29T09:00:00+00:00"):
        rec = {"snapshot_id": "0728", "cite_as": "datasets/coco@0728",
               "count": 100, "frozen_at": "2026-07-28T10:00:00+00:00",
               "census_id": "census_20260728_100000", "unverified_units": []}
        if retired:
            rec["data_retired"] = [{"units": ["u0", "u1"], "at": "rig",
                                    "retire_id": "retire_20260729_090000",
                                    "retired_at": retired_at}]
        self.write("proj/datasets/coco/snapshots/0728/snapshot.json", json.dumps(rec))

    def census(self, cid, scanned_at, units, complete=True):
        """A census record as `census.py scan` writes it — `units[uid].at` is the
        list of locations holding that unit, and `complete` is false when a
        machine did not answer. Both are load-bearing for the retirement join."""
        self.write(f"proj/datasets/coco/census/{cid}.json", json.dumps(
            {"census_id": cid, "scanned_at": scanned_at, "complete": complete,
             "units": {u: {"at": at, "layers": {}} for u, at in units.items()}}))

    def retired_and_censused(self, units, *, complete=True,
                             scanned_at="2026-07-30T09:00:00+00:00"):
        """The stamp plus a census taken after it — the only combination that can
        say anything about whether the bytes survived."""
        self.snapshot(retired=True)
        self.census("census_20260730_090000", scanned_at, units, complete=complete)

    def target(self, *, mode="production", value=48.5, status="completed",
               commit="__real__", reproducible=True, config_snapshot=True):
        if commit == "__real__":
            commit = self.sha
        run = {
            "run_id": "run_A", "stage": "training", "status": status,
            "mode": mode, "scope": {"samples": 100},
            "code": {"origin_commit": commit, "dirty_patch_path": None,
                     "dirty_files_count": 0, "reproducible": reproducible},
            "env": {"cuda": "12.1", "packages": {"torch": "2.1.0"}},
            "lineage": {"parents": ["datasets/coco@0728"], "fork_of": None},
            "metrics": {"best": {"primary_metric": "val_mAP",
                                 "primary_metric_value": value, "epoch": 1}},
            "outputs": {},
        }
        self.write("proj/stages/training/runs/run_A/run.json", json.dumps(run))
        snap = os.path.join(self.project, "stages", "training", "runs",
                            "run_A", "config_snapshot.json")
        if config_snapshot:
            self.write("proj/stages/training/runs/run_A/config_snapshot.json",
                       json.dumps({"runtime_params": {"lr": 1e-4}}))
        elif os.path.exists(snap):
            # setUp already wrote one; not writing it again is not removing it
            os.remove(snap)

    def trial_run(self, rid, value, *, mode="production", scope=None,
                  metric="val_mAP"):
        self.write(f"proj/stages/evaluation/runs/{rid}/run.json", json.dumps(
            {"run_id": rid, "stage": "evaluation", "status": "completed",
             "mode": mode, "scope": {"samples": 100} if scope is None else scope,
             "metrics": {"best": {"primary_metric": metric,
                                  "primary_metric_value": value}}}))
        return f"evaluation/{rid}"

    def repro(self, *args):
        return run_script(SCRIPT, *args)

    def check(self, *extra):
        code, out, err = self.repro("check", "--project", self.project,
                                    "--run", "training/run_A", "--json",
                                    "--no-env", "--no-write", *extra)
        self.assertEqual(code, 0, f"check should never refuse: {out} {err}")
        return out

    def eval_target(self, *, config_snapshot=True, value=48.5):
        """An EVALUATION-stage target, for the checks about `reproduced*`.

        Since a training run measured through eval is a re-measurement and not a
        reproduction, `reproduced` is only reachable when the target's own
        procedure WAS re-run — which for an eval target is what measuring it is.
        The checks on `reproduced`'s evidence bars therefore need this fixture;
        pointing them at a training run would test the new family gate instead of
        the bar each one is named for.
        """
        self.write("proj/stages/evaluation/config.json", json.dumps(
            {"param_injection": {"items": {
                "lr": {"via": "cli", "overridable": True, "evidence": "t.py:1"}}}}))
        run = {
            "run_id": "run_E", "stage": "evaluation", "status": "completed",
            "mode": "production", "scope": {"samples": 100},
            "code": {"origin_commit": self.sha, "dirty_patch_path": None,
                     "dirty_files_count": 0, "reproducible": True},
            "env": {"cuda": "12.1", "packages": {"torch": "2.1.0"}},
            "lineage": {"parents": ["datasets/coco@0728"], "fork_of": None},
            "metrics": {"best": {"primary_metric": "val_mAP",
                                 "primary_metric_value": value, "epoch": 1}},
            "outputs": {},
        }
        self.write("proj/stages/evaluation/runs/run_E/run.json", json.dumps(run))
        if config_snapshot:
            self.write("proj/stages/evaluation/runs/run_E/config_snapshot.json",
                       json.dumps({"runtime_params": {"lr": 1e-4}}))
        return "evaluation/run_E"

    def open_session(self, *extra, run="training/run_A"):
        """Opens the way a real caller now has to.

        `open` refuses a training target measured through eval unless somebody
        types `--remeasure-only`, because that combination reproduces nothing and
        the default path of a skill called /repro must not quietly answer a
        weaker question. Supplying it here keeps every check below testing the
        thing it is named for rather than re-testing that one gate — which
        `ReMeasuringAnArtifactIsNotReproducingAProcedure` owns.
        """
        extra = list(extra)
        needs = (run.split("/")[0] not in ("evaluation", "inference")
                 and "retrain" not in extra and "--remeasure-only" not in extra)
        if needs:
            extra.append("--remeasure-only")
        code, out, err = self.repro("open", "--project", self.project,
                                    "--run", run, *extra)
        return code, out, err

    def session_with_band(self, values=(48.42, 48.55, 48.48), *open_args,
                          run="training/run_A"):
        code, out, err = self.open_session(*open_args, run=run)
        self.assertEqual(code, 0, f"open failed: {out} {err}")
        sid = out["session_id"]
        for i, v in enumerate(values):
            ref = self.trial_run(f"run_B{i}", v)
            c, o, e = self.repro("trial", "--project", self.project,
                                 "--session", sid, "--run", ref)
            self.assertEqual(c, 0, f"trial failed: {o} {e}")
        c, o, e = self.repro("band", "--project", self.project, "--session", sid)
        self.assertEqual(c, 0, f"band failed: {o} {e}")
        return sid, o


class WorstAxisWins(ReproCase):
    """run-mechanics.md -> "Record integrity": a record read later must not
    understate what it found. The overall verdict collapses five axes into one
    word, and the collapse ranked them by their position in the verdict tuple —
    which put `unverifiable` (index 3) above `gone` (index 2) and reported a run
    whose cited data had been deleted as `reproducible_unverifiably`. The single
    worst state in the table, announced as the second mildest, with a `you can
    still` block that never fired.
    """

    def test_gone_data_outranks_an_unverifiable_axis(self):
        self.retired_and_censused({"u0": [], "u1": []})   # data: gone
        self.target(config_snapshot=False)                # params: unverifiable
        r = self.check()
        self.assertEqual(r["axes"]["data"]["verdict"], "gone")
        self.assertEqual(r["axes"]["params"]["verdict"], "unverifiable")
        self.assertEqual(r["overall"], "not_reproducible",
                         "a deleted dataset must not be reported as merely "
                         "unverifiable")

    def test_the_unverifiable_axes_are_still_named(self):
        """Ranking `drifted` above `unverifiable` is only safe because the
        unverifiable set is reported next to the verdict instead of folded into
        it. Drop that and the ranking silently hides an unchecked axis."""
        self.target(config_snapshot=False)
        r = self.check()
        self.assertIn("params", r["unverifiable_axes"])

    def test_retired_snapshot_is_the_retire_receipt(self):
        """CLAUDE.md "Never delete data a frozen snapshot still names": the
        `data_retired` stamp exists so a citation can resolve *and say the data
        is gone*. This is the thing that reads it — if nothing does, the stamp
        was decoration."""
        self.retired_and_censused({"u0": [], "u1": []})
        r = self.check()
        self.assertEqual(r["axes"]["data"]["verdict"], "gone")
        cite = r["axes"]["data"]["citations"][0]
        self.assertEqual(cite["retired_units"], 2)
        self.assertEqual(cite["retired_from"], ["rig"])

    def test_open_refuses_when_an_axis_is_gone(self):
        self.retired_and_censused({"u0": [], "u1": []})
        code, out, _ = self.open_session()
        self.assertEqual(code, 1, "nothing can be relaunched; this is a refusal")
        self.assertIn("refused", out)
        self.assertTrue(out.get("you_can_still") is not None,
                        "a dead training reproduction usually leaves a live eval "
                        "one; refusing without saying so loses the answer")


class ARetirementStampIsNotAVerdict(ReproCase):
    """CLAUDE.md "Never report data you could not look at": a machine that did
    not answer, a path that is not there, and a directory that is genuinely empty
    are three facts, and only the last means the data is gone.

    `/data-retire` frees space **at one location** — `plan --at <loc>`, and
    `below_min_copies` is excluded by default, so the ordinary stamp comes from
    waiving `cited_by_snapshot` alone on a deletion that provably left copies
    behind. Reading every stamp as `gone` made that run `not_reproducible`, which
    per CLAUDE.md means no relaunch gets past it: the user stops while the data
    sits on the authority untouched. Only a census taken after the deletion can
    tell the two apart, and until one is, `unverifiable` is the honest answer.
    """

    def test_a_deletion_that_left_copies_is_not_gone(self):
        self.retired_and_censused({"u0": ["auth", "backup"], "u1": ["auth"]})
        r = self.check()
        d = r["axes"]["data"]
        self.assertEqual(d["verdict"], "drifted",
                         "the units still resolve at the authority; calling this "
                         "gone stops a reproduction that would have worked")
        self.assertEqual(r["overall"], "reproducible_with_drift")
        self.assertEqual(d["citations"][0]["survivors"], 2)

    def test_no_census_since_the_deletion_is_unverifiable(self):
        """A census from before the retirement describes a disk that has since
        been written to. Reading it as evidence either way is the stale-record
        bug; the honest answer names the scan that would settle it."""
        self.snapshot(retired=True)
        self.census("census_20260728_100000", "2026-07-28T10:00:00+00:00",
                    {"u0": ["rig"], "u1": ["rig"]})
        r = self.check()
        d = r["axes"]["data"]
        self.assertEqual(d["verdict"], "unverifiable")
        self.assertIn("census.py scan", d["citations"][0]["detail"])
        self.assertEqual(r["overall"], "reproducible_unverifiably")

    def test_a_partial_census_cannot_declare_it_gone(self):
        """Every count under an incomplete census is a lower bound, so a unit
        missing from the listing may be on the machine that did not answer."""
        self.retired_and_censused({"u0": [], "u1": []}, complete=False)
        r = self.check()
        d = r["axes"]["data"]
        self.assertEqual(d["verdict"], "unverifiable")
        self.assertIn("did not answer", d["citations"][0]["detail"])

    def test_an_undated_stamp_cannot_be_ordered_against_a_census(self):
        """No `retired_at` means no census can be shown to postdate the delete,
        so the newest one on disk must not be read as if it did."""
        self.snapshot(retired=True, retired_at=None)
        self.census("census_20260730_090000", "2026-07-30T09:00:00+00:00",
                    {"u0": [], "u1": []})
        r = self.check()
        self.assertEqual(r["axes"]["data"]["verdict"], "unverifiable")

    def test_the_stamp_is_still_read_at_all(self):
        """The original point of the check this replaces: if nothing joins the
        stamp back to the runs that cited it, the stamp was decoration."""
        self.retired_and_censused({"u0": ["auth"], "u1": ["auth"]})
        r = self.check()
        c = r["axes"]["data"]["citations"][0]
        self.assertEqual(c["retired_units"], 2)
        self.assertEqual(c["retired_from"], ["rig"])
        self.assertNotEqual(r["axes"]["data"]["verdict"], "intact",
                            "a deletion the snapshot recorded must change the "
                            "axis; silently passing is the stamp going unread")


class UnreadableIsNotIntact(ReproCase):
    """CLAUDE.md "Never record a metric you did not read", applied to axis
    probes. Extraction failure and "nothing was wrong" must not both come out as
    a pass — for the code axis in particular, `reproducible: false` means the
    rebuilt tree is NOT the tree that ran, so a matching number afterwards is
    evidence and not proof.
    """

    def test_missing_commit_is_unverifiable(self):
        self.target(commit=None)
        self.assertEqual(self.check()["axes"]["code"]["verdict"], "unverifiable")

    def test_unresolvable_commit_is_gone_not_drifted(self):
        self.target(commit="0" * 40)
        self.assertEqual(self.check()["axes"]["code"]["verdict"], "gone")

    def test_reproducible_false_is_unverifiable_even_with_a_live_commit(self):
        self.target(reproducible=False)
        ax = self.check()["axes"]["code"]
        self.assertEqual(ax["verdict"], "unverifiable",
                         "a live commit does not redeem a tree that was never fully captured")
        self.assertIn("reproducible", ax["detail"])

    def test_uncited_data_is_unverifiable_not_intact(self):
        """A run that cited no snapshot read a path, and a directory that still
        exists says nothing about whether its contents are the ones that
        trained. This is the axis where MLClaw can beat a path string, and only
        if it declines to guess."""
        self.write("proj/stages/training/runs/run_A/run.json", json.dumps(
            {"run_id": "run_A", "stage": "training", "status": "completed",
             "mode": "production", "scope": {},
             "lineage": {"parents": []},
             "metrics": {"best": {"primary_metric": "m", "primary_metric_value": 1}}}))
        self.assertEqual(self.check()["axes"]["data"]["verdict"], "unverifiable")


class ABandIsMeasuredNotGuessed(ReproCase):
    """run-mechanics.md -> "Record integrity". A verdict on a delta is only as
    good as the noise estimate behind it, and `/refactor-run`'s +/-0.5% default is
    a guess about a pipeline nobody measured. Two failure directions: a band
    from too few points is a range wearing a band's name, and closing with no
    band at all reinstates the guess this loop exists to replace.
    """

    def test_band_refuses_under_three_unpinned_trials(self):
        code, out, _ = self.open_session()
        sid = out["session_id"]
        for i, v in enumerate((48.4, 48.6)):
            self.repro("trial", "--project", self.project, "--session", sid,
                       "--run", self.trial_run(f"run_B{i}", v))
        code, out, _ = self.repro("band", "--project", self.project, "--session", sid)
        self.assertEqual(code, 1)
        self.assertIn("refused", out)

    def test_close_refuses_without_a_band(self):
        code, out, _ = self.open_session()
        sid = out["session_id"]
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid, "--verdict", "reproduced")
        self.assertEqual(code, 1)
        self.assertIn("band", json.dumps(out))

    def test_target_inside_the_measured_interval_reproduces(self):
        _, band = self.session_with_band()
        self.assertTrue(band["band"]["target_inside"])
        self.assertEqual(band["metric_verdict"], "reproduced")

    def test_far_outside_diverges_and_near_outside_is_inconclusive(self):
        _, near = self.session_with_band((48.55, 48.61, 48.58))
        self.assertEqual(near["metric_verdict"], "inconclusive",
                         "a hair outside a narrow interval is not an answer; "
                         "more repeats is")
        _, far = self.session_with_band((45.1, 45.3, 45.2))
        self.assertEqual(far["metric_verdict"], "diverged")

    def test_declared_tolerance_is_compared_to_reality_not_used_as_the_rule(self):
        """The default would have accepted roughly twice this pipeline's real
        scatter. Saying so is the most useful line the session produces, and it
        has to survive into `caveats` where the verdict is read."""
        sid, band = self.session_with_band()
        self.assertIsNotNone(band["declared_tolerance_note"])
        self.assertIn("WIDER", band["declared_tolerance_note"])
        s = json.load(open(os.path.join(self.project, "repro", sid, "session.json")))
        self.assertTrue(any("WIDER" in c for c in s["caveats"]))


class ReproducedIsTheStrongestClaim(ReproCase):
    """CLAUDE.md "Never let somebody's word become a checked fact", applied to a
    verdict about a machine. `reproduced` is read as "same number, same
    conditions", so it must refuse every state where one of those is unknown.
    Downgrading to `reproduced_with_drift` is not a lesser answer — it is the
    accurate one, and it keeps saying so every time it is quoted.
    """

    def test_reproduced_refuses_while_an_axis_is_unverifiable(self):
        self.eval_target(config_snapshot=False)
        sid, _ = self.session_with_band(run="evaluation/run_E")
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid, "--verdict", "reproduced")
        self.assertEqual(code, 1)
        self.assertIn("reproduced_with_drift", json.dumps(out))

    def test_with_drift_is_allowed_and_stamps_the_caveat(self):
        self.eval_target(config_snapshot=False)
        sid, _ = self.session_with_band(run="evaluation/run_E")
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid,
                                  "--verdict", "reproduced_with_drift")
        self.assertEqual(code, 0)
        self.assertTrue(any("params" in c for c in out["caveats"]),
                        "the qualification must travel with the verdict, not sit "
                        "in a probe report nobody opens")

    def test_close_refuses_when_the_metric_verdict_is_not_reproduced(self):
        sid, band = self.session_with_band((45.1, 45.3, 45.2))
        self.assertEqual(band["metric_verdict"], "diverged")
        for verdict in ("reproduced", "reproduced_with_drift"):
            code, out, _ = self.repro("close", "--project", self.project,
                                      "--session", sid, "--verdict", verdict)
            self.assertEqual(code, 1, f"{verdict} must not close over a diverged band")

    def test_diverged_refuses_without_an_attributed_axis(self):
        sid, _ = self.session_with_band((45.1, 45.3, 45.2))
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid, "--verdict", "diverged")
        self.assertEqual(code, 1)
        self.assertIn("diverged_unattributed", json.dumps(out))

    def test_a_closed_session_refuses_more_trials(self):
        """The trials are the evidence. Adding to them after the verdict changes
        what the verdict was based on, retroactively."""
        self.eval_target()
        sid, _ = self.session_with_band(run="evaluation/run_E")
        self.repro("close", "--project", self.project, "--session", sid,
                   "--verdict", "reproduced_with_drift")
        code, out, _ = self.repro("trial", "--project", self.project,
                                  "--session", sid,
                                  "--run", self.trial_run("run_Z", 48.5))
        self.assertEqual(code, 1)


class TheProbeIsTheStrongerCheck(ReproCase):
    """run-mechanics.md -> "Record integrity": the chosen artifact and the
    recorded metric must describe the same thing. Two runs can share an
    aggregate metric and not share a prediction — the small objects all get lost
    and the average lands in the same place — so a declared probe that nobody
    ran means the WEAKER of the two checks passed alone.

    Both reproduction verdicts carry this bar. Hanging it on `reproduced` only
    let `reproduced_with_drift` close silently with an unrun probe, which is the
    same defect one verdict over.
    """

    PROBE = ("--probe", "datasets/coco@0728")

    def test_both_reproduction_verdicts_refuse_an_unrun_probe(self):
        for verdict in ("reproduced", "reproduced_with_drift"):
            with self.subTest(verdict=verdict):
                self.eval_target()
                sid, _ = self.session_with_band((48.42, 48.55, 48.48), *self.PROBE,
                                                run="evaluation/run_E")
                code, out, _ = self.repro("close", "--project", self.project,
                                          "--session", sid, "--verdict", verdict)
                self.assertEqual(code, 1, "a declared probe that never ran cannot "
                                          "close as a reproduction")
                self.assertIn("probe", json.dumps(out))

    def test_null_predictions_agree_does_not_read_as_true(self):
        sid, _ = self.session_with_band((48.42, 48.55, 48.48), *self.PROBE)
        s = json.load(open(os.path.join(self.project, "repro", sid, "session.json")))
        self.assertIsNone(s["predictions_agree"],
                          "no probe run is a third state, not agreement")

    def test_disagreeing_predictions_force_their_own_verdict(self):
        self.eval_target()
        sid, _ = self.session_with_band((48.42, 48.55, 48.48), *self.PROBE,
                                       run="evaluation/run_E")
        self.repro("trial", "--project", self.project, "--session", sid,
                   "--run", self.trial_run("run_P", 48.5), "--predictions-differ")
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid,
                                  "--verdict", "reproduced_with_drift")
        self.assertEqual(code, 1)
        self.assertIn("metric_ok_predictions_diverged", json.dumps(out))
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid,
                                  "--verdict", "metric_ok_predictions_diverged")
        self.assertEqual(code, 0)

    def test_no_probe_declared_means_no_probe_requirement(self):
        """The bar is "the probe you promised", not "a probe". Requiring one
        unconditionally would make the honest no-probe session unclosable and
        teach people to declare a probe they intend to skip."""
        self.eval_target()
        sid, _ = self.session_with_band(run="evaluation/run_E")
        self.eval_target(config_snapshot=False)
        code, _, _ = self.repro("close", "--project", self.project,
                                "--session", sid,
                                "--verdict", "reproduced_with_drift")
        self.assertEqual(code, 0)


class TrialsMustBeComparable(ReproCase):
    """run-mechanics.md -> "Metric comparability": compare only across runs with
    the same `mode` and an equivalent `scope`. A debug trial judged against a
    production target is the failure mode named there in full — nothing errors,
    no data is missing, and a wrong conclusion is drawn from correctly-recorded
    numbers. A band assembled from mismatched trials would be that failure with
    a measurement's authority on top.
    """

    def test_mode_mismatch_is_refused(self):
        _, out, _ = self.open_session()
        sid = out["session_id"]
        code, out, _ = self.repro("trial", "--project", self.project,
                                  "--session", sid,
                                  "--run", self.trial_run("run_D", 48.5, mode="debug"))
        self.assertEqual(code, 1)

    def test_scope_mismatch_is_refused(self):
        _, out, _ = self.open_session()
        sid = out["session_id"]
        code, out, _ = self.repro("trial", "--project", self.project,
                                  "--session", sid,
                                  "--run", self.trial_run("run_S", 48.5,
                                                          scope={"samples": 20}))
        self.assertEqual(code, 1)

    def test_a_different_metric_is_refused_unless_the_value_is_named(self):
        _, out, _ = self.open_session()
        sid = out["session_id"]
        ref = self.trial_run("run_M", 48.5, metric="val_acc")
        code, _, _ = self.repro("trial", "--project", self.project,
                                "--session", sid, "--run", ref)
        self.assertEqual(code, 1, "two metrics that share nothing but a slot are "
                                  "not the same quantity")
        code, _, _ = self.repro("trial", "--project", self.project,
                                "--session", sid, "--run", ref, "--value", "48.5")
        self.assertEqual(code, 0, "an explicitly read value is a stated fact and "
                                  "is allowed")

    def test_open_refuses_a_target_with_no_mode(self):
        self.target(mode=None)
        code, out, _ = self.open_session()
        self.assertEqual(code, 1)
        self.assertIn("mode", json.dumps(out))

    def test_open_refuses_a_target_with_no_number_or_no_completion(self):
        self.target(value=None)
        self.assertEqual(self.open_session()[0], 1)
        self.target(status="running")
        self.assertEqual(self.open_session()[0], 1)


class AttributionSpendsRunsOnlyOnSuspects(ReproCase):
    """CLAUDE.md "Skills & Dependencies": a loop that suggests work must suggest
    work that can pay off. Pinning an axis the probe called `intact` cannot move
    the number and costs a full run, so `attribute` must never propose one — and
    when nothing drifted at all it has to say that the divergence lies outside
    everything MLClaw records, rather than inventing an axis to blame.
    """

    def test_intact_axes_are_never_suggested(self):
        sid, _ = self.session_with_band((45.1, 45.3, 45.2))
        code, out, _ = self.repro("attribute", "--project", self.project,
                                  "--session", sid)
        self.assertEqual(code, 0, "attribute reports, it never refuses")
        for ax in out["unpinned_suspects_by_cost"]:
            self.assertNotEqual(self.check()["axes"][ax]["verdict"], "intact")

    def test_no_suspects_says_so_instead_of_blaming_an_axis(self):
        sid, _ = self.session_with_band((45.1, 45.3, 45.2))
        out = self.repro("attribute", "--project", self.project, "--session", sid)[1]
        if not out["suspect_axes"]:
            self.assertIsNone(out["attributed_to"])
            self.assertIn("note", out)

    def test_a_pin_that_restores_the_number_implicates_its_axis(self):
        self.target(config_snapshot=False)             # params: unverifiable
        sid, _ = self.session_with_band((45.1, 45.3, 45.2))
        self.repro("trial", "--project", self.project, "--session", sid,
                   "--run", self.trial_run("run_Pin", 48.5), "--pinned", "params")
        out = self.repro("attribute", "--project", self.project, "--session", sid)[1]
        self.assertIn("params", out["implicated"])
        self.assertEqual(out["attributed_to"], "params")
        code, _, _ = self.repro("close", "--project", self.project, "--session", sid,
                                "--verdict", "diverged", "--attributed-to", "params")
        self.assertEqual(code, 0)


class TimestampsAreUTCWithAnOffset(ReproCase):
    """run-mechanics.md -> "Record integrity": run timestamps are UTC with an
    explicit offset. Naive local strings from machines in different zones sort
    wrongly and look fine — and a repro session's trials are frequently launched
    from a different machine than the one that wrote the run being reproduced.
    """

    def test_every_written_timestamp_carries_an_offset(self):
        sid, _ = self.session_with_band()
        s = json.load(open(os.path.join(self.project, "repro", sid, "session.json")))
        stamps = [s["opened_at"]] + [t["at"] for t in s["trials"]]
        stamps.append(s["band"]["measured_at"])
        for value in stamps:
            with self.subTest(value=value):
                parsed = datetime.fromisoformat(value)
                self.assertIsNotNone(parsed.tzinfo, f"naive timestamp: {value}")
                self.assertEqual(parsed.tzinfo.utcoffset(parsed),
                                 timezone.utc.utcoffset(None))

    def test_check_writes_a_dated_observation_it_does_not_overwrite(self):
        """An axis audit is an observation of the world, like a census: retaking
        it next month gives a different answer, and both are true of their
        date. Overwriting would destroy the only evidence of when an axis was
        last intact."""
        for _ in range(2):
            code, _, _ = self.repro("check", "--project", self.project,
                                    "--run", "training/run_A", "--no-env", "--json")
            self.assertEqual(code, 0)
        d = os.path.join(self.project, "stages", "training", "runs", "run_A", "repro")
        self.assertGreaterEqual(len(os.listdir(d)), 1)
        for name in os.listdir(d):
            self.assertTrue(name.startswith("check_") and name.endswith(".json"))


if __name__ == "__main__":
    unittest.main()


class AFrameworkStagesCodeAxisIsNotAMissingTree(ReproCase):
    """`layout.md` -> "Code Source Resolution", the `framework` mode; and
    `.claude/skills/repro/references/axes.md` -> "code".

    `probe_code` read a null `origin_commit` as "the tree that ran was never
    identified". For a stage whose `code_source` is `framework` there was never a
    tree — the code is an installed package and the null is BY CONSTRUCTION. That
    is exactly the confusion `code.kind` was added to prevent, and the axis was
    the one place still making it: a perfectly pinned framework run reported
    `unverifiable` with advice about a git tree that does not exist.

    Its contract is `install <pkg>==<version>`, so what pins it is the version —
    which makes this axis's answer a version comparison, and `intact` a real
    verdict rather than a courtesy.
    """

    def framework_run(self, pinned="8.4.40", ran="8.4.40"):
        run = {"run_id": "run_A", "stage": "evaluation", "status": "completed",
               "mode": "production", "scope": {"samples": 100},
               "env": {"packages": {}},
               "metrics": {"best": {"primary_metric": "m", "primary_metric_value": 0.5}}}
        run["code"] = {"kind": "framework", "framework": "ultralytics",
                       "framework_version": pinned, "repo": None, "branch": None,
                       "origin_commit": None, "repo_subdir": None,
                       "dirty_patch_path": None, "dirty_files_count": None,
                       "untracked_skipped": [], "reproducible": True, "warnings": []}
        if ran:
            run.setdefault("env", {}).setdefault("packages", {})["ultralytics"] = ran
        return run

    def test_a_pinned_framework_never_reports_a_missing_sha(self):
        """The original defect, and it must stay fixed whatever the verdict is.
        Whether the axis lands `intact` or `unverifiable` now depends on the edit
        check below — but neither may be reached by way of "no commit recorded",
        which describes a tree that never existed."""
        for interp in (None, sys.executable):
            ax = repro.probe_code(self.project, self.framework_run(), "evaluation",
                                  framework_python=interp)
            self.assertIn("no tree by design", ax["detail"])
            self.assertNotIn("origin_commit", ax["detail"],
                             "it must not report a missing SHA for a stage that "
                             "has none")

    def test_an_unchecked_edit_question_is_unverifiable_not_intact(self):
        """A version pin cannot see an edit to the installed package. That question
        is answerable — pip's RECORD holds a sha256 per file — so leaving it
        unasked is `unverifiable`, and the axis has to say which question is open.

        `intact` here would be the fourth verdict's whole reason for existing,
        thrown away: a question nobody asked reading as a question answered.
        """
        ax = repro.probe_code(self.project, self.framework_run(), "evaluation")
        self.assertEqual(ax["verdict"], "unverifiable")
        self.assertEqual(ax.get("integrity"), "not_checked")
        self.assertIn("--framework-python", json.dumps(ax),
                      "and it has to name what would close it")

    def test_a_package_absent_from_the_asked_interpreter_is_not_a_pass(self):
        """With an interpreter the check runs, and a package that is not there is
        `unverifiable` — the same bar one step further out. `not_installed` here
        must not read as `not_installed` anywhere, which is the unreachable/gone
        split the discovery engine turns on, wearing different words."""
        ax = repro.probe_code(self.project,
                              self.framework_run(pinned="7.2.2", ran="7.2.2"),
                              "evaluation", framework_python=sys.executable)
        # The fixture pins `ultralytics`, which is not installed in the suite's
        # stdlib-only interpreter: the honest verdict is `unverifiable`, and
        # emphatically not `intact`. That is the same bar one step further out —
        # a probe that could not run never collapses into a pass.
        self.assertEqual(ax["verdict"], "unverifiable")
        self.assertEqual(ax["integrity"]["state"], "not_installed")
        self.assertIn("NOT a statement about the environment that ran",
                      ax["integrity"]["means"],
                      "not-installed-here must not read as not-installed-anywhere, "
                      "and the meaning has to travel with the reading rather than "
                      "living in whichever caller happens to print it")

    def test_the_pin_disagreeing_with_what_ran_is_drift(self):
        """The version is the whole contract here, so a record pinning one and an
        env recording another is the framework branch's equivalent of a SHA that
        does not resolve — and it must not read as intact."""
        ax = repro.probe_code(self.project, self.framework_run(ran="8.5.0"), "evaluation")
        self.assertEqual(ax["verdict"], "drifted")
        self.assertIn("8.5.0", ax["detail"])

    def test_a_framework_record_with_no_version_is_unverifiable(self):
        run = self.framework_run()
        run["code"]["framework_version"] = None
        ax = repro.probe_code(self.project, run, "evaluation")
        self.assertEqual(ax["verdict"], "unverifiable")

    def test_it_still_says_what_a_version_cannot_see(self):
        """`reproducible: true` on the framework branch is honest about rebuilding
        the code and blind to a local edit to the installed package. The axis has
        to carry that — but it is no longer a limitation to be lived with, so what
        it carries now is the check that closes it rather than an apology."""
        ax = repro.probe_code(self.project, self.framework_run(), "evaluation")
        self.assertIn("EDITED", json.dumps(ax))
        self.assertIn("RECORD", json.dumps(ax),
                      "and it must name the evidence, not just the worry")

    def test_an_edited_package_is_drift_and_says_the_contract_will_not_hold(self):
        """The blind spot, no longer blind. An installed package modified after
        install means `install <pkg>==<version>` reproduces something else — the
        same fact a dirty patch records for a git tree, and nothing else records
        it here."""
        run = self.framework_run(pinned="9.9.9", ran="9.9.9")
        real = framework_integrity_mod.framework_integrity

        def fake(spec, python=None, budget_s=120.0):
            return {"state": "edited", "installed_version": "9.9.9",
                    "files_checked": 40, "files_mismatched": 2, "files_missing": 0,
                    "mismatched_sample": ["pkg/engine/trainer.py"],
                    "package": "ultralytics", "interpreter": python,
                    "pinned_version": "9.9.9", "version_matches_pin": True}

        repro.framework_integrity = fake
        try:
            ax = repro.probe_code(self.project, run, "evaluation",
                                  framework_python=sys.executable)
        finally:
            repro.framework_integrity = real
        self.assertEqual(ax["verdict"], "drifted")
        self.assertIn("will NOT reproduce what ran", ax["detail"])
        self.assertIn("dirty patch", ax["fix"],
                      "the fix has to say what is about to be lost, because "
                      "rebuilding that environment destroys the only copy")

    def test_an_edit_check_that_could_not_run_is_unverifiable(self):
        """A probe that could not run never collapses into a pass — the fourth
        verdict's rule, applied to the newest probe in the file."""
        real = framework_integrity_mod.framework_integrity

        def fake(spec, python=None, budget_s=120.0):
            return {"state": "unverifiable", "detail": "hashing exceeded 120s",
                    "package": "ultralytics", "installed_version": None}

        repro.framework_integrity = fake
        try:
            ax = repro.probe_code(self.project, self.framework_run(), "evaluation",
                                  framework_python=sys.executable)
        finally:
            repro.framework_integrity = real
        self.assertEqual(ax["verdict"], "unverifiable")

    def test_a_git_run_with_no_sha_is_still_unverifiable(self):
        """The new branch must not become a way past the old verdict."""
        run = self.framework_run()
        run["code"] = {"origin_commit": None}
        ax = repro.probe_code(self.project, run, "evaluation")
        self.assertEqual(ax["verdict"], "unverifiable")


class ReMeasuringAnArtifactIsNotReproducingAProcedure(ReproCase):
    """`.claude/skills/repro/references/verdicts.md` -> "Final verdicts", the
    `remeasured` pair; and `/discover` `references/searches.md` -> "Where the
    vocabulary breaks, and it is not cosmetic", which names this exact failure one
    domain over: "Two words, same spelling, opposite bars."

    `measure_via: eval` is the default "including for training runs", and the cost
    argument is sound — re-measuring a surviving checkpoint answers "is the
    recorded number real" for the price of one eval. What was not sound was
    calling the result `reproduced`. Re-measuring a training run's artifact re-runs
    nothing about the training: a hyperparameter recorded wrongly, a dataset
    recorded wrongly, or a recipe that would no longer produce this model are all
    invisible, because the artifact is a GIVEN and only its number was checked.

    It matters because `skill-graph.md` makes a closed `reproduced*` session the
    ONLY thing that moves an inherited checkpoint's `origin.confidence` off
    `claimed`. One word doing two jobs meant the weaker fact was buying the
    stronger promotion — on exactly the inherited-checkpoint case the field exists
    for.

    The split is keyed on the TARGET'S STAGE rather than on a flag, because that
    is the fact that decides it: an eval run re-measured is an eval run re-run.
    """

    def test_a_training_target_measured_by_eval_cannot_close_as_reproduced(self):
        sid, _ = self.session_with_band()
        for verdict in ("reproduced", "reproduced_with_drift"):
            with self.subTest(verdict=verdict):
                code, out, _ = self.repro("close", "--project", self.project,
                                          "--session", sid, "--verdict", verdict)
                self.assertEqual(code, 1)
                blob = json.dumps(out)
                self.assertIn("did not reproduce a procedure", blob)
                self.assertIn("remeasured", blob, "the refusal must name the "
                                                  "verdict that IS available")

    def test_the_refusal_offers_both_ways_out(self):
        """Two of them, because they answer different questions and the caller has
        to be told which one they are choosing: record the weaker fact, or pay for
        the stronger one."""
        sid, _ = self.session_with_band()
        _, out, _ = self.repro("close", "--project", self.project,
                               "--session", sid, "--verdict", "reproduced")
        self.assertIn("retrain", json.dumps(out))
        self.assertIn("i-accept-the-cost", json.dumps(out))

    def test_remeasured_with_drift_closes(self):
        sid, _ = self.session_with_band()
        code, out, err = self.repro("close", "--project", self.project,
                                    "--session", sid,
                                    "--verdict", "remeasured_with_drift")
        self.assertEqual(code, 0, f"{out} {err}")

    def test_the_drift_downgrade_applies_to_the_weaker_family_too(self):
        """`remeasured` over a drifted axis is exactly as much a weaker fact as
        `reproduced` is, so the downgrade cannot be attached to one family only —
        that was how `reproduced_with_drift` originally slipped past the probe
        check, one verdict over."""
        self.target(config_snapshot=False)
        sid, _ = self.session_with_band()
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid, "--verdict", "remeasured")
        self.assertEqual(code, 1)
        self.assertIn("remeasured_with_drift", json.dumps(out))

    def test_an_eval_target_still_reaches_the_stronger_word(self):
        """The gate must not swallow the case where eval IS the procedure. An eval
        run re-measured has been re-run, so `reproduced` is the accurate word and
        withholding it would lose a fact."""
        self.eval_target(config_snapshot=False)
        sid, _ = self.session_with_band(run="evaluation/run_E")
        code, out, err = self.repro("close", "--project", self.project,
                                    "--session", sid,
                                    "--verdict", "reproduced_with_drift")
        self.assertEqual(code, 0, f"{out} {err}")

    def test_the_weaker_word_is_refused_when_the_procedure_was_re_run(self):
        """Symmetry, and it is not pedantry: recording `remeasured` on a session
        that really did reproduce loses a fact nobody can recover from the record
        later."""
        self.eval_target()
        sid, _ = self.session_with_band(run="evaluation/run_E")
        code, out, _ = self.repro("close", "--project", self.project,
                                  "--session", sid, "--verdict", "remeasured")
        self.assertEqual(code, 1)
        self.assertIn("understates", json.dumps(out))


class TheDefaultPathCannotQuietlyAnswerAWeakerQuestion(ReproCase):
    """`.claude/skills/repro/SKILL.md` -> Step 2, `measure_via`; and
    `references/verdicts.md` -> "Re-measuring is not reproducing".

    Splitting the verdict words was half a fix. `measure_via` still DEFAULTED to
    `eval`, so the default path of a skill called `/repro` — the one a user reaches
    by typing "复现一下" — ran inference over a val set and stamped a verdict that
    sounds like the one they asked for. A strict standard that the default route
    walks around is not a standard.

    So the combination is gated the same way its expensive twin is. `retrain`
    costs money and needs `--i-accept-the-cost`; `eval` against a training target
    costs almost nothing and buys a weaker answer, so it needs `--remeasure-only`.
    Symmetric, and the same discipline as `/data-label`'s `--spec | --no-spec`:
    **the absence has to be something a person typed rather than something that
    just didn't happen.**

    The ceiling is also announced at `open` rather than discovered at `close`,
    which is SKILL.md Step 1's own rule about the drift ceiling applied to this
    one — a caller who finds out at close has already spent the trials.
    """

    def raw_open(self, *extra, run="training/run_A"):
        return self.repro("open", "--project", self.project, "--run", run, *extra)

    def test_a_training_target_via_eval_is_refused_without_the_acknowledgement(self):
        code, out, _ = self.raw_open()
        self.assertEqual(code, 1)
        blob = json.dumps(out)
        self.assertIn("will not reproduce anything", blob)
        self.assertIn("--remeasure-only", blob)
        self.assertIn("retrain", blob, "the refusal must offer the other way out too")

    def test_the_acknowledgement_lets_it_open(self):
        code, out, err = self.raw_open("--remeasure-only")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertFalse(out["reproduces_the_procedure"])

    def test_the_ceiling_is_stated_at_open_not_discovered_at_close(self):
        _, out, _ = self.raw_open("--remeasure-only")
        self.assertTrue(out["verdict_ceiling"].startswith("remeasured"),
                        f"ceiling was {out['verdict_ceiling']!r}")
        self.assertIn("not re-run", out["ceiling_why"])

    def test_an_eval_target_needs_no_acknowledgement(self):
        """The gate must not tax the case where eval IS the procedure."""
        self.eval_target()
        code, out, err = self.raw_open(run="evaluation/run_E")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertTrue(out["reproduces_the_procedure"])
        self.assertTrue(out["verdict_ceiling"].startswith("reproduced"))

    def test_retrain_is_still_gated_on_its_own_flag(self):
        """Both gates stand: acknowledging the weaker question must not become a
        way to skip acknowledging the cost, or vice versa."""
        code, out, _ = self.raw_open("--measure-via", "retrain")
        self.assertEqual(code, 1)
        self.assertIn("i-accept-the-cost", json.dumps(out))
        code, out, err = self.raw_open("--measure-via", "retrain",
                                       "--i-accept-the-cost")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertTrue(out["reproduces_the_procedure"],
                        "retraining a training target IS reproducing it")


class ABandsSourceDecidesWhichWayItCanAnswer(ReproCase):
    """CLAUDE.md -> "Never silently" (never let somebody's word become a checked
    fact), and run-mechanics.md -> "Record integrity".

    Two bands wear the same name and have different evidentiary reach. A band from
    repeated unpinned trials is run-to-run spread and answers both ways. A band from
    the target run's own converged tail is the SAME trajectory scored at nearby
    epochs -- same seed, same data order, same init -- so it cannot contain the
    variation that makes two fresh runs differ. It is a lower bound, and a lower
    bound confirms without refuting.

    Letting the second one say `diverged` is the vocabulary-collapse defect again:
    a weaker fact wearing a stronger word, with nothing anywhere raising.

    The reason the weak band exists at all is cost. Three eval trials are two
    minutes; three retrains are three times the original run. Forcing the same
    trial count on both makes the expensive route pay up front for an ambiguity
    that may never arise -- and which only the first trial can reveal.
    """

    def one_trial_session(self, trial_value, run="training/run_A"):
        code, out, err = self.open_session(run=run)
        self.assertEqual(code, 0, f"open failed: {out} {err}")
        sid = out["session_id"]
        ref = self.trial_run("run_B0", trial_value)
        c, o, e = self.repro("trial", "--project", self.project,
                             "--session", sid, "--run", ref)
        self.assertEqual(c, 0, f"trial failed: {o} {e}")
        return sid

    # ---- the default number of trials follows what one costs ---------------- #

    def test_retrain_plans_for_one_trial_and_eval_for_three(self):
        code, out, err = self.open_session("--remeasure-only")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["band_target_trials"], 3,
                         "an eval trial is cheap; there is no reason to skimp")
        code, out, err = self.open_session("--measure-via", "retrain",
                                           "--i-accept-the-cost")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["band_target_trials"], 1,
                         "a retrain trial costs the original run, so three of them "
                         "is a 3x bill for a case that may not occur")

    def test_a_one_trial_plan_says_where_its_band_comes_from(self):
        """Planning for one trial must not read as planning for no band. The
        session's own `next` has to name the free one, or the caller reaches
        `band`, gets refused, and concludes the cheap route was never viable."""
        code, out, _ = self.open_session("--measure-via", "retrain",
                                        "--i-accept-the-cost")
        self.assertIn("--from-history", out["next"])
        self.assertIn("inconclusive", out["next"],
                      "and it has to say which single outcome justifies buying "
                      "the extra trials")

    def test_zero_trials_is_refused(self):
        code, out, _ = self.open_session("--band-trials", "0", "--remeasure-only")
        self.assertEqual(code, 1)
        self.assertIn("re-run nothing", json.dumps(out))

    # ---- the asymmetry, which is the whole point --------------------------- #

    def test_a_tail_band_can_confirm(self):
        """Inside a lower bound is sound: delta <= lower_bound <= true noise."""
        sid = self.one_trial_session(48.50)
        code, out, err = self.repro("band", "--project", self.project,
                                    "--session", sid,
                                    "--from-history", json.dumps(
                                        [48.40, 48.45, 48.52, 48.61, 48.47, 48.55]),
                                    "--history-what", "epochs 95-100")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["band"]["source"], "run_history")
        self.assertTrue(out["band"]["lower_bound"])
        self.assertFalse(out["band"]["can_refute"])
        self.assertEqual(out["metric_verdict"], "reproduced")

    def test_a_tail_band_can_never_refute(self):
        """The one that matters. A trial far outside the target's within-run
        wobble is `inconclusive`, NOT `diverged`: run-to-run spread may be wider
        than this band can see, so the delta is not yet shown to be real."""
        sid = self.one_trial_session(12.0)
        code, out, err = self.repro("band", "--project", self.project,
                                    "--session", sid,
                                    "--from-history", json.dumps(
                                        [48.40, 48.45, 48.52, 48.61, 48.47]))
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["metric_verdict"], "inconclusive",
                         "a lower bound on noise cannot establish that a delta "
                         "exceeds noise, however large the delta looks")
        self.assertIn("LOWER BOUND", out["why"])
        self.assertIn("Only repeats can", out["why"])

    def test_the_refuting_case_is_the_only_one_that_buys_trials(self):
        sid = self.one_trial_session(12.0)
        _, out, _ = self.repro("band", "--project", self.project, "--session", sid,
                               "--from-history", json.dumps(
                                   [48.40, 48.45, 48.52, 48.61, 48.47]))
        self.assertIn("cannot refute", out["next"])
        self.assertIn("ONLY case", out["next"])

    def test_a_trials_band_still_refutes(self):
        """The strong route must not be weakened by the weak one existing."""
        _, far = self.session_with_band((45.1, 45.3, 45.2))
        self.assertEqual(far["metric_verdict"], "diverged")
        self.assertEqual(far["band"]["source"], "trials")
        self.assertTrue(far["band"]["can_refute"])
        self.assertFalse(far["band"]["lower_bound"])

    def test_trials_win_when_both_are_available(self):
        """A run-to-run band is strictly stronger, so it decides -- but the free
        one is kept beside it rather than dropped."""
        code, out, _ = self.open_session()
        sid = out["session_id"]
        for i, v in enumerate((45.1, 45.3, 45.2)):
            self.repro("trial", "--project", self.project, "--session", sid,
                       "--run", self.trial_run(f"run_B{i}", v))
        code, out, err = self.repro("band", "--project", self.project,
                                    "--session", sid, "--from-history",
                                    json.dumps([48.40, 48.45, 48.52, 48.61, 48.47]))
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["band"]["source"], "trials")
        self.assertEqual(out["metric_verdict"], "diverged")
        self.assertEqual(out["band"]["also_measured"]["source"], "run_history")

    # ---- what the band is built from has to be a distribution -------------- #

    def test_a_handful_of_history_points_is_refused(self):
        sid = self.one_trial_session(48.50)
        code, out, _ = self.repro("band", "--project", self.project,
                                  "--session", sid,
                                  "--from-history", json.dumps([48.4, 48.5, 48.6]))
        self.assertEqual(code, 1)
        self.assertIn("not a distribution", json.dumps(out))

    def test_a_history_band_with_nothing_measured_against_it_is_refused(self):
        """With `run_history` the thing tested is the fresh trial, because the
        interval already came from the target. No trial, nothing to judge."""
        code, out, _ = self.open_session()
        sid = out["session_id"]
        code, out, _ = self.repro("band", "--project", self.project,
                                  "--session", sid, "--from-history",
                                  json.dumps([48.4, 48.45, 48.5, 48.55, 48.6]))
        self.assertEqual(code, 1)
        self.assertIn("nothing to judge", json.dumps(out))

    def test_no_trials_and_no_history_names_both_ways_out(self):
        code, out, _ = self.open_session()
        sid = out["session_id"]
        code, out, _ = self.repro("band", "--project", self.project, "--session", sid)
        self.assertEqual(code, 1)
        self.assertIn("--from-history", json.dumps(out))
        self.assertIn("never refute", json.dumps(out))

    # ---- the selection effect, which runs one way ------------------------- #

    def test_a_target_that_is_its_own_tail_max_is_flagged(self):
        """A best-checkpoint pick is the MAX of a wobbling tail. Judged against
        it, a fresh converged run reads as short -- every time, same direction,
        nothing raising. The session has to carry that."""
        sid = self.one_trial_session(48.50)
        # 48.5 is the target value the fixture records, and here it is also the
        # tail's max: exactly the shape of a best-epoch save.
        code, out, err = self.repro("band", "--project", self.project,
                                    "--session", sid, "--from-history",
                                    json.dumps([48.20, 48.31, 48.44, 48.38, 48.50]))
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertTrue(out["band"]["target_is_tail_extreme"])
        joined = " ".join(out["caveats_added"])
        self.assertIn("best-checkpoint pick", joined)
        self.assertIn("bias always runs that way", joined)
        s = json.load(open(os.path.join(self.project, "repro", sid, "session.json")))
        self.assertTrue(any("best-checkpoint pick" in c for c in s["caveats"]),
                        "and it has to reach `caveats`, where the verdict is read")

    def test_a_target_inside_its_tail_is_not_flagged(self):
        sid = self.one_trial_session(48.50)
        code, out, _ = self.repro("band", "--project", self.project,
                                  "--session", sid, "--from-history",
                                  json.dumps([48.20, 48.31, 48.90, 48.38, 48.55]))
        self.assertEqual(code, 0)
        self.assertFalse(out["band"]["target_is_tail_extreme"])

    def test_a_trials_band_narrower_than_the_within_run_wobble_is_flagged(self):
        """Run-to-run spread cannot be smaller than within-run spread. When it
        looks smaller, the repeats held something fixed that the original did
        not -- and the interval they produced is too narrow to judge on."""
        code, out, _ = self.open_session()
        sid = out["session_id"]
        for i, v in enumerate((48.49, 48.50, 48.51)):
            self.repro("trial", "--project", self.project, "--session", sid,
                       "--run", self.trial_run(f"run_B{i}", v))
        code, out, err = self.repro("band", "--project", self.project,
                                    "--session", sid, "--from-history",
                                    json.dumps([47.0, 47.6, 48.2, 48.8, 49.4]))
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertTrue(any("NARROWER" in c for c in out["caveats_added"]))

    def test_history_from_a_file_is_accepted_and_what_it_is_gets_recorded(self):
        """Which epochs count as converged is a judgement; a band whose window
        nobody wrote down cannot be checked later."""
        sid = self.one_trial_session(48.50)
        self.write("tail.json", json.dumps([48.40, 48.45, 48.52, 48.61, 48.47]))
        code, out, err = self.repro(
            "band", "--project", self.project, "--session", sid,
            "--from-history", self.path("tail.json"),
            "--history-what", "epochs 101-140; mosaic closed at 100")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["band"]["history_what"],
                         "epochs 101-140; mosaic closed at 100")

    def test_unreadable_history_breaks_rather_than_refuses(self):
        """Exit 2, not 1: a malformed argument is the script breaking, not the
        answer being no. CLAUDE.md -> "Script Integration"."""
        sid = self.one_trial_session(48.50)
        code, out, _ = self.repro("band", "--project", self.project,
                                  "--session", sid, "--from-history", "{not json")
        self.assertEqual(code, 2)


class AnAxisMayNotAssertACauseTheRecordDoesNotState(ReproCase):
    """run-mechanics.md -> "Record integrity", and CLAUDE.md -> "Never silently"
    (never record a metric you did not read — here, a reason you did not read).

    `code.reproducible: false` correctly forbids `intact`. But the detail asserted
    ONE cause — "a differing file was too large to embed" — because that was the
    only way `code_snapshot.py` could set the flag. Records arrive from elsewhere
    too: an imported external run whose launch script was edited and never
    committed sets the same flag for a completely different reason, and the axis
    then states, in the field a reader acts on, a fact nobody recorded.

    The verdict was right and the explanation was invented. That combination is
    worse than a wrong verdict: nothing looks off, so nobody re-checks.
    """

    def unreproducible_run(self, warnings):
        run = {"run_id": "run_A", "stage": "training", "status": "completed",
               "mode": "production", "scope": {"samples": 1},
               "env": {"packages": {}},
               "metrics": {"best": {"primary_metric": "m", "primary_metric_value": 1}},
               "code": {"origin_commit": self.sha, "dirty_patch_path": None,
                        "dirty_files_count": None, "reproducible": False,
                        "warnings": warnings}}
        return repro.probe_code(self.project, run, "training")

    def test_the_recorded_reason_is_surfaced(self):
        ax = self.unreproducible_run(
            ["THE SCRIPT THAT RAN IS NOT IN GIT: three TODO blanks were filled at "
             "launch and never committed"])
        self.assertEqual(ax["verdict"], "unverifiable")
        self.assertIn("NOT IN GIT", ax["detail"])
        self.assertNotIn("too large to embed", ax["detail"],
                         "the old hardcoded cause must not be asserted over a "
                         "record that states a different one")

    def test_no_recorded_reason_says_so_rather_than_inventing_one(self):
        for warnings in ([], None):
            ax = self.unreproducible_run(warnings)
            self.assertEqual(ax["verdict"], "unverifiable")
            self.assertIn("without a warning saying why", ax["detail"])
            self.assertIsNone(ax.get("recorded_warnings"))

    def test_the_verdict_is_unchanged_whatever_the_reason(self):
        """The flag's meaning is not up for negotiation: a rebuilt tree that is
        not the one that ran can never be `intact`."""
        for warnings in ([], ["anything at all"], ["a", "b"]):
            self.assertEqual(self.unreproducible_run(warnings)["verdict"],
                             "unverifiable")


class ComparingAnOrderStatisticToItsOwnDrawsAnswersNothing(ReproCase):
    """run-mechanics.md -> "Record integrity", and `.claude/skills/repro/references/
    verdicts.md` -> "A band has a source".

    A best-checkpoint save is the MAX of a converged tail. Testing that max against
    the range of the very draws it was taken from is comparing an order statistic to
    individuals: it sits at or above the top BY CONSTRUCTION, so `outside` carries no
    information at all. Found live — a faithful 140-epoch reproduction whose tail mean
    matched the target's to four decimals came back `inconclusive` because its
    best-pick exceeded the target's best-pick by 0.0013.

    The failure is subtle in the way that matters: the verdict was CONSERVATIVE, so
    nothing looked broken. A too-cautious answer produced by a mis-posed question is
    still a wrong answer, and it costs 6.5 GPU-hours per extra trial to act on.

    So the trial's tail may be supplied and the comparison made like for like. This
    is not an escape hatch: the tail must be SUPPLIED (never inferred), both
    distributions are recorded side by side, and the one-directional rule is
    untouched — outside a lower-bound band still cannot mean `diverged`.
    """

    TARGET_TAIL = [48.40, 48.45, 48.52, 48.47, 48.55, 48.44, 48.50]   # max 48.55

    def session_with_trial(self, trial_value):
        code, out, err = self.open_session()
        self.assertEqual(code, 0, f"{out} {err}")
        sid = out["session_id"]
        c, o, e = self.repro("trial", "--project", self.project, "--session", sid,
                             "--run", self.trial_run("run_B0", trial_value))
        self.assertEqual(c, 0, f"{o} {e}")
        return sid

    def band(self, sid, *extra):
        return self.repro("band", "--project", self.project, "--session", sid,
                          "--from-history", json.dumps(self.TARGET_TAIL), *extra)

    def test_a_max_against_its_own_range_is_the_mis_posed_question(self):
        """Kept as the baseline the fix is measured against: without the trial's
        tail, a best-pick above the target's best-pick reads `inconclusive`."""
        sid = self.session_with_trial(48.61)
        code, out, err = self.band(sid)
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["metric_verdict"], "inconclusive")
        self.assertEqual(out["band"]["tested"], "trial")

    def test_like_for_like_tests_the_trial_tail_mean(self):
        sid = self.session_with_trial(48.61)
        trial_tail = [48.38, 48.44, 48.51, 48.46, 48.61, 48.42, 48.49]  # mean ~48.473
        code, out, err = self.band(sid, "--trial-history", json.dumps(trial_tail),
                                   "--trial-history-what", "epochs 94-100")
        self.assertEqual(code, 0, f"{out} {err}")
        self.assertEqual(out["band"]["tested"], "trial_tail_mean")
        self.assertEqual(out["metric_verdict"], "reproduced")
        self.assertIn("trial tail mean", out["why"])

    def test_both_readings_are_recorded_never_just_the_flattering_one(self):
        """The extreme-vs-extreme delta is symmetric and fair on its own terms; it
        is simply not what a range can adjudicate. Dropping it would make this a
        way to launder a worse number."""
        sid = self.session_with_trial(48.61)
        trial_tail = [48.38, 48.44, 48.51, 48.46, 48.61, 48.42, 48.49]
        _, out, _ = self.band(sid, "--trial-history", json.dumps(trial_tail))
        both = out["band"]["both_readings"]
        self.assertIn("like_for_like", both)
        self.assertIn("extreme_vs_extreme", both)
        self.assertAlmostEqual(both["extreme_vs_extreme"]["trial"], 48.61, places=6)
        self.assertAlmostEqual(both["extreme_vs_extreme"]["target"], 48.5, places=6)
        self.assertEqual(out["band"]["trial_history"]["n"], len(trial_tail))

    def test_it_cannot_turn_a_real_divergence_into_a_pass(self):
        """A trial whose whole tail sits far below the target's stays out, and a
        lower-bound band still refuses to call that `diverged`."""
        sid = self.session_with_trial(40.0)
        far = [39.8, 39.9, 40.0, 39.85, 39.95, 39.9, 40.05]
        _, out, _ = self.band(sid, "--trial-history", json.dumps(far))
        self.assertEqual(out["metric_verdict"], "inconclusive",
                         "outside a lower bound is never `diverged` -- unchanged")
        self.assertFalse(out["band"]["can_refute"])

    def test_the_trial_tail_must_be_supplied_not_inferred(self):
        sid = self.session_with_trial(48.61)
        code, out, _ = self.repro("band", "--project", self.project, "--session", sid,
                                  "--trial-history", json.dumps([1, 2, 3, 4, 5]))
        self.assertEqual(code, 2, "a trial tail with no target tail bands nothing")
        code, out, _ = self.band(sid, "--trial-history", json.dumps([48.4, 48.5]))
        self.assertEqual(code, 1)
        self.assertIn("not a distribution", json.dumps(out))
