"""The only irreversible operation on the data line, and the one with no retrain.

`retention.py` deletes checkpoints; a wrong ranking there costs a retrain. This
deletes captures, and 260731 cannot be re-shot. Both halves of CLAUDE.md
"Contracts" apply at once: an irreversible action, and a record written now and
read later by someone who can no longer verify it — nobody re-derives, six
months on, whether the day that is missing was deleted deliberately.

The checks below are grouped by what would go wrong if the code drifted: a
deletion planned off numbers nobody could trust, a path assembled from config
instead of read from a listing, and a citation that outlives its bytes without
saying so.
"""
import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "data-retire/retire.py"


class RetireCase(TempDirCase):
    """Real directories on disk under a `local` location, because `apply`
    actually deletes and a check that stubbed that out would be checking
    something else."""

    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        self.ds = "boxes"
        self.rig = self.path("rig")
        self.auth = self.path("auth")
        self.backup = self.path("backup")
        os.makedirs(self.project, exist_ok=True)
        self.write_json(f"proj/datasets/{self.ds}/dataset.json", {
            "dataset_id": self.ds, "project": self.project,
            "identity": {"unit_glob": "*/*", "unit_label": "scene", "exclude": []},
            "layers": [{"label": "rgb", "marker": "rgb", "kind": "source",
                        "produced_by": None}],
            "completeness": {"marker": "DONE", "partial_marker_field": None},
            "locations": [
                {"key": "auth", "role": "authority", "via": "local",
                 "root": self.auth, "has_layers": None},
                {"key": "rig", "role": "origin", "via": "local",
                 "root": self.rig, "has_layers": None},
                {"key": "backup", "role": "backup", "via": "local",
                 "root": self.backup, "has_layers": None}],
            "replication": {"min_source_copies": 2}, "consumers": [],
            "derived_from": None})

    def on_disk(self, root, *units):
        for u in units:
            os.makedirs(os.path.join(root, u, "rgb"), exist_ok=True)

    def census(self, units, *, complete=True, unreachable=(),
               reachable=("auth", "rig", "backup")):
        """`units` maps unit id -> the location keys holding it. Layers and
        completeness follow unless overridden by a dict."""
        recs, unarchived = {}, []
        for uid, spec in units.items():
            at = spec if isinstance(spec, (list, tuple)) else spec["at"]
            rec = {"at": list(at), "layers": {"rgb": list(at)},
                   "completeness": "complete", "done_at": list(at)}
            if isinstance(spec, dict):
                rec.update({k: v for k, v in spec.items() if k != "at"})
            recs[uid] = rec
            if "auth" not in at:
                unarchived.append(uid)
        cid = "census_20260801_000000"
        self.write_json(f"proj/datasets/{self.ds}/census/{cid}.json", {
            "census_id": cid, "dataset": self.ds, "project": self.project,
            "scanned_at": "2026-08-01T00:00:00+00:00",
            "complete": complete, "unreachable": list(unreachable),
            "root_missing": [],
            "locations": [{"key": k, "reachable": k in reachable,
                           "root": getattr(self, k)}
                          for k in ("auth", "rig", "backup")],
            "units": recs,
            "verdicts": {"gap": {}, "drift": {}, "unreplicated": {},
                         "unarchived": unarchived, "incomplete": [], "partial": []},
            "totals": {"units": len(recs), "unarchived": len(unarchived),
                       "min_source_copies": 2, "unarchived_checked": True}})
        return cid

    def snapshot(self, sid, units, *, manifest=True):
        d = f"proj/datasets/{self.ds}/snapshots/{sid}"
        self.write_json(f"{d}/snapshot.json", {
            "snapshot_id": sid, "cite_as": f"datasets/{self.ds}@{sid}",
            "dataset": self.ds, "project": self.project,
            "frozen_at": "2026-08-01T00:00:00+00:00",
            "from_census": "census_20260801_000000", "manifest": "manifest.jsonl"})
        if manifest:
            self.write(f"{d}/manifest.jsonl", "".join(
                json.dumps({"unit": u}) + "\n" for u in units))

    def plan(self, *extra, at="rig"):
        return run_script(SCRIPT, "plan", "--project", self.project,
                          "--dataset", self.ds, "--at", at, *extra)

    def apply(self, plan_path, token=None, *extra):
        if token is None:
            with open(plan_path, encoding="utf-8") as fh:
                token = json.load(fh)["confirm_token"]
        return run_script(SCRIPT, "apply", "--plan", plan_path,
                          "--confirm", token, *extra)


class NothingIsPlannedOffNumbersNobodyCouldTrust(RetireCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at.

    A partial census undercounts copies, which sounds like the safe direction
    and is not — the location that did not answer may be the very survivor the
    plan is counting on, so "2 copies remain, delete one" becomes a guess about
    a disk nobody could reach. This is the one refusal with no override, and a
    flag appearing here later is the drift this check exists to catch.
    """

    def test_a_partial_census_is_refused(self):
        self.census({"260731/s000": ["auth", "rig"]},
                    complete=False, unreachable=["auth"], reachable=("rig",))
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("PARTIAL", out["refused"])

    def test_no_override_exists_for_a_partial_census(self):
        self.census({"260731/s000": ["auth", "rig"]},
                    complete=False, unreachable=["auth"], reachable=("rig",))
        for flag in ("--waive", "--allow-unreadable-snapshots"):
            args = (flag, "unarchived") if flag == "--waive" else (flag,)
            rc, _, _ = self.plan(*args)
            self.assertEqual(rc, 1, f"{flag} must not unlock a partial census")

    def test_no_census_at_all_is_refused(self):
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("no census", out["refused"])

    def test_an_unreachable_target_is_refused(self):
        self.census({"260731/s000": ["auth"]}, reachable=("auth",))
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("not reachable", out["refused"])

    def test_a_snapshot_with_no_readable_manifest_is_not_one_that_names_nothing(self):
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        self.snapshot("v1", [], manifest=False)
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("v1", out["refused"])
        # ...and waiving it records that they were never consulted
        rc, out, err = self.plan("--allow-unreadable-snapshots")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["unconsulted_snapshots"], ["v1"])

    def test_one_unparseable_manifest_line_is_the_same_failure_as_no_manifest(self):
        """A manifest that opens fine but has one corrupted line used to drop
        just that line's unit from the "named" set (`continue` past the bad
        line) -- letting the rest of a corrupted manifest read as trustworthy.
        It must fail the same way an unopenable manifest does: the whole
        snapshot is unusable, not "usable minus the bad line"."""
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        self.snapshot("v2", ["260731/s000"])
        d = f"proj/datasets/{self.ds}/snapshots/v2"
        self.write(f"{d}/manifest.jsonl",
                  '{"unit": "260731/s000"}\n' + "not json\n")
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("v2", out["refused"])
        rc, out, err = self.plan("--allow-unreadable-snapshots")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["unconsulted_snapshots"], ["v2"])


class UnitsAreRankedByWhatSurvivesTheDeletion(RetireCase):
    """CLAUDE.md -> "Never silently": never delete a file you cannot rank. The
    rank here is survivability, not quality — this script has no opinion about
    whether data is any good, only about what is left when a copy goes.

    Each exclusion names itself so the plan can be read, and each is waivable by
    name so the override says which risk is being accepted. A blanket "yes" flag
    appearing here would collapse four different decisions into one.
    """

    def test_a_third_copy_is_deletable_with_nothing_waived(self):
        """The guard that stops the checks from being unpassable in practice: a
        unit at three locations with a floor of two, archived, complete
        everywhere and cited by nothing, needs no override at all."""
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        rc, out, err = self.plan()
        self.assertEqual(rc, 0, err)
        self.assertEqual([u["unit"] for u in out["delete"]], ["260731/s000"])
        self.assertNotIn("waived", out["delete"][0])

    def test_dropping_under_min_source_copies_is_excluded(self):
        """Two copies with a floor of two: deleting one leaves one, which is the
        state the floor exists to prevent."""
        self.census({"260731/s000": ["auth", "rig"]})
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("below_min_copies", out["excluded_because"])

    def test_an_unarchived_unit_is_excluded(self):
        """It has never reached the authority, so this copy is the only one
        there has ever been."""
        self.census({"260731/s000": ["rig"]})
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("unarchived", out["excluded_because"])

    def test_a_cited_unit_is_excluded(self):
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        self.snapshot("v1", ["260731/s000"])
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("cited_by_snapshot", out["excluded_because"])

    def test_the_last_finished_looking_copy_is_excluded(self):
        """The survivor carries no completeness marker while this one does.
        Deleting it destroys the only version anything claims finished — the
        `dataset.json -> completeness` rule at deletion time."""
        self.census({"260731/s000": {"at": ["auth", "rig", "backup"],
                                     "layers": {"rgb": ["auth", "rig", "backup"]},
                                     "done_at": ["rig"]}})
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("survivor_less_complete", out["excluded_because"])

    def test_each_exclusion_is_waivable_by_its_own_name(self):
        """A unit on the rig alone trips three at once. Waiving two still
        refuses — a blanket "yes" appearing here would collapse three separate
        decisions into one, which is how an override stops carrying
        information."""
        self.census({"260731/s000": ["rig"]})
        rc, _, _ = self.plan("--waive", "below_min_copies")
        self.assertEqual(rc, 1, "waiving one risk must not waive another")
        rc, out, _ = self.plan("--waive", "below_min_copies", "--waive", "unarchived")
        self.assertEqual(rc, 1)
        self.assertEqual(list(out["excluded_because"]), ["survivor_less_complete"])
        rc, out, err = self.plan("--waive", "below_min_copies",
                                 "--waive", "unarchived",
                                 "--waive", "survivor_less_complete")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["delete"][0]["waived"],
                         ["below_min_copies", "survivor_less_complete", "unarchived"])

    def test_nothing_safe_is_an_answer_not_a_crash(self):
        """"0 of 43 units are safe to retire" is the finding. Exit 2 would send
        an agent into the fallback rule and around the check."""
        self.census({"260731/s000": ["rig"]})
        rc, out, _ = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("0 of 1", out["refused"])
        self.assertIn("unarchived", out["excluded_because"])

    def test_a_unit_the_census_never_listed_cannot_be_named(self):
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        rc, out, _ = self.plan("--unit", "260731/ghost")
        self.assertEqual(rc, 1)
        self.assertIn("260731/ghost", out["units"])


class OnlyACensusListedPathIsDeletable(RetireCase):
    """data-line.md -> "Retire: the only delete on this line": a path is deletable
    only if a census listing
    enumerated it, never one assembled from config.

    `locations[].root` is a string somebody edits; a unit id is something a scan
    came back with. This is the guard that stands between a typo in `root` and
    `rm -rf /`, so it is checked at the join and it aborts the whole apply
    rather than deleting the rest.
    """

    def prepared(self, units=("260731/s000",)):
        self.census({u: ["auth", "rig", "backup"] for u in units})
        self.on_disk(self.rig, *units)
        rc, out, err = self.plan()
        self.assertEqual(rc, 0, err)
        return out["_path"]

    def test_a_clean_apply_deletes_and_records(self):
        p = self.prepared()
        rc, out, err = self.apply(p)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["deleted"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.rig, "260731/s000")))
        rec = self.read_json(f"proj/datasets/{self.ds}/retire/{out['retire_id']}.json")
        self.assertEqual(rec["status"], "complete")
        self.assertEqual(rec["deleted"], ["260731/s000"])

    def test_an_escaping_unit_path_aborts_the_whole_apply(self):
        p = self.prepared(("260731/s000", "260731/s001"))
        with open(p, encoding="utf-8") as fh:
            plan = json.load(fh)
        plan["delete"][0]["unit"] = "../../../etc"
        # re-token, so this tests the containment guard rather than the token
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        rc, out, _ = self.apply(p, token=plan["confirm_token"])
        self.assertEqual(rc, 1)
        # nothing was deleted, including the innocent second unit
        self.assertTrue(os.path.exists(os.path.join(self.rig, "260731/s001")))

    def test_an_edited_delete_list_is_caught_by_the_token(self):
        """The token digests exactly the unit list that was ranked, so a unit
        appended by hand was never put through the exclusion checks."""
        p = self.prepared()
        with open(p, encoding="utf-8") as fh:
            plan = json.load(fh)
        plan["delete"].append({"unit": "260731/s999"})
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        rc, out, _ = self.apply(p, token=plan["confirm_token"])
        self.assertEqual(rc, 1)
        self.assertIn("token", out["refused"])

    def test_a_wrong_token_refuses(self):
        p = self.prepared()
        rc, _, _ = self.apply(p, token="deadbeefdeadbeef")
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(os.path.join(self.rig, "260731/s000")))

    def test_a_unit_that_vanished_is_not_recorded_as_deleted_by_us(self):
        """Recording it would put a deletion in the log this tool did not
        perform, which is the one thing the log exists to be right about."""
        p = self.prepared()
        import shutil
        shutil.rmtree(os.path.join(self.rig, "260731/s000"))
        rc, out, _ = self.apply(p)
        self.assertEqual(rc, 1)
        self.assertIn("already absent", out["refused"])

        rc, out, err = self.apply(p, None, "--allow-already-gone")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["deleted"], 0)
        self.assertEqual(out["already_absent"], ["260731/s000"])


class TheRecordOutlivesWhatItDeleted(RetireCase):
    """data-line.md -> "Retire: the only delete on this line": keep the deletion
    log one level above what it
    deletes, so a deletion cannot take its own record with it — and write it
    before the first rm, so a crash leaves a record rather than silence.

    And the citation half: deleting units a frozen snapshot names does not break
    the citation. `datasets/boxes@v1` goes on resolving and the manifest goes on
    listing them, so every run that cited it goes on reading as reproducible.
    The stamp is the only place a reader a year later can find out.
    """

    def test_the_record_lives_in_the_project_not_at_the_deleted_path(self):
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        self.on_disk(self.rig, "260731/s000")
        rc, out, err = self.apply(self.plan()[1]["_path"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(self.path(
            "proj", "datasets", self.ds, "retire", f"{out['retire_id']}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.rig, "260731/s000")))
        # and nothing was written anywhere under the location that was deleted
        # from — a tombstone beside the corpse goes with the next cleanup
        left = [f for _, _, fs in os.walk(self.rig) for f in fs]
        self.assertEqual(left, [])

    def test_waiving_a_citation_stamps_the_loss_into_the_snapshot(self):
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        self.snapshot("v1", ["260731/s000"])
        self.on_disk(self.rig, "260731/s000")
        rc, out, err = self.plan("--waive", "cited_by_snapshot")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.apply(out["_path"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["snapshots_stamped"], ["v1"])
        snap = self.read_json(f"proj/datasets/{self.ds}/snapshots/v1/snapshot.json")
        self.assertEqual(snap["data_retired"][0]["units"], ["260731/s000"])
        self.assertEqual(snap["data_retired"][0]["at"], "rig")

    def test_the_log_reads_back(self):
        """A deletion log nobody can read is a deletion log that did not
        happen."""
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        self.on_disk(self.rig, "260731/s000")
        _, plan, _ = self.plan("--because", "rig is full")
        self.apply(plan["_path"])
        rc, out, err = run_script(SCRIPT, "log", "--project", self.project)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["total_units_deleted"], 1)
        self.assertEqual(out["retirements"][0]["because"], "rig is full")

    def test_the_log_excludes_plans_that_never_ran(self):
        """A plan is not a deletion. Counting one would report data as gone that
        is still on the disk."""
        self.census({"260731/s000": ["auth", "rig", "backup"]})
        self.plan()
        rc, out, err = run_script(SCRIPT, "log", "--project", self.project)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["retirements"], [])


if __name__ == "__main__":
    unittest.main()
