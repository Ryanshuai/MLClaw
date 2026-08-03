"""A citation that still resolves is the hardest kind of wrong.

`/data`'s reason to exist is one fact nothing else computes: a frozen snapshot
whose census never saw inflow that has since been accepted. Every part of that
failure looks healthy. `datasets/boxes@260731` still names a real set, the
manifest still lists real units, the run that cites it still reproduces — and
"we trained on the latest data" is false, with nothing anywhere raising.

That is the bar in CLAUDE.md "Contracts": a record written now and read later by
someone who can no longer verify it. Nobody re-derives, six months on, whether
the snapshot behind a training run predated the third annotation batch.

The checks below are grouped by what would go wrong if the code drifted:
staleness measured against the wrong timestamp (the bug this file was written
after finding), an unknown ordering reported as clean, and gates that stop the
wrong transition.
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import TempDirCase, run_script

SCRIPT = "data/phase.py"


def ts(offset_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat(
        timespec="seconds")


class DataCase(TempDirCase):
    """Builds the record tree by hand rather than by driving census.py: these
    checks are about how `/data` *joins* the four record types, so the inputs
    have to be settable to shapes a live scan would take hours to produce (a
    census older than a handoff, a missing census file, a naive timestamp)."""

    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        os.makedirs(self.project, exist_ok=True)
        self.ds = "boxes"
        self.ddir = self.path("proj", "datasets", self.ds)
        self.write_json(f"proj/datasets/{self.ds}/dataset.json", {
            "dataset_id": self.ds, "project": self.project,
            "identity": {"unit_glob": "*", "unit_label": "scene", "exclude": []},
            "layers": [{"label": "rgb", "marker": "rgb", "kind": "source",
                        "produced_by": None},
                       {"label": "depth", "marker": "depth", "kind": "derived",
                        "produced_by": "run:training"}],
            "completeness": {"marker": "DONE", "partial_marker_field": None},
            "locations": [{"key": "auth", "role": "authority", "via": "local",
                           "root": self.path("authority")}],
            "replication": {"min_source_copies": 2}, "consumers": [],
        })

    def census(self, cid="census_20260731_000000", *, scanned_at=None, complete=True,
               verdicts=None, totals=None):
        rec = {"census_id": cid, "dataset": self.ds, "project": self.project,
               "scanned_at": scanned_at or ts(-600), "complete": complete,
               "unreachable": [] if complete else ["rig"], "root_missing": [],
               "locations": [], "units": {},
               "verdicts": verdicts or {},
               "totals": {"units": 2, "gap": 0, "drift": 0, "unreplicated": 0,
                          "unarchived": 0, "incomplete": 0, "partial": 0,
                          "unarchived_checked": True, **(totals or {})}}
        self.write_json(f"proj/datasets/{self.ds}/census/{cid}.json", rec)
        return rec

    def snapshot(self, sid="s1", *, from_census="census_20260731_000000", frozen_at=None):
        self.write_json(f"proj/datasets/{self.ds}/snapshots/{sid}/snapshot.json", {
            "snapshot_id": sid, "cite_as": f"datasets/{self.ds}@{sid}",
            "dataset": self.ds, "project": self.project,
            "frozen_at": frozen_at or ts(), "from_census": from_census,
            "unverified_units": [], "unverifiable_units": [], "layer_coverage": {}})

    def handoff(self, hid="handoff_1", *, status="accepted", closed_at=None,
                dataset="__self__", created_at=None):
        self.write_json(f"proj/handoffs/{hid}/handoff.json", {
            "handoff_id": hid, "project": "proj", "stage": None,
            "dataset": self.ds if dataset == "__self__" else dataset,
            "kind": "annotation", "status": status, "round": 1,
            "to": "vendor-a", "created_at": created_at or ts(-3600),
            "closed_at": closed_at if status == "accepted" else None,
            "accepted": {"coverage": 1.0} if status == "accepted" else None})

    def phase(self, *extra):
        rc, out, err = run_script(SCRIPT, "phase", "--project", self.project,
                                  "--dataset", self.ds, *extra)
        self.assertEqual(rc, 0, f"phase rc={rc} err={err}")
        return out["datasets"][0]

    def gate(self, to, *extra):
        return run_script(SCRIPT, "gate", "--project", self.project,
                          "--dataset", self.ds, "--to", to, *extra)

    def blockers(self, assessment):
        return {b["blocker"] for b in assessment["blockers"]}


class StalenessIsMeasuredAgainstTheCensus(DataCase):
    """CLAUDE.md -> "Never silently": a snapshot's contents come from the census
    it froze from, not from the moment somebody ran the freeze.

    This is the check the file was written after failing. Comparing
    `snapshot.frozen_at` against `handoff.closed_at` passes every ordinary case
    and reverses the one that matters: freeze at 5pm off a census scanned
    yesterday cannot contain labels accepted at noon, but its timestamp is the
    newer of the two, so the stale snapshot reports as current.
    """

    def test_a_freeze_newer_than_the_inflow_is_still_stale_if_its_census_is_older(self):
        self.census("census_old", scanned_at=ts(-86400))     # scanned yesterday
        self.handoff(closed_at=ts(-3600))                    # accepted an hour ago
        self.snapshot(from_census="census_old", frozen_at=ts())  # frozen just now
        a = self.phase()
        self.assertIn("snapshot_stale", self.blockers(a))
        self.assertEqual(a["phase"], "freeze")

    def test_a_census_that_saw_the_inflow_is_current(self):
        self.census("census_new", scanned_at=ts(-60))
        self.handoff(closed_at=ts(-600))
        self.snapshot(from_census="census_new")
        a = self.phase()
        self.assertNotIn("snapshot_stale", self.blockers(a))
        self.assertEqual(a["phase"], "ready")

    def test_equal_timestamps_count_as_stale(self):
        """One-second resolution makes the ordering unknown, and unknown must
        not read as clean — the same resolution collision that lets two runs
        share a run_id."""
        same = ts(-100)
        self.census("census_tie", scanned_at=same)
        self.handoff(closed_at=same)
        self.snapshot(from_census="census_tie")
        self.assertIn("snapshot_stale", self.blockers(self.phase()))

    def test_only_accepted_inflow_counts(self):
        """A rejected or cancelled handoff brought nothing in, so it cannot make
        a snapshot stale; an OPEN one is a different blocker, not this one."""
        self.census("c", scanned_at=ts(-600))
        self.handoff("handoff_r", status="rejected")
        self.snapshot(from_census="c")
        self.assertNotIn("snapshot_stale", self.blockers(self.phase()))

    def test_a_handoff_for_another_dataset_is_not_this_dataset_s_inflow(self):
        self.census("c", scanned_at=ts(-600))
        self.handoff("handoff_x", closed_at=ts(), dataset="other_ds")
        self.snapshot(from_census="c")
        self.assertNotIn("snapshot_stale", self.blockers(self.phase()))


class NotComparedIsNotFresh(DataCase):
    """run-mechanics.md -> "Record integrity": extraction failure and absence are
    different facts and must not become the same value.

    The checker is capable of committing that bug against itself. When a
    timestamp is missing, unparseable, or carries no UTC offset, the ordering is
    unknown — and silently skipping it produces an empty stale list, which reads
    downstream as "checked, and it is current".
    """

    def test_a_naive_timestamp_is_undetermined_not_fresh(self):
        self.census("c", scanned_at="2026-07-31T00:00:00")   # no offset
        self.handoff(closed_at=ts(-600))
        self.snapshot(from_census="c")
        a = self.phase()
        self.assertIn("staleness_undetermined", self.blockers(a))
        self.assertEqual(len(a["staleness_undetermined"]), 1)
        self.assertNotEqual(a["phase"], "ready")

    def test_a_missing_census_file_is_undetermined_not_fresh(self):
        self.census("c_present", scanned_at=ts(-600))
        self.handoff(closed_at=ts(-300))
        self.snapshot(from_census="c_vanished")
        self.assertIn("staleness_undetermined", self.blockers(self.phase()))

    def test_undetermined_blocks_consume(self):
        self.census("c", scanned_at="2026-07-31T00:00:00")
        self.handoff(closed_at=ts(-600))
        self.snapshot(from_census="c")
        rc, _, _ = self.gate("consume")
        self.assertEqual(rc, 1)


class PhaseIsTheEarliestUnfinishedBox(DataCase):
    """data-line.md -> "The line, and what each phase owns": Collect → Label → Curate →
    Freeze, in that order. Reporting the most alarming finding instead of the
    earliest unfinished one sends people to fix the wrong end of the line.
    """

    def test_no_census_is_collect(self):
        self.assertEqual(self.phase()["phase"], "collect")

    def test_incomplete_units_outrank_a_later_gap(self):
        self.census(totals={"incomplete": 1, "gap": 1},
                    verdicts={"gap": {"depth": ["u1"]}})
        self.assertEqual(self.phase()["phase"], "collect")

    def test_an_open_handoff_outranks_a_gap(self):
        self.census(totals={"gap": 1}, verdicts={"gap": {"depth": ["u1"]}})
        self.handoff(status="sent")
        self.assertEqual(self.phase()["phase"], "label")

    def test_a_gap_routes_by_the_layers_produced_by(self):
        self.census(totals={"gap": 1}, verdicts={"gap": {"depth": ["u1"]}})
        a = self.phase()
        self.assertEqual(a["phase"], "curate")
        self.assertEqual(a["next"], "run:training")
        # `run:<stage>` means an MLClaw stage makes this layer, so the composer
        # goes to that stage's run skill — not to /data-curate, which records
        # derivations rather than producing a layer in place.
        self.assertEqual(a["next_skill"], "/training-run")

    def test_every_unfinished_phase_names_a_skill_to_compose(self):
        """data-line.md -> "How /data composes the line": /data composes through the
        Skill Dependency
        Graph and never through a second mechanism of its own. A phase whose
        only `next` is a shell command is a phase it cannot route, so the
        composition contract would be true of the prose and false of the code.

        `ready` is the deliberate exception: the data line is done, and what
        happens next belongs to the model lifecycle.
        """
        self.census()                                   # -> freeze
        a = self.phase()
        self.assertEqual(a["next_skill"], "/data-freeze")

        self.snapshot(from_census="census_20260731_000000")
        b = self.phase()
        self.assertEqual(b["phase"], "ready")
        self.assertIsNone(b["next_skill"],
                          "the data line does not compose across into training")

    def test_clean_and_unfrozen_is_freeze(self):
        self.census()
        self.assertEqual(self.phase()["phase"], "freeze")

    def test_retire_is_never_returned_as_a_position(self):
        """Retirement is an action on units, not a state a dataset arrives at:
        one that has had forty days deleted off the rig is still `ready`. But a
        phase list that just stopped at `ready` would leave the box unnamed, so
        it reports what HAS been deleted instead — a count in a census counts
        what is left and says nothing about what used to be there."""
        self.census()
        self.write_json(f"proj/datasets/{self.ds}/retire/retire_1.json", {
            "retire_id": "retire_1", "at": "rig", "status": "complete",
            "finished_at": ts(-3600), "deleted": ["u1", "u2"],
            "waived": ["unarchived"], "because": "rig full"})
        # ...and a plan is not a deletion; counting one reports data as gone
        # that is still on the disk.
        self.write_json(f"proj/datasets/{self.ds}/retire/retire_2_plan.json", {
            "retire_id": "retire_2", "delete": [{"unit": "u3"}]})
        a = self.phase()
        self.assertFalse(a["retire"]["is_a_phase"])
        self.assertEqual(a["retire"]["units_deleted"], 2)
        self.assertEqual(a["retire"]["waived"], ["unarchived"])
        self.assertEqual(a["phase"], "freeze", "a retirement is not a position")


class AReplayShowsWhatWasTrueThen(DataCase):
    """CLAUDE.md -> "Never silently": never let somebody's word become a checked
    fact — here the word is the record's *current* state standing in for its past.

    The records on disk are all current. A snapshot frozen this morning sits in
    the same directory as one from June, and a handoff accepted on Friday reads
    as accepted for every week before it. Reading them straight would draw a
    timeline in which nothing was ever in flight and everything was always
    frozen: a history that reassures rather than informs.
    """

    def history(self, *extra):
        rc, out, err = run_script(SCRIPT, "history", "--project", self.project,
                                  "--dataset", self.ds, *extra)
        self.assertEqual(rc, 0, f"history rc={rc} err={err}")
        return {t["census_id"]: t for t in out["datasets"][0]["timeline"]}

    def test_a_handoff_terminal_now_was_still_out_then(self):
        """The load-bearing one. `status: accepted` is today's fact; on the
        Tuesday before it closed, the batch was out and the dataset was in
        Label."""
        self.census("census_a", scanned_at=ts(-86400 * 6))
        self.census("census_b", scanned_at=ts(-3600))
        self.handoff(created_at=ts(-86400 * 8), closed_at=ts(-86400 * 3))
        h = self.history()
        self.assertEqual(h["census_a"]["phase"], "label")
        self.assertEqual(h["census_a"]["handoffs"]["open"], 1)
        self.assertEqual(h["census_b"]["handoffs"]["open"], 0)

    def test_a_handoff_not_yet_sent_is_absent_not_open(self):
        """The mirror of the above, and the reason `created_at` is read at all:
        a batch sent last week did not exist the month before."""
        self.census("census_a", scanned_at=ts(-86400 * 30))
        self.handoff(created_at=ts(-86400 * 7), status="sent")
        self.assertEqual(self.history()["census_a"]["handoffs"]["open"], 0)

    def test_a_snapshot_frozen_later_is_absent_from_an_earlier_column(self):
        self.census("census_a", scanned_at=ts(-86400 * 6))
        self.census("census_b", scanned_at=ts(-86400 * 3))
        self.snapshot("v1", from_census="census_b", frozen_at=ts(-86400 * 2))
        self.census("census_c", scanned_at=ts(-3600))
        h = self.history()
        self.assertEqual(h["census_a"]["snapshots"], [])
        self.assertEqual(h["census_a"]["phase"], "freeze")
        self.assertEqual(h["census_b"]["snapshots"], [],
                         "a snapshot cannot predate the scan it froze from")
        self.assertEqual(h["census_c"]["snapshots"], ["v1"])
        self.assertEqual(h["census_c"]["phase"], "ready")

    def test_a_record_that_cannot_be_placed_in_time_marks_the_column(self):
        """Not placed is not absent. Dropping it would replay the moment as
        though the record had never been made, and the column would look clean
        rather than incomplete."""
        self.census("census_a", scanned_at=ts(-3600))
        self.write_json("proj/handoffs/handoff_naive/handoff.json", {
            "handoff_id": "handoff_naive", "project": "proj", "dataset": self.ds,
            "kind": "annotation", "status": "sent", "round": 1, "to": "vendor-a",
            "created_at": "2026-07-31T00:00:00", "closed_at": None})   # no offset
        t = self.history()["census_a"]
        self.assertFalse(t["replay"]["complete"])
        self.assertEqual(t["replay"]["unplaceable"][0]["id"], "handoff_naive")
        self.assertIn("replay_incomplete", {b["blocker"] for b in t["blockers"]})

    def test_todays_census_age_is_not_stamped_on_every_past_column(self):
        """`census_stale` is measured from now, so in a replay it would report
        that a scan was three weeks old on the day it ran."""
        self.census("census_a", scanned_at=ts(-86400 * 40))
        self.census("census_b", scanned_at=ts(-86400 * 39))
        for t in self.history().values():
            self.assertNotIn("census_stale", {b["blocker"] for b in t["blockers"]})
        # ...while the current view still reports it
        self.assertIn("census_stale", self.blockers(self.phase()))

    def test_one_set_of_rules_over_two_sets_of_records(self):
        """When nothing has happened since the scan, the last column and the
        live view agree — same rules, same records. That is the invariant; it is
        not that they always match.

        Note what the fixture needs: a census AFTER the freeze. A snapshot cut
        from census N necessarily postdates N, so N's own replay correctly shows
        nothing frozen, and a board with no live column would never show a fresh
        freeze at all.
        """
        self.census("census_a", scanned_at=ts(-86400 * 2))
        self.handoff(created_at=ts(-86400 * 3), closed_at=ts(-86400 * 2.5))
        self.snapshot(from_census="census_a", frozen_at=ts(-86400))
        self.census("census_b", scanned_at=ts(-600))
        h = self.history()
        self.assertEqual(h["census_a"]["snapshots"], [],
                         "the freeze had not happened when census_a ran")
        live, replayed = self.phase(), h["census_b"]
        self.assertEqual(replayed["phase"], live["phase"])
        self.assertEqual(replayed["why"], live["why"])

    def test_the_last_column_may_legitimately_disagree_with_now(self):
        """And when something HAS happened since, they must differ — a batch
        accepted after the last scan makes the snapshot stale today while the
        scan itself still shows it out. Forcing the two into agreement would
        erase exactly the gap this tool exists to surface, in whichever
        direction it was forced.
        """
        self.census("census_a", scanned_at=ts(-600))
        self.handoff(created_at=ts(-3600), closed_at=ts(-300))   # after the scan
        self.snapshot(from_census="census_a")
        live, replayed = self.phase(), self.history()["census_a"]
        self.assertEqual(replayed["phase"], "label",
                         "at the scan, the batch was still out")
        self.assertEqual(replayed["handoffs"]["open"], 1)
        self.assertEqual(live["phase"], "freeze")
        self.assertIn("snapshot_stale", self.blockers(live))


class GatesStopOneTransitionEach(DataCase):
    """CLAUDE.md -> "Script Integration": exit 1 = worked, the answer is no.

    A gate that fires on every transition trains people to pass the override,
    at which point it protects nothing. Each blocker names the one transition it
    stops; only a census that could not be trusted stops all of them.
    """

    def test_open_inflow_stops_freeze_only(self):
        self.census()
        self.handoff(status="sent")
        self.assertEqual(self.gate("freeze")[0], 1)
        self.assertEqual(self.gate("curate")[0], 0)

    def test_unreplicated_source_stops_curate_only(self):
        self.census(totals={"unreplicated": 3})
        self.assertEqual(self.gate("curate")[0], 1)
        self.assertEqual(self.gate("freeze")[0], 0)

    def test_an_untrustworthy_census_stops_everything(self):
        """A partial census makes every count a lower bound, so no transition
        can be justified from it."""
        self.census(complete=False)
        for to in ("freeze", "curate", "consume"):
            self.assertEqual(self.gate(to)[0], 1, f"{to} should be blocked")

    def test_acknowledge_must_restate_the_measured_count(self):
        self.census(totals={"unreplicated": 3})
        self.assertEqual(self.gate("curate", "--acknowledge", "2")[0], 1)
        rc, out, err = self.gate("curate", "--acknowledge", "1")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["passed"])

    def test_a_clean_dataset_needs_no_override(self):
        """A guard that always fires is a guard nobody reads."""
        self.census()
        self.snapshot(from_census="census_20260731_000000")
        for to in ("freeze", "curate", "consume"):
            self.assertEqual(self.gate(to)[0], 0, f"{to} should pass cleanly")

    def test_a_broken_input_exits_2_not_1(self):
        """The fallback rule depends on this split: a refusal that exits 2 gets
        redone by hand, which routes around the gate."""
        rc, _, _ = run_script(SCRIPT, "phase", "--project", self.project,
                              "--dataset", "no_such_dataset")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
