"""A reading of production can never be retaken, so it is right the first time or not at all.

Every other observation on the data line is repeatable. A census re-scanned next
month gives a different answer and both are true of their date. A window re-read
next month gives nothing: the traffic rolled off and the model that answered has
been replaced. That makes a reading the bar in CLAUDE.md -> "Contracts" in its
strictest form — a record written now, read later, by someone who cannot go back
and check.

The checks below are grouped by what would go wrong if the code drifted:

  * a window that is not an interval — a naive timestamp nothing can order, or an
    open end that makes the draw unreproducible (CLAUDE.md -> "Skills &
    Dependencies": the reading's window is closed and offset-bearing)
  * enumerated read as population — the rule that turns a logging outage into a
    quiet day (CLAUDE.md -> "Never silently": never report data you could not
    look at, and a count from a partial view is a lower bound)
  * a policy that could be biased — a non-uniform draw makes drift measure the
    filter instead of the world, so the dangerous case must be inexpressible
  * a denominator asserted rather than cited — `/data-collect --cite-window`
    recording a bias nobody measured
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import (TempDirCase, requires_rsync_accepting_native_paths,
                     run_script)

SCRIPT = "data-online-sample/online.py"
COLLECT = "data-collect/collect.py"


def iso(dt):
    return dt.isoformat(timespec="seconds")


class OnlineCase(TempDirCase):
    """A declared dataset plus a fake production tree on the local filesystem.

    `kind: local` is used throughout on purpose: these checks are about the
    record, not about ssh, and a local listing exercises the same three-outcome
    path without needing a second machine.
    """

    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        os.makedirs(self.project, exist_ok=True)
        self.write_json("proj/project.json", {"name": "det"})
        self.write_json("proj/datasets/boxes/dataset.json", {
            "dataset_id": "boxes",
            "identity": {"unit_glob": "*/", "unit_is": "directory"},
            "layers": [{"label": "rgb", "kind": "source"}],
            "locations": [], "completeness": None,
        })
        # Two days of "production", one unit each.
        self.day0 = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for d, units in (("2026/07/30", ["a1", "a2", "a3", "a4"]),
                         ("2026/07/31", ["b1", "b2"])):
            for u in units:
                os.makedirs(self.path("prod", d, u), exist_ok=True)

    def declare(self, **over):
        args = {"resource": self.path("prod"), "kind": "local",
                "partition": "strftime", "pattern": self.path("prod") + "/%Y/%m/%d"}
        args.update(over)
        argv = ["declare", "--project", self.project, "--dataset", "boxes",
                "--resource", args["resource"], "--kind", args["kind"],
                "--partition", args["partition"]]
        if args.get("pattern"):
            argv += ["--pattern", args["pattern"]]
        if args.get("replace"):
            argv += ["--replace"]
        return run_script(SCRIPT, *argv)

    def sample(self, *extra, frm=None, to=None):
        return run_script(SCRIPT, "sample", "--project", self.project,
                          "--dataset", "boxes",
                          "--from", frm or iso(self.day0),
                          "--to", to or iso(self.day0 + timedelta(days=2)),
                          *extra)


class WindowIsAnInterval(OnlineCase):
    """CLAUDE.md -> "Skills & Dependencies": a reading's window is a closed
    interval whose ends carry a UTC offset. Both halves are load-bearing and
    both fail silently if dropped.
    """

    def test_a_naive_timestamp_is_refused(self):
        self.declare()
        code, out, _ = self.sample(frm="2026-07-30T00:00:00")
        self.assertEqual(code, 1, "a bare local timestamp cannot be ordered "
                                 "against a reading taken in another zone")
        self.assertIn("offset", json.dumps(out))

    def test_a_window_ending_in_the_future_is_refused(self):
        self.declare()
        ahead = datetime.now(timezone.utc) + timedelta(days=1)
        code, out, _ = self.sample(to=iso(ahead))
        self.assertEqual(code, 1, "an open window is a moving target, so the "
                                 "enumeration and the draw are not reproducible")

    def test_the_same_window_draws_the_same_units(self):
        """The seed defaults to the window itself. A draw nobody can reproduce
        makes the record assert a sample no one can check."""
        self.declare()
        c1, o1, _ = self.sample("--n", "2")
        c2, o2, _ = self.sample("--n", "2")
        self.assertEqual((c1, c2), (0, 0))
        w1 = self.read_json(f"proj/datasets/boxes/online/{o1['window_id']}.json")
        w2 = self.read_json(f"proj/datasets/boxes/online/{o2['window_id']}.json")
        self.assertEqual(w1["units"], w2["units"])
        self.assertEqual(w1["draw"]["enumeration_digest"],
                         w2["draw"]["enumeration_digest"])

    def test_the_draw_is_stable_when_a_unit_appears(self):
        """Hash-ordered, not shuffled: a unit's membership must not change
        because some other unit turned up."""
        self.declare()
        _, first, _ = self.sample("--n", "2")
        kept = set(self.read_json(
            f"proj/datasets/boxes/online/{first['window_id']}.json")["units"])
        os.makedirs(self.path("prod", "2026/07/30", "a9"), exist_ok=True)
        _, second, _ = self.sample("--n", "3")
        now = set(self.read_json(
            f"proj/datasets/boxes/online/{second['window_id']}.json")["units"])
        self.assertTrue(kept <= now,
                        f"membership churned: {kept} not within {now}")


class PopulationIsNotEnumerated(OnlineCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at; a
    count from a partial view is a lower bound and must be said as one.

    Request logging is itself sampled and rotated, so what a listing finds is
    what reached the store, not what happened. Collapsing the two is how a
    logging outage reads as a quiet day.
    """

    def test_unknown_population_is_null_and_rates_are_a_lower_bound(self):
        self.declare()
        code, out, _ = self.sample("--n", "2")
        self.assertEqual(code, 0, out)
        self.assertIsNone(out["population"])
        self.assertEqual(out["population_basis"], "enumeration_only")
        self.assertEqual(out["rates_are"], "lower bound")
        w = self.read_json(f"proj/datasets/boxes/online/{out['window_id']}.json")
        self.assertIsNone(w["sample_rate"],
                          "a rate against an unknown denominator is not a rate")

    def test_enumerated_is_never_promoted_to_population(self):
        self.declare()
        _, out, _ = self.sample("--n", "2")
        self.assertEqual(out["enumerated"], 6)
        self.assertIsNone(out["population"],
                          "6 units reached the store; how many events happened "
                          "is a different fact and nothing here measured it")

    def test_a_declared_population_makes_rates_exact(self):
        self.declare()
        code, out, _ = self.sample("--n", "2", "--population", "1000",
                                   "--population-source", "metrics backend")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["population_basis"], "declared")
        self.assertEqual(out["rates_are"], "exact")
        w = self.read_json(f"proj/datasets/boxes/online/{out['window_id']}.json")
        self.assertAlmostEqual(w["sample_rate"], 2 / 1000)

    def test_a_population_below_the_enumeration_is_refused(self):
        self.declare()
        code, out, _ = self.sample("--n", "2", "--population", "3")
        self.assertEqual(code, 1, "the declared total cannot be under the units "
                                 "actually seen")

    def test_an_unreachable_prefix_is_not_a_quiet_window(self):
        """Three outcomes, kept apart. Only 'it answered and holds nothing' means
        the window was quiet."""
        self.declare(pattern="/nonexistent/root/%Y/%m/%d")
        code, out, _ = self.sample("--n", "2")
        # A missing prefix answered — the shell ran and said so — which is
        # `missing`, not `unreachable`, and leaves the reading complete.
        self.assertEqual(code, 0, out)
        w = self.read_json(f"proj/datasets/boxes/online/{out['window_id']}.json")
        self.assertTrue(w["missing_prefixes"], "a path that is not there is a "
                                              "fact, and it must be recorded as "
                                              "that fact")
        self.assertEqual(w["enumerated"], 0)

    def test_an_external_enumeration_says_it_was_external(self):
        """With --units-from nothing here looked, so reachability is somebody
        else's claim and the record must not imply otherwise."""
        self.declare()
        self.write("units.txt", "x1\tp/x1\nx2\tp/x2\n")
        code, out, _ = self.sample("--n", "2", "--units-from", self.path("units.txt"))
        self.assertEqual(code, 0, out)
        w = self.read_json(f"proj/datasets/boxes/online/{out['window_id']}.json")
        self.assertIn("external", w["source"]["enumerated_by"])
        self.assertEqual(sorted(w["units"]), ["x1", "x2"])


class PolicyCannotBeBiased(OnlineCase):
    """CLAUDE.md -> "Skills & Dependencies": this skill's draw is uniform always,
    because a biased reading measures the filter instead of the world and makes a
    drift verdict undefined. The dangerous case must be INEXPRESSIBLE, not merely
    discouraged — the biased pull lives in /data-collect.
    """

    def test_there_is_no_policy_flag(self):
        self.declare()
        code, out, _ = self.sample("--policy", "confidence_band")
        self.assertEqual(code, 2, "a policy flag would make the wrong reading "
                                 "one keystroke away")

    def test_every_reading_records_the_policy_it_used(self):
        self.declare()
        _, out, _ = self.sample("--n", "2")
        self.assertEqual(out["policy"], "uniform")

    def test_the_contract_it_was_taken_under_is_recorded(self):
        """Same reason census.py records it: a later comparison cannot otherwise
        tell a change in production from a change in how production was counted."""
        self.declare()
        _, out, _ = self.sample("--n", "2")
        w = self.read_json(f"proj/datasets/boxes/online/{out['window_id']}.json")
        self.assertEqual(w["taken_under"]["partition"], "strftime")
        self.assertEqual(w["identity"]["unit_glob"], "*/",
                         "the online and offline sides must count the same unit")

    def test_replacing_the_contract_is_refused_by_default(self):
        self.declare()
        code, out, _ = self.declare(partition="flat", pattern=self.path("prod"))
        self.assertEqual(code, 1, "readings already on record were taken under "
                                 "the old contract")
        code, _, _ = self.declare(partition="flat", pattern=self.path("prod"),
                                  replace=True)
        self.assertEqual(code, 0)


class DenominatorIsCitedNotAsserted(OnlineCase):
    """CLAUDE.md -> "Skills & Dependencies": /data-collect cites a reading as its
    denominator, because a biased pull's bias is invisible once the frames are on
    disk — they look exactly like data somebody captured.
    """

    def collect(self, *extra):
        src = self.path("src")
        os.makedirs(src, exist_ok=True)
        self.write("src/f.txt", "x")
        return run_script(COLLECT, "pull", "--project", self.project,
                          "--from", "local", "--at", src,
                          "--into", self.path("landing"), *extra)

    def test_a_window_that_is_not_on_record_is_refused(self):
        code, out, _ = self.collect("--cite-window", "boxes/window_nope")
        self.assertEqual(code, 1, "recording a citation with no reading behind it "
                                 "makes an unmeasured bias read as a measured one")

    def test_a_malformed_citation_breaks_rather_than_passing(self):
        code, out, _ = self.collect("--cite-window", "justadataset")
        self.assertEqual(code, 2)

    def test_a_cited_reading_carries_its_own_honesty_through(self):
        # The only check here that needs the transfer to actually SUCCEED; the
        # others assert on refusals, which rsync never gets to see.
        requires_rsync_accepting_native_paths(self.tmp)
        self.declare()
        _, s, _ = self.sample("--n", "2")
        code, out, _ = self.collect("--cite-window", f"boxes/{s['window_id']}")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["denominator"]["population_basis"],
                         "enumeration_only")
        self.assertEqual(out["denominator"]["rates_are"], "lower bound")
        self.assertIn("LOWER BOUND", out["note_denominator"],
                      "a pull quoting a fraction of production must inherit the "
                      "reading's own limits")


if __name__ == "__main__":
    unittest.main()
