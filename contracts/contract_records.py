#!/usr/bin/env python3
"""`shared/_records.py` -- the primitive the record layer rests on, and the one
module with no check of its own.

CLAUDE.md -> "Contracts" states the admission bar: *a record written now and read
later by someone who can no longer verify it, or an irreversible action.* Every
script that writes such a record calls this file to do it -- 20+ importers for the
exit-code contract, the two clocks, the atomic write, and the scoped commit. It was
the largest uncovered module in the repo, and the round that hoisted three drifted
atomic writers into it is exactly the kind of change that needs one.

The properties below are the ones whose failure is SILENT. Nothing here checks that
`_records` is tidy; each class names the wrong record it stops being written.
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import timezone

from helpers import GitRepoCase, TempDirCase, load_script

rec = load_script("shared/_records.py")


class ThreeExitCodesAreThreeAnswers(unittest.TestCase):
    """CLAUDE.md -> "Script Integration": 0 worked · 1 worked and the answer is no ·
    2 the script broke.

    The distinction is the whole reason these are three functions and not one
    `die()`: a skill decides whether to FALL BACK by reading the exit code, so a
    refusal that exits 2 gets worked around by hand -- which means overriding the
    safety check that produced it -- and a crash that exits 1 gets reported to the
    user as a finding.
    """

    def _run(self, body):
        src = ("import sys, os\n"
               "sys.path.insert(0, %r)\n"
               "from _records import emit, refuse, broke\n" % os.path.join(
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "scripts", "shared") + body)
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", src],
                           capture_output=True, text=True, encoding="utf-8")
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {"_stdout": p.stdout, "_stderr": p.stderr}

    def test_emit_is_exit_0_and_carries_neither_marker(self):
        rc, out = self._run('emit({"ok": 1})')
        self.assertEqual(rc, 0)
        self.assertNotIn("refused", out)
        self.assertNotIn("error", out)

    def test_refuse_is_exit_1_and_says_refused(self):
        rc, out = self._run('refuse("no", fix="do x")')
        self.assertEqual(rc, 1)
        self.assertEqual(out["refused"], "no")
        self.assertEqual(out["fix"], "do x")
        self.assertNotIn("error", out,
                         "a refusal that also says `error` reads as a crash, and the "
                         "skill falls back and redoes the work by hand")

    def test_broke_is_exit_2_and_says_error(self):
        rc, out = self._run('broke("crashed", why="detail")')
        self.assertEqual(rc, 2)
        self.assertEqual(out["error"], "crashed")
        self.assertEqual(out["why"], "detail")
        self.assertNotIn("refused", out)

    def test_all_three_are_utf8_not_escaped(self):
        """The money ledger was found written as `GPU \\u673a\\u623f`. A record a
        person has to read is not machine-only output."""
        rc, out = self._run('refuse("机房 ‼️")')
        self.assertEqual(rc, 1)
        self.assertEqual(out["refused"], "机房 ‼️")


class TwoClocksAndPickingTheWrongOneIsARecordBug(unittest.TestCase):
    """`_records.py`'s own comment: there are deliberately TWO of each, and picking
    the wrong one is a record-integrity bug rather than a style slip.

      parse_ts   for ORDERING. A naive string is None -- it cannot be ordered
                 against another machine's clock, and guessing is how a stale
                 record passes for a fresh one.
      parse_iso  for DURATIONS. A naive string is read as local WITH A FLAG saying
                 so, because refusing outright would report every pre-offset run as
                 durationless.

    Nothing checked that they still differ, and collapsing them is a one-line edit
    that no other test in the repo would notice.
    """

    def test_parse_ts_refuses_a_naive_string(self):
        self.assertIsNone(rec.parse_ts("2026-08-18T10:00:00"))

    def test_parse_ts_accepts_an_aware_one(self):
        dt = rec.parse_ts("2026-08-18T10:00:00+00:00")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)

    def test_parse_ts_refuses_junk_rather_than_raising(self):
        for bad in ("", None, "yesterday", 17, []):
            self.assertIsNone(rec.parse_ts(bad), bad)

    def test_parse_iso_assumes_local_and_says_it_did(self):
        dt, assumed = rec.parse_iso("2026-08-18T10:00:00")
        self.assertTrue(assumed, "an assumption a caller cannot see is not an "
                                 "assumption, it is a claim")
        self.assertIsNotNone(dt.tzinfo, "the return is always aware")

    def test_parse_iso_does_not_flag_an_aware_one(self):
        _dt, assumed = rec.parse_iso("2026-08-18T10:00:00+02:00")
        self.assertFalse(assumed)

    def test_parse_iso_reads_the_z_suffix(self):
        dt, assumed = rec.parse_iso("2026-08-18T10:00:00Z")
        self.assertFalse(assumed)
        self.assertEqual(dt.utcoffset(), timezone.utc.utcoffset(None))

    def test_the_two_disagree_about_a_naive_string_on_purpose(self):
        """The single assertion that fails if anyone unifies them."""
        naive = "2026-08-18T10:00:00"
        self.assertIsNone(rec.parse_ts(naive))
        self.assertIsNotNone(rec.parse_iso(naive)[0])

    def test_age_days_is_none_when_the_order_is_unknown(self):
        self.assertIsNone(rec.age_days("2026-08-18T10:00:00"))
        self.assertIsNone(rec.age_days(None))

    def test_now_utc_is_orderable_and_now_iso_is_subtractable(self):
        self.assertIsNotNone(rec.parse_ts(rec.now_utc()))
        self.assertFalse(rec.parse_iso(rec.now_iso())[1])

    def test_id_stamp_is_an_identifier_not_a_timestamp(self):
        s = rec.id_stamp()
        self.assertEqual(len(s), 15)
        self.assertNotIn(":", s, "an id goes in a path; a colon is not portable there")


class AWriteThatCrashesLeavesTheOldRecord(TempDirCase):
    """CLAUDE.md -> "Never silently" and run-mechanics.md "Record integrity". A
    torn record is worse than a missing one: it is unreadable where a valid one
    used to be, and the thing that read it last has already acted on it."""

    def test_the_temp_file_does_not_survive_a_successful_write(self):
        p = self.path("r.json")
        rec.atomic_write_json(p, {"a": 1})
        self.assertEqual(os.listdir(self.tmp), ["r.json"])

    def test_a_crash_mid_write_leaves_the_previous_record_intact(self):
        p = self.path("r.json")
        rec.atomic_write_json(p, {"good": True})

        class Unserialisable:
            pass
        with self.assertRaises(TypeError):
            rec.atomic_write_json(p, {"bad": Unserialisable()})
        self.assertEqual(json.load(open(p, encoding="utf-8")), {"good": True},
                         "os.replace is what makes this true; a direct open(w) would "
                         "have truncated the record before failing")

    def test_the_temp_name_carries_the_pid(self):
        """Two processes writing the same record would otherwise share one `.tmp`
        and tear it -- precisely the state this function exists to prevent."""
        import inspect
        src = inspect.getsource(rec.atomic_write_json)
        self.assertIn("os.getpid()", src)

    def test_tmp_stays_last_so_every_suffix_reader_still_skips_it(self):
        """The cross-module invariant the docstring claims, checked rather than
        asserted in prose: `_dataset_paths.census_paths` admits on `.json`, so a
        tmp named `<id>.tmp.json` would be picked up as a census."""
        paths = load_script("shared/_dataset_paths.py")
        ddir = self.path("datasets", "boxes")
        real = os.path.join(ddir, "census", f"{paths.CENSUS_PREFIX}20260818_120000.json")
        rec.atomic_write_json(real, {"complete": True})
        open(f"{real}.4242.tmp", "w", encoding="utf-8").write("{}")
        found = paths.census_paths(ddir)
        self.assertEqual([os.path.basename(f) for f in found],
                         [os.path.basename(real)],
                         "a tmp named `<id>.tmp.json` instead would be read as a "
                         "census, and a half-written one at that")

    def test_non_ascii_is_written_literally(self):
        """`ensure_ascii=False` by default. The money ledger was the one file in the
        repo written escaped, and nobody noticed because JSON round-trips either
        way -- it is the person reading it who cannot."""
        p = self.path("r.json")
        rec.atomic_write_json(p, {"where": "GPU 机房"})
        self.assertIn("机房", open(p, encoding="utf-8").read())

    def test_a_missing_parent_directory_is_created(self):
        p = self.path("a", "b", "r.json")
        rec.atomic_write_json(p, {"x": 1})
        self.assertTrue(os.path.isfile(p))

    def test_fsync_is_opt_in_and_still_writes_the_same_record(self):
        p = self.path("r.json")
        rec.atomic_write_json(p, {"x": 1}, fsync=True)
        self.assertEqual(json.load(open(p, encoding="utf-8")), {"x": 1})


class ReadJsonKeepsAbsentAndUnreadableApart(TempDirCase):
    """CLAUDE.md -> "Never silently": *Never report data you could not look at.*
    `required=False` says absent is a legitimate answer HERE. It never says an
    unreadable file is."""

    def _in_subprocess(self, path, required):
        src = ("import sys\nsys.path.insert(0, %r)\n"
               "from _records import read_json\n"
               "print(read_json(%r, required=%r))\n" % (
                   os.path.join(os.path.dirname(os.path.dirname(
                       os.path.abspath(__file__))), "scripts", "shared"),
                   path, required))
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", src],
                           capture_output=True, text=True, encoding="utf-8")
        return p.returncode, p.stdout

    def test_absent_and_not_required_is_none_not_an_exit(self):
        rc, out = self._in_subprocess(self.path("nope.json"), False)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "None")

    def test_absent_and_required_is_exit_2(self):
        self.assertEqual(self._in_subprocess(self.path("nope.json"), False)[0], 0)
        self.assertEqual(self._in_subprocess(self.path("nope.json"), True)[0], 2)

    def test_unreadable_is_exit_2_even_when_absent_would_have_been_fine(self):
        p = self.write("bad.json", "{not json")
        self.assertEqual(self._in_subprocess(p, False)[0], 2,
                         "`required=False` licences ABSENT, never UNREADABLE -- "
                         "collapsing them reports a truncated record as no record")


class GitSaveCommitsExactlyWhatItWasGiven(GitRepoCase):
    """`git_save`'s docstring states the safety property: a record skill that ran
    `git add -A` would sweep up whatever the user had in progress and commit it
    under a message about a dataset sweep -- a real harm, found much later.

    Nothing was checking it, and `git add -A` is a one-word edit.
    """

    def setUp(self):
        super().setUp()
        self.make_repo(files={"seed.txt": "seed\n"})

    def _save(self, paths, msg="record: x"):
        return rec.git_save(self.repo, paths, msg)

    def test_it_commits_the_record(self):
        p = os.path.join(self.repo, "rec.json")
        rec.atomic_write_json(p, {"a": 1})
        out = self._save([p])
        self.assertTrue(out["committed"], out)
        self.assertTrue(out["sha"])

    def test_it_leaves_an_unrelated_dirty_file_alone(self):
        p = os.path.join(self.repo, "rec.json")
        rec.atomic_write_json(p, {"a": 1})
        with open(os.path.join(self.repo, "seed.txt"), "a", encoding="utf-8") as f:
            f.write("the user's work in progress\n")
        self._save([p])
        status = self.git("status", "--short").stdout
        self.assertIn("seed.txt", status,
                      "the user's in-progress edit must still be uncommitted -- "
                      "this is what `git commit -- <paths>` buys")
        self.assertTrue(status.startswith(" M seed.txt") or "\n M seed.txt" in status,
                        "and it must not even be STAGED: `git add -A` would stage the "
                        f"user's work under a record skill's name. status was {status!r}")

    def test_it_does_not_commit_an_unrelated_staged_file_either(self):
        """`git commit -- <paths>` commits the working-tree version of those paths
        regardless of what else is staged. That is the form, and this is why."""
        p = os.path.join(self.repo, "rec.json")
        rec.atomic_write_json(p, {"a": 1})
        other = os.path.join(self.repo, "other.txt")
        open(other, "w", encoding="utf-8").write("staged by someone else\n")
        self.git("add", "--", other)
        self._save([p])
        self.assertIn("other.txt", self.git("status", "--short").stdout)

    def test_running_it_twice_is_safe_and_says_why(self):
        p = os.path.join(self.repo, "rec.json")
        rec.atomic_write_json(p, {"a": 1})
        self.assertTrue(self._save([p])["committed"])
        second = self._save([p])
        self.assertFalse(second["committed"])
        self.assertIn("not changed", second["why"])

    def test_an_ignored_path_is_reported_not_silently_dropped(self):
        """`secrets.json` is ignored deliberately. A save that skipped it quietly
        would report a record as kept that nothing will carry anywhere."""
        open(os.path.join(self.repo, ".gitignore"), "w",
             encoding="utf-8").write("secrets.json\n")
        self.git("add", "--", ".gitignore")
        self.git("commit", "-m", "ignore")
        p = os.path.join(self.repo, "secrets.json")
        rec.atomic_write_json(p, {"token": "x"})
        out = self._save([p])
        self.assertFalse(out["committed"])
        self.assertEqual(out["skipped_ignored"], [p])
        self.assertIn(".gitignore", out["why"])

    def test_a_non_git_tree_reports_rather_than_raising(self):
        out = rec.git_save(self.path("not_a_repo"), [], "x")
        self.assertFalse(out["committed"])
        self.assertIn("git", out["why"])

    def test_a_missing_file_is_nothing_to_commit_not_a_crash(self):
        out = self._save([os.path.join(self.repo, "never_written.json")])
        self.assertFalse(out["committed"])
        self.assertIn("no record file exists", out["why"])

    def test_git_tracked_answers_about_git_not_about_the_disk(self):
        p = os.path.join(self.repo, "rec.json")
        rec.atomic_write_json(p, {"a": 1})
        self.assertFalse(rec.git_tracked(self.repo, p),
                         "the file exists; that is not the same as git knowing it")
        self._save([p])
        self.assertTrue(rec.git_tracked(self.repo, p))


class ANumberBackCitedToAQuoteThatDoesNotContainIt(unittest.TestCase):
    """CLAUDE.md -> "Never silently": *Never record a metric you did not read.*

    Two records now cite
    numbers back to a transcribed line (`graph.json -> sources[].quote`,
    `conclusions.json -> evidence[].quote`), and this is the rule that decides
    whether the citation is real.

    ‼️ A FLOOR, not a proof: a quote containing the digits does not show the source
    was open, but a quote NOT containing them shows it was not.
    """

    def test_a_fraction_matches_the_log_that_printed_it_as_a_percentage(self):
        self.assertTrue(rec.quotes_the_number(0.0462, "affects 4.62% of frames"))

    def test_a_number_that_is_not_in_the_quote_fails(self):
        self.assertFalse(rec.quotes_the_number(0.47, "affects 4.62% of frames"))

    def test_a_non_number_is_not_checkable_here_and_passes(self):
        self.assertTrue(rec.quotes_the_number("multi-frame fusion", "anything"))

    def test_a_bool_is_not_a_number_for_this_purpose(self):
        self.assertTrue(rec.quotes_the_number(True, "no digits here"))

    def test_digits_strips_leading_zeros_so_the_two_renderings_meet(self):
        self.assertEqual(rec.digits(0.0462), rec.digits(4.62))
        self.assertEqual(rec.digits(0), "0")


if __name__ == "__main__":
    unittest.main()
