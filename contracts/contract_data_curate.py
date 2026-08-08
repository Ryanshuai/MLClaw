"""A derived dataset that reads as a captured one is the failure with no undo.

Every other record on the data line describes something a scan can re-observe.
A derivation cannot be: once `boxes_v2` is on disk it looks exactly like data
somebody captured, and that it is a 30% sample with the blurry scenes dropped
lives nowhere but in a memory. That is the bar in CLAUDE.md "Contracts" — a
record written now and read later by someone who can no longer verify it.

The checks below are grouped by what would go wrong if the code drifted: a
derivation asserted rather than checked, an in-place transform rewriting bytes
that frozen snapshots still name, and a trace that ends quietly on a link it
could not follow.
"""
import json
import os
import unittest

from helpers import TempDirCase, requires_symlinks, run_script

SCRIPT = "data-curate/curate.py"
TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "lifecycle", "data", "dataset.json")


class CurateCase(TempDirCase):
    """Builds the record tree by hand: these checks are about what `register`
    will and will not write, so the run record and the snapshot have to be
    settable to shapes a real pipeline takes hours to reach."""

    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        os.makedirs(self.project, exist_ok=True)
        self.src = "boxes"
        self.declare(self.src, root=self.path("store", "boxes"))
        self.scan(self.src)
        self.freeze(self.src, "v1")

    def scan(self, dataset):
        """A clean census, so the curate gate passes. Its absence is a correct
        refusal — you cannot derive from a dataset nothing has looked at — which
        is why every check here has to supply one rather than route around it."""
        cid = "census_20260801_000000"
        self.write_json(f"proj/datasets/{dataset}/census/{cid}.json", {
            "census_id": cid, "dataset": dataset, "project": self.project,
            "scanned_at": "2026-08-01T00:00:00+00:00", "complete": True,
            "unreachable": [], "root_missing": [], "locations": [],
            "units": {"260731/s000": {"at": ["auth"], "layers": {"rgb": ["auth"]},
                                      "completeness": "complete"}},
            "verdicts": {}, "totals": {"units": 1, "gap": 0, "drift": 0,
                                       "unreplicated": 0, "unarchived": 0,
                                       "incomplete": 0, "partial": 0,
                                       "unarchived_checked": True}})

    def declare(self, dataset, *, root, derived_from=None):
        cfg = {
            "dataset_id": dataset, "project": self.project,
            "identity": {"unit_glob": "*/*", "unit_label": "scene", "exclude": []},
            "layers": [{"label": "rgb", "marker": "rgb", "kind": "source",
                        "produced_by": None}],
            "completeness": {"marker": "DONE", "partial_marker_field": None},
            "locations": [{"key": "auth", "role": "authority", "via": "local",
                           "root": root, "has_layers": None}],
            "replication": {"min_source_copies": 2}, "consumers": [],
            "derived_from": derived_from,
        }
        self.write_json(f"proj/datasets/{dataset}/dataset.json", cfg)
        return cfg

    def freeze(self, dataset, sid):
        self.write_json(f"proj/datasets/{dataset}/snapshots/{sid}/snapshot.json", {
            "snapshot_id": sid, "cite_as": f"datasets/{dataset}@{sid}",
            "dataset": dataset, "project": self.project,
            "frozen_at": "2026-08-01T00:00:00+00:00", "from_census": "census_x",
            "selection": {"count": 10}, "unverified_units": []})

    def make_run(self, run_id, *, status="completed", parents=(), stage="curate"):
        self.write_json(f"proj/stages/{stage}/runs/{run_id}/run.json", {
            "run_id": run_id, "status": status,
            "lineage": {"parents": list(parents)}})
        return f"{stage}/{run_id}"

    def plan(self, *extra, to="boxes_v2", frm="datasets/boxes@v1", op="sample",
             into=None):
        return run_script(SCRIPT, "plan", "--project", self.project,
                          "--from", frm, "--to", to, "--op", op,
                          "--into", into or self.path("out", to), *extra)

    def good_plan(self, to="boxes_v2"):
        rc, out, err = self.plan(to=to)
        self.assertEqual(rc, 0, f"plan rc={rc} err={err} out={out}")
        return out["_path"]


class ADerivationIsCheckedAgainstTheRunOrMarkedClaimed(CurateCase):
    """CLAUDE.md -> "Never silently": never let somebody's word become a checked
    fact. `/ask-human` draws this line for answers; here the instrument is a run
    record instead of a person, and the failure is the same — "this came from
    boxes@v1" asserted by whoever typed the command is indistinguishable, once
    written, from one a run actually recorded.
    """

    def test_a_run_that_cites_the_parents_produces_provenance_run(self):
        p = self.good_plan()
        run = self.make_run("run_1", parents=["datasets/boxes@v1"])
        rc, out, err = run_script(SCRIPT, "register", "--project", self.project,
                                  "--plan", p, "--run", run)
        self.assertEqual(rc, 0, err)
        df = self.read_json("proj/datasets/boxes_v2/dataset.json")["derived_from"]
        self.assertEqual(df["provenance"], "run")
        self.assertEqual(df["parents"], ["datasets/boxes@v1"])
        self.assertEqual(df["run"], run)

    def test_a_run_that_does_not_cite_the_parents_is_refused(self):
        p = self.good_plan()
        run = self.make_run("run_2", parents=["datasets/other@v9"])
        rc, out, _ = run_script(SCRIPT, "register", "--project", self.project,
                                "--plan", p, "--run", run)
        self.assertEqual(rc, 1)
        self.assertIn("datasets/boxes@v1", out["missing"])
        self.assertFalse(os.path.exists(
            self.path("proj/datasets/boxes_v2/dataset.json")))

    def test_an_unfinished_run_is_refused(self):
        """The output tree exists either way, which is the whole problem: a
        crashed conversion is indistinguishable from a whole one the moment it
        is registered as a dataset."""
        p = self.good_plan()
        for status in ("running", "failed", "crashed"):
            run = self.make_run(f"run_{status}", status=status,
                                parents=["datasets/boxes@v1"])
            rc, _, _ = run_script(SCRIPT, "register", "--project", self.project,
                                  "--plan", p, "--run", run)
            self.assertEqual(rc, 1, f"{status} should be refused")

    def test_claimed_is_recorded_as_claimed_and_needs_a_reason(self):
        """An unverified derivation is a legitimate record — refusing it outright
        pushes the work outside the tool. What it may never do is wear the same
        label as a checked one."""
        p = self.good_plan()
        rc, _, _ = run_script(SCRIPT, "register", "--project", self.project,
                              "--plan", p, "--claimed")
        self.assertEqual(rc, 1, "--claimed without --because must be refused")

        rc, _, err = run_script(SCRIPT, "register", "--project", self.project,
                                "--plan", p, "--claimed",
                                "--because", "converted with a one-off script")
        self.assertEqual(rc, 0, err)
        df = self.read_json("proj/datasets/boxes_v2/dataset.json")["derived_from"]
        self.assertEqual(df["provenance"], "claimed")
        self.assertIsNone(df["run"])
        self.assertTrue(df["because"])

    def test_register_never_writes_a_null_derived_from(self):
        """`null` means captured. A derived dataset carrying it would be a lie
        told by the one field that exists to prevent exactly that."""
        p = self.good_plan()
        run = self.make_run("run_3", parents=["datasets/boxes@v1"])
        run_script(SCRIPT, "register", "--project", self.project,
                   "--plan", p, "--run", run)
        cfg = self.read_json("proj/datasets/boxes_v2/dataset.json")
        self.assertIsNotNone(cfg["derived_from"])

    def test_locations_are_not_inherited_from_the_parent(self):
        """The output is somewhere new by construction. Copying the parent's
        roots would declare the derived data to be on machines that have never
        held it — and the census would then report every unit as a GAP there."""
        p = self.good_plan()
        run = self.make_run("run_4", parents=["datasets/boxes@v1"])
        run_script(SCRIPT, "register", "--project", self.project,
                   "--plan", p, "--run", run)
        cfg = self.read_json("proj/datasets/boxes_v2/dataset.json")
        roots = [l["root"] for l in cfg["locations"]]
        self.assertNotIn(self.path("store", "boxes"), roots)
        # the data-describing half IS inherited
        self.assertEqual(cfg["identity"]["unit_glob"], "*/*")


class TheOutputNeverLandsInsideItsOwnInput(CurateCase):
    """data-line.md -> "Curate: a derivation cannot be re-observed": an in-place
    transform rewrites bytes that frozen
    snapshots still name, and every one of those citations goes on resolving.

    This is the only curate failure with no undo, so it is checked in both
    directions and through realpath — a symlink into the source tree is the
    same accident wearing different clothes.
    """

    def test_output_inside_a_source_location_is_refused(self):
        rc, out, _ = self.plan(into=self.path("store", "boxes", "v2"))
        self.assertEqual(rc, 1)
        self.assertIn("overlap", out["refused"])

    def test_output_equal_to_a_source_location_is_refused(self):
        rc, _, _ = self.plan(into=self.path("store", "boxes"))
        self.assertEqual(rc, 1)

    def test_a_source_location_inside_the_output_is_refused(self):
        rc, _, _ = self.plan(into=self.path("store"))
        self.assertEqual(rc, 1)

    def test_a_symlink_into_the_source_does_not_get_past_it(self):
        os.makedirs(self.path("store", "boxes"), exist_ok=True)
        link = self.path("sneaky")
        requires_symlinks(self.path("store", "boxes"), link)
        rc, _, _ = self.plan(into=os.path.join(link, "v2"))
        self.assertEqual(rc, 1)

    def test_a_disjoint_output_is_allowed(self):
        rc, _, err = self.plan(into=self.path("elsewhere", "boxes_v2"))
        self.assertEqual(rc, 0, err)


class AParentIsAFrozenCitationAndAnIdIsNeverReused(CurateCase):
    """layout.md -> "Dataset identity and census records": a run cites
    `datasets/{id}@{snapshot_id}`, never the dataset id alone, because a dataset
    grows and a citation that cannot say which afternoon is not lineage. A
    derivation edge is the same edge pointing the other way.
    """

    def test_a_bare_dataset_id_is_not_a_parent(self):
        rc, out, _ = self.plan(frm="boxes")
        self.assertEqual(rc, 2, "a malformed citation is bad input, not a verdict")

    def test_a_snapshot_that_was_never_frozen_is_refused(self):
        rc, out, _ = self.plan(frm="datasets/boxes@never")
        self.assertEqual(rc, 1)
        self.assertIn("snapshot", out["refused"])

    def test_planning_onto_an_existing_dataset_id_is_refused(self):
        self.declare("taken", root=self.path("store", "taken"))
        rc, out, _ = self.plan(to="taken")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", out["refused"])

    def test_registering_twice_does_not_silently_replace_the_record(self):
        p = self.good_plan()
        run = self.make_run("run_5", parents=["datasets/boxes@v1"])
        args = ("register", "--project", self.project, "--plan", p, "--run", run)
        self.assertEqual(run_script(SCRIPT, *args)[0], 0)
        self.assertEqual(run_script(SCRIPT, *args)[0], 1)
        # retrying a register that went wrong is legitimate...
        self.assertEqual(run_script(SCRIPT, *args, "--re-register")[0], 0)

    def test_an_id_something_has_already_observed_cannot_be_re_registered(self):
        """Once a census or a snapshot exists, the identity has left this
        command: replacing its contract makes those records describe something
        else. `--re-register` is for undoing a bad register, not for versioning.
        """
        p = self.good_plan()
        run = self.make_run("run_6", parents=["datasets/boxes@v1"])
        args = ("register", "--project", self.project, "--plan", p, "--run", run)
        self.assertEqual(run_script(SCRIPT, *args)[0], 0)
        self.scan("boxes_v2")
        rc, out, _ = run_script(SCRIPT, *args, "--re-register")
        self.assertEqual(rc, 1)
        self.assertIn("census", out["refused"])


class TheChainNeverEndsQuietly(CurateCase):
    """run-mechanics.md -> "Record integrity": extraction failure and absence are
    different facts and must not become the same value.

    A trace is read by someone deciding whether a training set is what they
    think it is. Stopping at a parent this project has no record of, and
    printing the result as a chain, reports a derived dataset as a captured
    root — the strongest possible version of that bug.
    """

    def chain(self, dataset):
        rc, out, err = run_script(SCRIPT, "trace", "--project", self.project,
                                  "--dataset", dataset)
        self.assertEqual(rc, 0, err)
        return out

    def test_a_captured_root_is_reported_as_captured(self):
        out = self.chain(self.src)
        self.assertEqual(out["roots"], [self.src])
        self.assertTrue(out["trustworthy"])

    def test_a_dataset_that_is_not_here_is_bad_input_not_a_finding(self):
        """At the root, `unknown` would dress a typo up as a gap in the
        records — and `trustworthy: false` would then read as a statement about
        data that was never involved."""
        rc, _, _ = run_script(SCRIPT, "trace", "--project", self.project,
                              "--dataset", "no_such_dataset")
        self.assertEqual(rc, 2)

    def test_an_unresolvable_parent_is_unknown_not_a_root(self):
        self.declare("orphan", root=self.path("store", "orphan"), derived_from={
            "provenance": "run", "parents": ["datasets/vanished@v1"],
            "op": "convert", "run": "curate/run_x"})
        out = self.chain("orphan")
        self.assertEqual(out["unknown_links"], ["vanished"])
        self.assertEqual(out["roots"], [])
        self.assertFalse(out["trustworthy"])

    def test_a_claimed_link_makes_the_whole_chain_untrustworthy(self):
        self.declare("v2", root=self.path("store", "v2"), derived_from={
            "provenance": "claimed", "parents": [f"datasets/{self.src}@v1"],
            "op": "convert", "run": None, "because": "one-off script"})
        out = self.chain("v2")
        self.assertEqual(out["claimed_links"], ["v2"])
        self.assertEqual(out["roots"], [self.src])
        self.assertFalse(out["trustworthy"])

    def test_a_cycle_is_refused_not_traversed(self):
        for a, b in (("x", "y"), ("y", "x")):
            self.declare(a, root=self.path("store", a), derived_from={
                "provenance": "run", "parents": [f"datasets/{b}@v1"],
                "op": "convert", "run": "curate/run_z"})
        rc, out, _ = run_script(SCRIPT, "trace", "--project", self.project,
                                "--dataset", "x")
        self.assertEqual(rc, 1)
        self.assertIn("cycle", out["refused"])


class TemplateMatchesWhatIsWritten(unittest.TestCase):
    """CLAUDE.md -> "Key design principles": JSON configs are the source of
    truth, fixed keys. A template missing a key the script writes sends a manual
    fallback to produce a record the readers cannot use — and the fallback is
    exactly when the template is the only guidance there is.
    """

    def test_dataset_template_declares_derived_from(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            tpl = json.load(fh)
        self.assertIn("derived_from", tpl)
        self.assertIsNone(tpl["derived_from"],
                          "null is the captured case and must be the template's value")

    def test_the_template_says_what_null_means(self):
        """A blank-looking default with no comment is how `null` starts meaning
        'nobody filled this in' instead of 'this data was captured'."""
        with open(TEMPLATE, encoding="utf-8") as fh:
            tpl = json.load(fh)
        note = tpl.get("_comment_derived_from", "")
        self.assertIn("captured", note)
        self.assertIn("claimed", note)


if __name__ == "__main__":
    unittest.main()
