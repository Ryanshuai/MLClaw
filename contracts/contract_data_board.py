"""A page of tidy counts is what an inventory looks like.

The other report skills have no contract, because a rendering is not a record.
This one gets a narrow exception for a single property: the board is the most
dangerous surface in MLClaw for CLAUDE.md "Never report data you could not look
at". A partial census — some machine did not answer — produces counts that are
lower bounds, and a lower bound laid out in a neat table, on a wall, next to a
timestamp, is read as a complete count by everyone who walks past it.

Nothing else here is checked. Layout, colour and wording are free to change; the
banner and the no-recompute rule are not.
"""
import json
import os
import re
import unittest

from helpers import REPO_ROOT, TempDirCase, run_script

SCRIPT = "data-report/board.py"


class BoardCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        os.makedirs(self.project, exist_ok=True)

    def dataset(self, name="ds"):
        self.write_json(f"proj/datasets/{name}/dataset.json", {
            "dataset_id": name, "project": self.project,
            "identity": {"unit_glob": "*", "unit_label": "u", "exclude": []},
            "layers": [{"label": "rgb", "kind": "source", "marker": "rgb",
                        "produced_by": None}],
            "completeness": {"marker": "DONE", "partial_marker_field": None},
            "locations": [{"key": "auth", "role": "authority", "via": "local",
                           "root": self.path("auth")}],
            "replication": {"min_source_copies": 2}, "consumers": []})

    def census(self, name="ds", *, complete=True, units=40):
        self.write_json(f"proj/datasets/{name}/census/census_1.json", {
            "census_id": "census_1", "dataset": name, "project": self.project,
            "scanned_at": "2026-07-31T00:00:00+00:00", "complete": complete,
            "unreachable": [] if complete else ["rig"], "root_missing": [],
            "locations": [], "units": {}, "verdicts": {},
            "totals": {"units": units, "gap": 0, "drift": 0, "unreplicated": 0,
                       "unarchived": 0, "incomplete": 0, "partial": 0,
                       "unarchived_checked": True}})

    def render(self, *extra):
        out = self.path("board.html")
        rc, res, err = run_script(SCRIPT, "--project", self.project, "--out", out, *extra)
        self.assertEqual(rc, 0, f"render failed: {err or res}")
        with open(out, encoding="utf-8") as f:
            return f.read(), res

    def banner(self, page):
        """The banner element, not a substring of the document. The drill-down
        script legitimately carries the same warning text for the cells that
        need it, so a page-wide search can no longer tell 'warned' from 'has the
        word in it somewhere'."""
        m = re.search(r'<div class="banner">(.*?)</div>', page, re.S)
        return m.group(1) if m else None

    def drill(self, page):
        """The per-cell payload the panel renders from."""
        blob = page.split('<script id="cells" type="application/json">')[1].split("</script>")[0]
        return json.loads(blob.replace("<\\/", "</"))


class APartialCensusNeverRendersAsAnInventory(BoardCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at.

    A machine that did not answer makes every count a lower bound. The board has
    to say so above the numbers, because by the time a reader reaches the table
    the counts have already been believed.
    """

    def test_an_unreachable_location_produces_a_banner(self):
        self.dataset(); self.census(complete=False)
        page, res = self.render()
        self.assertIn("lower bound", self.banner(page) or "")
        self.assertIn("ds", res["partial_census"])

    def test_the_banner_comes_before_the_counts(self):
        """Order is the whole point: a warning under the table is a footnote."""
        self.dataset(); self.census(complete=False)
        page, _ = self.render()
        self.assertLess(page.index('<div class="banner">'), page.index("<table"),
                        "the lower-bound warning must precede the first table")

    def test_a_complete_census_gets_no_banner(self):
        """A warning that is always on is a warning nobody reads."""
        self.dataset(); self.census(complete=True)
        page, res = self.render()
        self.assertIsNone(self.banner(page))
        self.assertEqual(res["partial_census"], [])

    def test_a_partial_column_carries_the_warning_into_its_own_cell(self):
        """The banner is page-level; the grid is per-column. A cell whose census
        was partial has to say so when it is opened, or drilling into it reads
        as a clean reading of that day."""
        self.dataset(); self.census(complete=False)
        page, _ = self.render()
        cells = list(self.drill(page).values())
        self.assertTrue(cells, "the timeline produced no cells")
        self.assertTrue(all(c["counts_are_lower_bound"] for c in cells))

    def test_a_never_scanned_dataset_is_named_not_shown_as_empty(self):
        """Zero units and never-looked are different facts, and a dataset with
        no census would otherwise render as a tidy row of zeroes."""
        self.dataset()
        page, _ = self.render()
        self.assertIn("never been scanned", page)


class TheBoardComputesNothing(BoardCase):
    """data-line.md -> "How /data composes the line": `/data` is the only thing
    that knows a dataset's
    position. A second implementation of the phase rules is a second set of
    answers, and the one on the wall is the one people act on.
    """

    def src(self):
        board = os.path.join(REPO_ROOT, "lifecycle", "scripts", "data-report", "board.py")
        with open(board, encoding="utf-8") as f:
            return f.read()

    def test_the_renderer_does_not_reimplement_the_phase_rules(self):
        src = self.src()
        for marker in ("def _phase", "def assess", "TERMINAL_HANDOFF", "handoffs_for("):
            self.assertNotIn(marker, src,
                             f"{marker!r} suggests the board re-derives what phase.py owns")
        for verb in ('"phase"', '"history"'):
            self.assertIn(verb, src, f"the board must get {verb} from phase.py")

    def test_the_renderer_never_opens_a_record(self):
        """The sharper form of the same rule, and the one that survives the
        board learning to read more fields. `stale_against` used to be on the
        forbidden list; it stopped being evidence of recomputation the moment
        phase.py started returning it as a count to display. What cannot change
        is where the numbers come in: one pipe, from phase.py's stdout.
        """
        src = self.src()
        self.assertNotIn("json.load(", src,
                         "the board reads records from disk; everything must "
                         "arrive through phase.py")
        for rec in ('"datasets"', '"census"', '"snapshots"', '"handoffs"'):
            self.assertNotIn(f"os.path.join(project, {rec}", src,
                             f"the board builds a path into {rec}")

    def test_there_is_no_auto_refresh(self):
        """A board goes stale because the census is old, not because the page
        is. Re-rendering on a timer would redraw an eleven-day-old scan under a
        fresh timestamp, which is the most convincing way available to lie about
        data nobody has looked at. Staleness is drawn instead — faded cells with
        an age on them.
        """
        self.dataset(); self.census()
        page, _ = self.render()
        self.assertIsNone(re.search(r'http-equiv\s*=\s*["\']refresh', page, re.I))
        for api in ("setInterval", "setTimeout", "location.reload", "EventSource",
                    "WebSocket", "fetch("):
            self.assertNotIn(api, page, f"{api} makes this a dashboard, not a board")

    def test_the_page_is_self_contained(self):
        """It gets emailed, opened from file://, and put on a screen with no
        network. An external reference turns a board into a blank page."""
        self.dataset(); self.census()
        page, _ = self.render()
        for pat in (r'src\s*=\s*"https?:', r'href\s*=\s*"https?:', r'@import'):
            self.assertIsNone(re.search(pat, page), f"external reference: {pat}")


class ACarriedCellNeverPassesForAScan(BoardCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at.

    The x axis is the union of every dataset's censuses, so most columns belong
    to some other dataset's scan. Drawing a dataset's last known state solid in
    a column where nobody looked at it claims a scan that never ran — and the
    grid's whole value is that you can see, at a glance, where nobody has
    looked. That distinction is an identity test on the timestamp, never a
    tolerance: five hours of carry-forward is still carry-forward.
    """

    def two_datasets(self):
        for n in ("a", "b"):
            self.dataset(n)
        self.write_json("proj/datasets/a/census/census_20260710_090000.json", {
            "census_id": "census_20260710_090000", "dataset": "a",
            "project": self.project, "scanned_at": "2026-07-10T09:00:00+00:00",
            "complete": True, "unreachable": [], "root_missing": [], "locations": [],
            "units": {}, "verdicts": {},
            "totals": {"units": 7, "gap": 0, "drift": 0, "unreplicated": 0,
                       "unarchived": 0, "incomplete": 0, "partial": 0,
                       "unarchived_checked": True}})
        # b is scanned five hours later, on the same day
        self.write_json("proj/datasets/b/census/census_20260710_140000.json", {
            "census_id": "census_20260710_140000", "dataset": "b",
            "project": self.project, "scanned_at": "2026-07-10T14:00:00+00:00",
            "complete": True, "unreachable": [], "root_missing": [], "locations": [],
            "units": {}, "verdicts": {},
            "totals": {"units": 2, "gap": 0, "drift": 0, "unreplicated": 0,
                       "unarchived": 0, "incomplete": 0, "partial": 0,
                       "unarchived_checked": True}})

    def test_a_five_hour_carry_forward_is_still_carry_forward(self):
        self.two_datasets()
        page, res = self.render()
        self.assertEqual(res["columns"], 2)
        d = self.drill(page)
        self.assertFalse(d["a|0"]["_carried"], "a's own 09:00 scan")
        self.assertTrue(d["a|1"]["_carried"],
                        "a was not scanned at 14:00 — that is b's column")
        self.assertFalse(d["b|1"]["_carried"], "b's own 14:00 scan")

    def test_a_dataset_that_did_not_exist_yet_gets_no_cell(self):
        """Distinct from carry-forward and from an empty scan: before its first
        census there is nothing to carry, and a zero would be a reading."""
        self.two_datasets()
        page, _ = self.render()
        self.assertNotIn("b|0", self.drill(page))

    def test_the_rightmost_column_is_live_and_is_not_a_scan(self):
        """A snapshot cut from census N necessarily postdates N, so N's own
        replay correctly shows nothing frozen. Without a live column you would
        freeze a dataset and watch the board not move — and worse, the grid
        would silently disagree with the table under it with nothing saying
        which was which.
        """
        self.two_datasets()
        page, _ = self.render()
        d = self.drill(page)
        self.assertIn("a|2", d, "there must be a column past the two censuses")
        self.assertTrue(d["a|2"]["_live"])
        self.assertIsNone(d["a|2"]["replay"],
                          "the live column is an assessment, not a replay")
        self.assertFalse(d["a|1"]["_live"])
        self.assertIn(">now<", page, "the live column must be labelled as such")

    def test_the_column_count_is_reported_when_the_axis_is_capped(self):
        """A grid truncated to the last N columns looks exactly like a complete
        one. CLAUDE.md -> "Never silently", never report data you could not look at:
        a bounded view has to say what it left out."""
        self.two_datasets()
        _page, res = self.render("--last", "1")
        self.assertEqual(res["columns"], 1)
        self.assertEqual(res["column_limit"], 1)


if __name__ == "__main__":
    unittest.main()
