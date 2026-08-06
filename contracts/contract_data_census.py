"""Contract checks for the data census layer.

Two rules carry this file, and they are the two whose breach raises nothing:

**"I could not look" is not "it is not there."** A census reports across machines,
so failure to reach one is an ordinary outcome — and if it is spelled the same
way as an empty disk, the report is confidently wrong in the direction that hurts:
a backup nobody can reach reads as a backup holding nothing, which reads as a
backup that needs filling, which reads as work rather than as an outage.

**A directory is not a completion certificate.** Creating the output directory up
front is a normal capture design (an operator with no screen must see the number
they are shooting into), so its existence proves nothing. A half-finished unit
that reads as whole is the only defect here that survives into a trained model.

The rest guard the verdict arithmetic, because a verdict is what someone deletes
on the strength of.
"""
import io
import json
import os
import unittest
from argparse import Namespace
from contextlib import redirect_stdout

from helpers import TempDirCase, load_script, run_script

census = load_script("data-check/census.py")


def layer(label, marker, kind="source", **kw):
    return {"label": label, "marker": marker, "kind": kind, "produced_by": None, **kw}


class CensusCase(TempDirCase):
    """A project with a declared dataset and real directories to scan."""

    def declare(self, locations, layers=None, completeness=None, min_copies=2,
                unit_glob="*/*", exclude=("_rig",)):
        cfg = {
            "dataset_id": "ds",
            "identity": {"unit_glob": unit_glob, "unit_label": "scene",
                         "exclude": list(exclude)},
            "layers": layers or [layer("rgbd", "rgbd/rig.json", "source"),
                                 layer("sfm", "sfm", "derived")],
            "completeness": completeness or {"marker": "rgbd/capture_done.json",
                                            "partial_marker_field": "ended"},
            "locations": locations,
            "replication": {"min_source_copies": min_copies},
        }
        self.write_json("proj/datasets/ds/dataset.json", cfg)
        self.write_json("proj/project.json", {"name": "proj"})
        return cfg

    def loc(self, key, root, role="working", **kw):
        return {"key": key, "role": role, "via": "local",
                "server": None, "root": self.path(root), "has_layers": None, **kw}

    def unit(self, root, uid, layers=(), done=None):
        """Build one unit on disk. `done=None` writes no completion marker."""
        for rel in layers:
            marker = {"rgbd": "rgbd/rig.json", "sfm": "sfm",
                      "gt": "gt/boxes/seqbox.npy"}[rel]
            full = self.path(root, uid, marker)
            os.makedirs(os.path.dirname(full) if "." in os.path.basename(marker)
                        else full, exist_ok=True)
            if "." in os.path.basename(marker):
                self.write(os.path.join(root, uid, marker), "{}")
        if done is not None:
            body = {"shots": 3} if done is True else {"shots": 3, "ended": done}
            self.write(os.path.join(root, uid, "rgbd/capture_done.json"),
                       json.dumps(body))
        os.makedirs(self.path(root, uid), exist_ok=True)

    def scan(self, allow_unreachable=True):
        args = Namespace(project=self.path("proj"), dataset="ds", json=False,
                         allow_unreachable=allow_unreachable, fn=None)
        try:
            census.cmd_scan(args)
        except SystemExit as e:
            if not allow_unreachable:
                raise
            if e.code not in (0, None):
                raise
        cdir = self.path("proj/datasets/ds/census")
        names = sorted(os.listdir(cdir))
        return self.read_json(f"proj/datasets/ds/census/{names[-1]}")


class ThreeStatesNeverTwo(CensusCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at. A
    machine that did not answer, a path that is not there, and a directory that is
    genuinely empty are three facts, and only the last means the data is gone.
    """

    def test_unreachable_is_not_empty(self):
        cfg = self.declare([self.loc("gone", "nowhere", role="authority")])
        cfg["locations"][0].update(via="server", server="no.such.host.invalid",
                                   root="/data")
        r = census.probe(cfg["locations"][0], cfg)
        self.assertFalse(r["reachable"],
                         "an unreachable host must not report as reachable")
        self.assertIsNotNone(r.get("error"), "why it failed must be recorded")
        self.assertNotIn("units", r,
                         "an unreachable host must produce no unit listing at all — "
                         "an empty dict here is what reads as 'zero scenes'")

    def test_missing_root_is_reachable_but_not_empty(self):
        cfg = self.declare([self.loc("a", "absent", role="authority")])
        r = census.probe(cfg["locations"][0], cfg)
        self.assertTrue(r["reachable"])
        self.assertTrue(r["root_missing"],
                        "the machine answered; the path is not there. That is a "
                        "third state, not a flavour of empty")

    def test_present_but_empty_is_the_only_real_zero(self):
        os.makedirs(self.path("empty"))
        cfg = self.declare([self.loc("a", "empty", role="authority")])
        r = census.probe(cfg["locations"][0], cfg)
        self.assertTrue(r["reachable"])
        self.assertFalse(r["root_missing"])
        self.assertEqual(r["units"], {})

    def test_partial_census_is_marked_and_refused(self):
        self.unit("auth", "260725/s000", ["rgbd"], done=True)
        cfg = self.declare([self.loc("auth", "auth", role="authority"),
                            self.loc("off", "x", role="backup")])
        cfg["locations"][1].update(via="server", server="no.such.host.invalid",
                                   root="/data")
        self.write_json("proj/datasets/ds/dataset.json", cfg)

        c = self.scan()
        self.assertFalse(c["complete"],
                         "a census missing a location must not read as complete")
        self.assertEqual(c["unreachable"], ["off"])

        with self.assertRaises(SystemExit) as cm:
            self.scan(allow_unreachable=False)
        self.assertEqual(cm.exception.code, 1,
                         "exit 1: the script worked and the answer is no")

    def test_unarchived_not_checked_is_not_zero(self):
        """The authority is the only thing UNARCHIVED can be measured against."""
        self.unit("box", "260727/s000", ["rgbd"], done=True)
        cfg = self.declare([self.loc("auth", "x", role="authority"),
                            self.loc("box", "box", role="origin",
                                     has_layers=["rgbd"])])
        cfg["locations"][0].update(via="server", server="no.such.host.invalid",
                                   root="/data")
        self.write_json("proj/datasets/ds/dataset.json", cfg)
        c = self.scan()
        self.assertFalse(c["totals"]["unarchived_checked"],
                         "with the authority unreachable, 'never copied off' was "
                         "never computed and must not be filed as none found")
        self.assertEqual(c["verdicts"]["unarchived"], [])


class CompletenessIsAMarker(CensusCase):
    """CLAUDE.md -> "Never silently": never say a unit is complete because its
    directory exists. Completion is the marker named in
    `dataset.json -> completeness`, written when the work ended.
    """

    def test_directory_without_marker_is_incomplete(self):
        self.unit("auth", "260725/s000", ["rgbd", "sfm"], done=None)
        self.declare([self.loc("auth", "auth", role="authority")])
        c = self.scan()
        self.assertEqual(c["units"]["260725/s000"]["completeness"], "incomplete")
        self.assertEqual(c["verdicts"]["incomplete"], ["260725/s000"])

    def test_no_marker_declared_is_unverifiable_never_complete(self):
        self.unit("auth", "260725/s000", ["rgbd"], done=True)
        self.declare([self.loc("auth", "auth", role="authority")],
                     completeness={"marker": None, "partial_marker_field": None})
        c = self.scan()
        self.assertEqual(c["units"]["260725/s000"]["completeness"], "unverifiable",
                         "an undeclared marker must never resolve to 'complete' — "
                         "that is the default that makes a half unit read as whole")
        self.assertEqual(c["verdicts"]["incomplete"], [])

    def test_interrupted_marker_is_partial_not_complete(self):
        self.unit("auth", "260725/s000", ["rgbd"], done="interrupted")
        self.unit("auth", "260725/s001", ["rgbd"], done=True)
        self.declare([self.loc("auth", "auth", role="authority")])
        c = self.scan()
        self.assertEqual(c["units"]["260725/s000"]["completeness"], "partial")
        self.assertEqual(c["units"]["260725/s001"]["completeness"], "complete",
                         "a cleanly ended unit carries no such key, and its absence "
                         "is the clean signal — not a missing read")
        self.assertEqual([p["unit"] for p in c["verdicts"]["partial"]],
                         ["260725/s000"])


class VerdictArithmetic(CensusCase):
    """layout.md -> "Dataset identity and census records": `kind` is what makes a
    missing copy readable — `derived` can be recomputed, `source` cannot and its
    copy count is the data's survival odds. A verdict is what someone deletes on
    the strength of, so the counts have to mean what they say.
    """

    def test_zero_copies_is_never_under_replication(self):
        """A regression. `present == []` is a GAP or a by-design absence; feeding
        it to the copy-count check reports every unit still on its origin machine
        as an under-replicated copy of every layer that machine never held."""
        self.unit("box", "260727/s000", ["rgbd"], done=True)
        self.declare([self.loc("auth", "auth", role="authority"),
                      self.loc("box", "box", role="origin", has_layers=["rgbd"])],
                     layers=[layer("rgbd", "rgbd/rig.json", "source"),
                             layer("gt", "gt/boxes/seqbox.npy", "human_locked")])
        os.makedirs(self.path("auth"))
        c = self.scan()
        self.assertNotIn("gt", c["verdicts"]["unreplicated"],
                         "gt exists nowhere for this unit — that is not 'too few "
                         "copies', and the box was never supposed to hold it")
        self.assertEqual(c["verdicts"]["unreplicated"].get("rgbd"), ["260727/s000"])

    def test_by_design_absence_is_not_a_gap(self):
        """`has_layers` on a location is a design statement, not an inventory. A
        capture box reported as missing every downstream layer is all noise, and
        a report that is all noise gets ignored, which is worse than none."""
        self.unit("box", "260727/s000", ["rgbd"], done=True)
        self.unit("auth", "260727/s000", ["rgbd", "sfm"], done=True)
        self.declare([self.loc("auth", "auth", role="authority"),
                      self.loc("box", "box", role="origin", has_layers=["rgbd"])])
        c = self.scan()
        self.assertEqual(c["verdicts"]["gap"], {},
                         "sfm is present at the authority and not expected on the "
                         "box, so nothing is missing anywhere")
        self.assertEqual(c["verdicts"]["drift"], {})

    def test_unarchived_is_distinct_from_unreplicated(self):
        """Copy count and reach are different questions: two copies that never
        entered the pipeline is a different state from one copy that did."""
        self.unit("box", "260727/s000", ["rgbd"], done=True)
        self.unit("mirror", "260727/s000", ["rgbd"], done=True)
        self.unit("auth", "260725/s000", ["rgbd"], done=True)
        self.declare([self.loc("auth", "auth", role="authority"),
                      self.loc("box", "box", role="origin", has_layers=["rgbd"]),
                      self.loc("mirror", "mirror", role="backup",
                               has_layers=["rgbd"])])
        c = self.scan()
        self.assertEqual(c["verdicts"]["unarchived"], ["260727/s000"],
                         "two copies, and it has still never reached the authority")
        self.assertNotIn("260727/s000",
                         c["verdicts"]["unreplicated"].get("rgbd", []),
                         "it is replicated; that is precisely why the two verdicts "
                         "cannot be one field")
        self.assertIn("260725/s000", c["verdicts"]["unreplicated"]["rgbd"])

    def test_working_location_absence_is_not_drift(self):
        self.unit("auth", "260725/s000", ["rgbd", "sfm"], done=True)
        self.unit("laptop", "260725/s000", ["rgbd"], done=True)
        self.declare([self.loc("auth", "auth", role="authority"),
                      self.loc("laptop", "laptop", role="working")], min_copies=1)
        c = self.scan()
        self.assertEqual(c["verdicts"]["drift"], {},
                         "a working set is a deliberate subset; flagging its "
                         "absences buries the real ones")

    def test_the_working_exclusion_is_one_directional(self):
        """The other direction is not a subset, it is unpublished work.

        Suppressing "the laptop is missing sfm" must not also suppress "the
        laptop has an sfm the authority never got" — that layer exists on one
        machine that is not the one anything computes from, and the whole
        exclusion would be a hiding place if it cut both ways.
        """
        self.unit("auth", "260725/s000", ["rgbd"], done=True)
        self.unit("laptop", "260725/s000", ["rgbd", "sfm"], done=True)
        self.declare([self.loc("auth", "auth", role="authority"),
                      self.loc("laptop", "laptop", role="working")], min_copies=1)
        c = self.scan()
        self.assertEqual(c["verdicts"]["drift"].get("sfm"), ["260725/s000"],
                         "present on the working machine, absent at the authority "
                         "— that is drift toward the authority, not a subset")


class DeclarationRefusals(CensusCase):
    """CLAUDE.md -> "Never silently": the fields that decide whether a report means
    "run the job again" or "it is gone" are refused rather than defaulted.
    """

    def assert_refuses(self, mutate):
        cfg = self.declare([self.loc("a", "auth", role="authority")])
        mutate(cfg)
        self.write_json("proj/datasets/ds/dataset.json", cfg)
        with self.assertRaises(SystemExit) as cm:
            census.load_layout_contract(self.path("proj"), "ds")
        self.assertEqual(cm.exception.code, 1)

    def test_empty_unit_glob(self):
        self.assert_refuses(lambda c: c["identity"].update(unit_glob=""))

    def test_missing_layer_kind(self):
        self.assert_refuses(lambda c: c["layers"][0].update(kind="maybe"))

    def test_missing_marker(self):
        self.assert_refuses(lambda c: c["layers"][0].update(marker=""))

    def test_label_that_would_corrupt_the_probe(self):
        """Labels ride into a shell list as `label:marker` and come back
        comma-separated. A label containing either character does not error — it
        silently reparses into a different layer name."""
        for bad in ("rg:bd", "rg,bd", "rg bd"):
            with self.subTest(label=bad):
                self.assert_refuses(lambda c, b=bad: c["layers"][0].update(label=b))

    def test_two_authorities(self):
        """DRIFT is resolved *toward* something; two candidates means it isn't."""
        self.assert_refuses(lambda c: c["locations"].append(
            {"key": "b", "role": "authority", "via": "local", "server": None,
             "root": "/tmp", "has_layers": None}))

    def test_undeclared_dataset(self):
        self.write_json("proj/project.json", {"name": "proj"})
        with self.assertRaises(SystemExit) as cm:
            census.load_layout_contract(self.path("proj"), "nope")
        self.assertEqual(cm.exception.code, 1)


class SnapshotRefusals(CensusCase):
    """layout.md -> "Dataset identity and census records": a consuming run cites a
    snapshot, never the dataset id alone. That makes the frozen set a record
    somebody reads later and cannot re-verify — so what may enter it is checked.
    """

    def setUp(self):
        super().setUp()
        self.unit("auth", "260725/s000", ["rgbd", "sfm"], done=True)
        self.unit("auth", "260725/s001", ["rgbd"], done=None)      # incomplete
        self.declare([self.loc("auth", "auth", role="authority")], min_copies=1)

    def snap(self, **kw):
        args = Namespace(project=self.path("proj"), dataset="ds", json=False,
                         id="v1", layer=None, at=None, units_from=None,
                         allow_incomplete=-1, fn=None)
        for k, v in kw.items():
            setattr(args, k, v)
        return census.cmd_snapshot(args)

    def test_refuses_unverified_units(self):
        self.scan()
        with self.assertRaises(SystemExit) as cm:
            self.snap()
        self.assertEqual(cm.exception.code, 1)

    def test_allow_incomplete_binds_to_the_measured_count(self):
        self.scan()
        with self.assertRaises(SystemExit) as cm:
            self.snap(allow_incomplete=99)
        self.assertEqual(cm.exception.code, 1,
                         "a remembered number must not unlock a different set")
        self.snap(allow_incomplete=1)
        snap = self.read_json("proj/datasets/ds/snapshots/v1/snapshot.json")
        self.assertEqual(snap["unverified_units"], ["260725/s001"],
                         "what was accepted has to survive into the record, or the "
                         "next reader sees a clean set")

    def test_refuses_against_a_partial_census(self):
        cfg = self.declare([self.loc("auth", "auth", role="authority"),
                            self.loc("off", "x", role="backup")], min_copies=1)
        cfg["locations"][1].update(via="server", server="no.such.host.invalid",
                                   root="/d")
        self.write_json("proj/datasets/ds/dataset.json", cfg)
        self.scan()
        with self.assertRaises(SystemExit) as cm:
            self.snap(allow_incomplete=1)
        self.assertEqual(cm.exception.code, 1)

    def test_identity_is_never_reused(self):
        self.scan()
        self.snap(layer="sfm")
        with self.assertRaises(SystemExit) as cm:
            self.snap(layer="sfm")
        self.assertEqual(cm.exception.code, 1,
                         "a frozen set that changed under an existing citation is "
                         "worse than no citation")

    def test_cite_as_names_the_snapshot_not_the_dataset(self):
        self.scan()
        self.snap(layer="sfm")
        snap = self.read_json("proj/datasets/ds/snapshots/v1/snapshot.json")
        self.assertEqual(snap["cite_as"], "datasets/ds@v1")
        lines = [json.loads(l) for l in
                 self.read("proj/datasets/ds/snapshots/v1/manifest.jsonl").splitlines()]
        self.assertEqual(lines[0]["_manifest"]["count"], 1)
        self.assertEqual(lines[1]["unit"], "260725/s000")


class ResolveIsAViewNotARecord(CensusCase):
    """data-line.md -> "Freeze: the boundary to the model lifecycle":
    and `datasets/<id>@<snapshot>` in `lineage.parents` is what makes data and
    models one graph. `resolve` is that boundary's crossing: a snapshot pins
    membership in dataset space, and the training side needs paths. The whole
    risk is that the resolved thing looks like the frozen thing — a machine's
    root baked into a record that layout.md declares machine-independent, or a
    subset handed over reading as the whole snapshot.
    """

    def setUp(self):
        super().setUp()
        self.unit("auth", "260725/s000", ["rgbd", "sfm"], done=True)
        self.unit("auth", "260725/s001", ["rgbd"], done=True)   # no sfm
        self.declare([self.loc("auth", "auth", role="authority"),
                      self.loc("box", "box", role="origin", has_layers=["rgbd"]),
                      self.loc("cold", "cold", role="backup")], min_copies=1)
        self.scan()
        census.cmd_snapshot(Namespace(
            project=self.path("proj"), dataset="ds", json=False, id="v1",
            layer=None, at=None, units_from=None, allow_incomplete=-1, fn=None))

    def resolve(self, **kw):
        args = Namespace(project=self.path("proj"), dataset="ds", json=True,
                         snapshot="v1", at="auth", layer=None,
                         allow_missing=-1, out=None, fn=None)
        for k, v in kw.items():
            setattr(args, k, v)
        buf = io.StringIO()
        with redirect_stdout(buf):
            census.cmd_resolve(args)
        return json.loads(buf.getvalue())

    def test_paths_are_openable_not_location_keys(self):
        """The join the training side cannot do: manifest × root × marker. A
        manifest alone carries location KEYS, so a consumer holding only it
        cannot open one file."""
        args = Namespace(project=self.path("proj"), dataset="ds", json=False,
                         snapshot="v1", at="auth", layer=["rgbd"],
                         allow_missing=-1, out=None, fn=None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            census.cmd_resolve(args)
        lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
        self.assertIn("_resolved", lines[0])
        for row in lines[1:]:
            self.assertTrue(os.path.exists(row["paths"]["rgbd"]),
                            f"{row['paths']['rgbd']} must be an openable path, "
                            f"not a location key")

    def test_partial_resolution_is_refused_not_silently_trimmed(self):
        """s001 has no sfm. Emitting one unit where the snapshot froze two hands
        training a subset that reads as the whole citation."""
        with self.assertRaises(SystemExit) as cm:
            self.resolve()
        self.assertEqual(cm.exception.code, 1,
                         "exit 1: the script worked and the answer is no")

    def test_allow_missing_binds_to_the_measured_count(self):
        """Same rule as --allow-incomplete: the number must be the one measured
        now, so a remembered count cannot wave through a set that has since
        changed."""
        with self.assertRaises(SystemExit):
            self.resolve(allow_missing=2)
        h = self.resolve(allow_missing=1)
        self.assertEqual((h["count"], h["excluded_missing"]), (1, 1))

    def test_excluded_and_unverified_counts_survive_into_the_header(self):
        """A resolve is the last record before the dataloader. If it drops the
        snapshot's unverified/unverifiable counts, the run's own description of
        its data is the first place they are gone."""
        h = self.resolve(allow_missing=1)
        for k in ("excluded_missing", "unverified_units", "unverifiable_units",
                  "paths_true_as_of", "snapshot_age_days"):
            self.assertIn(k, h, f"{k} must survive into the resolved header")

    def test_refuses_to_write_inside_the_snapshot_dir(self):
        """layout.md: dataset records are machine-independent on purpose. A
        resolved path names one machine's root; inside `snapshots/` it would
        travel with the citation and outlive the machine."""
        with self.assertRaises(SystemExit) as cm:
            self.resolve(layer=["rgbd"],
                         out=self.path("proj/datasets/ds/snapshots/v1/r.jsonl"))
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(
            self.path("proj/datasets/ds/snapshots/v1/r.jsonl")))

    def test_refuses_backup_as_a_compute_source(self):
        """dataset.json -> locations: `backup` is "written to, never read from
        for compute"."""
        with self.assertRaises(SystemExit) as cm:
            self.resolve(at="cold", layer=["rgbd"])
        self.assertEqual(cm.exception.code, 1)

    def test_undeclared_layer_at_a_location_is_not_missing_data(self):
        """`has_layers` is a design statement. Reporting sfm as absent at the
        capture box would describe a wrong request as a gap."""
        with self.assertRaises(SystemExit) as cm:
            self.resolve(at="box", layer=["sfm"])
        self.assertEqual(cm.exception.code, 1)

    def test_no_location_is_chosen_silently(self):
        """`--at` is required. Defaulting to the authority is how a run trains
        off a copy nobody meant it to read."""
        rc, _, err = run_script("data-check/census.py", "resolve",
                                "--project", self.path("proj"),
                                "--dataset", "ds", "--snapshot", "v1")
        self.assertEqual(rc, 2, "argparse exits 2 on a missing required argument")
        self.assertIn("--at", err)

    def test_remote_location_is_flagged_as_needing_path_mapping(self):
        """run-mechanics.md -> "Path Mapping (Cross-Machine Execution)": paths under a `server`
        location are on another machine. A consumer that cannot tell will open
        them locally and find nothing."""
        cfg = self.read_json("proj/datasets/ds/dataset.json")
        cfg["locations"][0].update(via="server", server="ipc")
        self.write_json("proj/datasets/ds/dataset.json", cfg)
        h = self.resolve(layer=["rgbd"])
        self.assertEqual(h["reachable"], "server:ipc")


class StatusTouchesNothing(CensusCase):
    """CLAUDE.md -> "On Conversation Start": the session-open check reads records.
    A verb that goes out and asks every machine cannot be what a session opens
    with — four ssh timeouts before the user's first sentence.
    """

    def test_status_reads_records_only(self):
        self.unit("auth", "260725/s000", ["rgbd"], done=True)
        self.declare([self.loc("auth", "auth", role="authority")])
        self.scan()

        called = []
        real = census.subprocess.run
        census.subprocess.run = lambda *a, **k: called.append(a) or real(*a, **k)
        try:
            census.cmd_status(Namespace(project=self.path("proj"), workspace=None,
                                        json=True, fn=None))
        finally:
            census.subprocess.run = real
        self.assertEqual(called, [], "status must not spawn a single process")

    def test_unparseable_timestamp_is_not_age_zero(self):
        """A census whose age cannot be computed is `null`, not fresh. Age is what
        decides whether to re-scan, and `0` reads as "scanned just now"."""
        self.unit("auth", "260725/s000", ["rgbd"], done=True)
        self.declare([self.loc("auth", "auth", role="authority")])
        c = self.scan()
        p = f"proj/datasets/ds/census/{c['census_id']}.json"
        rec = self.read_json(p)
        rec["scanned_at"] = "not a timestamp"
        self.write_json(p, rec)

        buf = io.StringIO()
        with redirect_stdout(buf):
            census.cmd_status(Namespace(project=self.path("proj"), workspace=None,
                                        json=True, fn=None))
        rows = json.loads(buf.getvalue())
        self.assertIsNone(rows[0]["age_days"],
                          "an uncomputable age must be null, never 0")



class TheCensusRecordsTheContractItWasTakenUnder(TempDirCase):
    """layout.md -> "Dataset identity and census records": `unit_glob`'s depth is
    load-bearing and getting it wrong is silent.

    A census that does not record the contract it was taken under cannot be
    compared with another one: editing the glob renames every unit at once, so
    two censuses differ by 1240 units with every byte still on disk. Recording
    it is what makes a later reader able to tell a data change from a counting
    change.
    """

    def test_scan_records_taken_under(self):
        project = self.path("proj")
        root = self.path("store")
        os.makedirs(os.path.join(root, "260731", "s000", "rgb"), exist_ok=True)
        self.write_json("proj/datasets/ds/dataset.json", {
            "dataset_id": "ds", "project": project,
            "identity": {"unit_glob": "*/*", "unit_label": "scene", "exclude": []},
            "layers": [{"label": "rgb", "marker": "rgb", "kind": "source",
                        "produced_by": None}],
            "completeness": {"marker": "DONE", "partial_marker_field": None},
            "locations": [{"key": "auth", "role": "authority", "via": "local",
                           "root": root, "has_layers": None}],
            "replication": {"min_source_copies": 1}, "consumers": []})
        rc, _out, err = run_script("data-check/census.py", "scan", "--project",
                                   project, "--dataset", "ds", "--json")
        self.assertEqual(rc, 0, err)
        cdir = os.path.join(project, "datasets", "ds", "census")
        rec = json.load(open(os.path.join(cdir, sorted(os.listdir(cdir))[-1])))
        self.assertIn("taken_under", rec)
        self.assertEqual(rec["taken_under"]["identity"]["unit_glob"], "*/*")
        self.assertEqual(rec["taken_under"]["completeness"]["marker"], "DONE")

if __name__ == "__main__":
    unittest.main()
