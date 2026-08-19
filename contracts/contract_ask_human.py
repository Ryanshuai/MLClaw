"""Somebody's word must not become a checked fact.

`/ask-human` records answers from people. Its one job that a notes field could not do
is keeping "they said so" apart from "something confirmed it" — and keeping it
apart in a MACHINE-READABLE field, because prose describing the difference gets
skimmed while `answer.kind` gets branched on.

The failure is the familiar shape: nothing raises. An operator says the shoot
finished, "done" goes in a record, a training set is frozen on it, and the seven
unfinished units are in the model. That is CLAUDE.md "Never record a metric you
did not read" with a person as the instrument, which is the bar in "Contracts":
a record written now and read later by someone who can no longer verify it.

Everything else here — the vocabulary, the staleness thresholds, the wording —
is free to change. These three groups are not.
"""
import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "ask-human/ask.py"


class AskCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        os.makedirs(self.project, exist_ok=True)

    def open_(self, *extra, asked="is the capture finished?"):
        rc, out, err = run_script(SCRIPT, "open", "--project", self.project,
                                  "--to", "operator", "--asked", asked, *extra)
        self.assertEqual(rc, 0, f"open failed: {err or out}")
        return out["ask_id"]

    def answer(self, aid, *extra, says="done"):
        return run_script(SCRIPT, "answer", "--project", self.project,
                          "--id", aid, "--says", says, *extra)

    def record(self, aid):
        with open(os.path.join(self.project, "asks", f"{aid}.json"), encoding="utf-8") as f:
            return json.load(f)

    def status(self, *extra):
        rc, out, _ = run_script(SCRIPT, "status", "--project", self.project, *extra)
        self.assertEqual(rc, 0)
        return out


class ClaimIsTheDefaultAndVerifiedMustBeEarned(AskCase):
    """CLAUDE.md -> "Never silently": never let somebody's word become a checked
    fact.

    `verified` is the only status asserting that something other than a person
    confirmed the answer. If it can be typed freely the vocabulary is decoration,
    and a decorative distinction is worse than none — it looks like a guarantee.
    """

    def test_an_answer_is_a_claim_unless_told_otherwise(self):
        aid = self.open_()
        rc, out, err = self.answer(aid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.record(aid)["answer"]["kind"], "claim")

    def test_verified_is_refused_when_nothing_checked_it(self):
        aid = self.open_()                      # no --verify declared
        rc, out, _ = self.answer(aid, "--as", "verified")
        self.assertEqual(rc, 1)
        self.assertIn("verified", out["refused"])
        self.assertIsNone(self.record(aid)["answer"], "a refused answer must not be filed")

    def test_a_passing_check_earns_verified_and_records_what_ran(self):
        aid = self.open_("--verify", "echo 52 units complete")
        rc, _, err = self.answer(aid, "--as", "verified")
        self.assertEqual(rc, 0, err)
        vb = self.record(aid)["answer"]["verified_by"]
        self.assertEqual(vb["exit_code"], 0)
        self.assertIn("52 units", vb["output"])

    def test_a_check_that_contradicts_the_person_refuses(self):
        """The case that pays for the whole feature. They say finished, the
        census disagrees, and nothing else in MLClaw is standing between that
        sentence and the record."""
        aid = self.open_("--verify", "exit 1")
        rc, out, _ = self.answer(aid, "--as", "verified")
        self.assertEqual(rc, 1)
        self.assertIn("contradicted", out["refused"])
        self.assertIsNone(self.record(aid)["answer"])

    def test_a_check_that_did_not_run_cannot_produce_verified(self):
        """The escape hatch must not be a way through. Leaving `kind` at
        "verified" while the check was skipped is the laundering this file
        exists to stop, committed via --skip-verify."""
        aid = self.open_("--verify", "echo ok")
        rc, _, _ = self.answer(aid, "--as", "verified", "--skip-verify")
        self.assertEqual(rc, 1)

    def test_skipping_the_check_is_allowed_only_with_other_corroboration(self):
        aid = self.open_("--verify", "echo ok")
        rc, _, err = self.answer(aid, "--as", "verified", "--skip-verify",
                                 "--evidence", "census counted 52 complete")
        self.assertEqual(rc, 0, err)
        vb = self.record(aid)["answer"]["verified_by"]
        self.assertFalse(vb["ran"], "the declared check must be recorded as not run")
        self.assertEqual(vb["corroborated_by"], "census counted 52 complete")

    def test_a_broken_check_does_not_silently_pass(self):
        """A verify command that cannot run is not a verify command that
        agreed — the extraction-failure-vs-absence rule, in its own house."""
        aid = self.open_("--verify", "exec /nonexistent/binary")
        rc, _, _ = self.answer(aid, "--as", "verified")
        self.assertEqual(rc, 1)


class AnAnswerCarriesWhoAndWhen(AskCase):
    """run-mechanics.md -> "Record integrity": a record read later by someone who
    can no longer check it. An unattributed answer is a rumour, and an answer
    about the world has a shelf life.
    """

    def test_the_answer_is_attributed(self):
        aid = self.open_()
        self.answer(aid, "--by", "Li")
        self.assertEqual(self.record(aid)["answer"]["by"], "Li")

    def test_attribution_falls_back_to_who_was_asked_never_to_nobody(self):
        aid = self.open_()
        self.answer(aid)
        self.assertEqual(self.record(aid)["answer"]["by"], "operator")

    def test_an_expired_answer_is_surfaced_as_expired(self):
        """'This data is fine to use' was true in July. Nothing makes it true
        today, and the record is the only thing that can say so."""
        aid = self.open_()
        self.answer(aid, "--valid-until", "2020-01-01T00:00:00+00:00")
        self.assertIn(aid, self.status()["expired_answers"])

    def test_unverified_claims_are_listed_not_buried(self):
        a1 = self.open_(asked="q1")
        self.answer(a1)
        a2 = self.open_(asked="q2", *["--verify", "echo ok"])
        self.answer(a2, "--as", "verified")
        st = self.status()
        self.assertEqual(st["unverified_claims"], [a1])

    def test_an_answered_ask_is_not_overwritten_in_place(self):
        """Somebody may already have acted on the first answer; replacing it
        silently rewrites the reason they did."""
        aid = self.open_()
        self.answer(aid, says="not yet")
        rc, _, _ = self.answer(aid, says="ok now")
        self.assertEqual(rc, 1)
        self.assertEqual(self.record(aid)["answer"]["says"], "not yet")


class NothingIsSent(AskCase):
    """CLAUDE.md -> "Never silently": sending to an external party is
    outward-facing and irreversible.

    The channel seam is deliberately empty. This check exists so that filling it
    in later is a decision somebody makes on purpose, rather than something that
    arrives with an adapter and is discovered afterwards.
    """

    def test_open_does_not_transmit_and_says_so(self):
        rc, out, _ = run_script(SCRIPT, "open", "--project", self.project,
                                "--to", "vendor", "--asked", "anything")
        self.assertEqual(rc, 0)
        self.assertIn("not message people", out["note"])
        self.assertEqual(self.record(out["ask_id"])["channel"], "manual")

    def test_the_record_names_the_party_not_their_address(self):
        """`asks/` is git-tracked; `resources.json` is the file that never is."""
        aid = self.open_()
        blob = json.dumps(self.record(aid))
        for leak in ("@", "http", "192.168", "100."):
            self.assertNotIn(leak, blob.replace("operator", ""),
                             f"a contact-shaped string ({leak}) reached a tracked record")


if __name__ == "__main__":
    unittest.main()
