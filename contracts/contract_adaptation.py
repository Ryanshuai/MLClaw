"""The adaptation ledger — what it must refuse to write down.

An adaptation session is a record written during a campaign and read afterwards
by somebody who can no longer re-run the probe: the converter is gone, the source
directory has moved on, and all that survives is what this file says happened.
That is the bar in CLAUDE.md -> "Conventions" for what earns a check, so the
checks here are about the four places the record could quietly say something
stronger than the evidence supports.
"""

import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "adaptation/adapt.py"
CONTRACT = {"items": {"annotations": {"format": "coco_json"}}}


class AdaptCase(TempDirCase):
    """A project with one consuming stage that declares a contract."""

    def setUp(self):
        super().setUp()
        self.write_json(os.path.join("stages", "training", "input.json"), CONTRACT)

    def run_adapt(self, *args):
        return run_script(SCRIPT, *args)

    def opened(self, **kw):
        rc, out, err = self.run_adapt(
            "open", "--project", self.tmp, "--dataset", "boxes",
            "--snapshot", "260808", "--consumer-stage", "training")
        self.assertEqual(rc, 0, f"open failed: {out or err}")
        return out["session_id"]

    def session(self, sid):
        return self.read_json(os.path.join("adaptation", sid, "session.json"))


class ADerivationNeedsAFrozenParent(AdaptCase):
    """data-line.md -> "Curate: a derivation cannot be re-observed": a campaign
    derives a new dataset from a parent, and a parent still moving means the
    result cannot say what it was made of. The refusal has to live at `open` —
    once rounds have run against a directory, there is nothing left to freeze
    that would describe what they actually consumed.
    """

    def test_an_unfrozen_parent_is_refused(self):
        rc, out, _ = self.run_adapt("open", "--project", self.tmp,
                                    "--dataset", "boxes",
                                    "--consumer-stage", "training")
        self.assertEqual(rc, 1, out)
        self.assertIn("snapshot", str(out).lower())

    def test_a_consumer_with_no_contract_is_refused(self):
        """Without the consuming code's requirement there is no oracle, so every
        round would be judged by whoever happened to be looking — the
        self-verification the loop exists to replace."""
        self.write_json(os.path.join("stages", "training", "input.json"), {"items": {}})
        rc, out, _ = self.run_adapt("open", "--project", self.tmp,
                                    "--dataset", "boxes", "--snapshot", "260808",
                                    "--consumer-stage", "training")
        self.assertEqual(rc, 1, out)
        self.assertIn("contract", str(out).lower())


class AContractIsCopiedNotReadLive(AdaptCase):
    """CLAUDE.md -> "Key design principles": the JSON config is the source of
    truth, which cuts both ways — a contract edited mid-campaign would silently
    invalidate every earlier round's verdict while the record goes on reading as
    one continuous loop. Same reason a repro session copies `mode` and `scope`.
    """

    def test_editing_the_stage_config_does_not_change_what_rounds_were_judged_against(self):
        sid = self.opened()
        self.write_json(os.path.join("stages", "training", "input.json"),
                        {"items": {"annotations": {"format": "yolo_txt"}}})
        rec = self.session(sid)
        self.assertEqual(rec["contract_at_open"]["annotations"]["format"], "coco_json",
                         "the session followed a later edit to the stage config")


class LoadingIsNotCorrectness(AdaptCase):
    """CLAUDE.md -> "Never silently": the same shape as "never say a unit is
    complete because its directory exists". A converter emitting all-zero boxes
    loads perfectly and a category id the dataloader silently clamps is not a
    crash, so a round that passed only the fatal layer must not be recordable as
    meeting the bar.
    """

    def test_a_round_cannot_pass_on_the_probe_alone(self):
        sid = self.opened()
        rc, out, _ = self.run_adapt("round", "--project", self.tmp, "--id", sid,
                                    "--probe", "pass", "--audit", "not_run")
        self.assertEqual(rc, 1, out)

    def test_a_probe_pass_with_a_dirty_audit_is_not_clean(self):
        sid = self.opened()
        rc, out, _ = self.run_adapt("round", "--project", self.tmp, "--id", sid,
                                    "--probe", "pass", "--audit", "dirty")
        self.assertEqual(rc, 0, out)
        self.assertFalse(out["meets_declared_clean"])


class AProbeThatNeverRanIsNotAPass(AdaptCase):
    """CLAUDE.md -> "Never silently": extraction failure and "it never produced
    it" must not both become the same cell. Applied to this loop, a probe that
    fails open turns every unchecked round into a pass, so `adapted` — which
    asserts the consuming code accepted the data — is refusable when nothing ran.
    `unverifiable` is the verdict that exists for it.
    """

    def test_adapted_is_refused_when_no_round_ran_the_probe(self):
        sid = self.opened()
        self.run_adapt("round", "--project", self.tmp, "--id", sid,
                       "--probe", "not_run", "--audit", "not_run")
        rc, out, _ = self.run_adapt("close", "--project", self.tmp, "--id", sid,
                                    "--verdict", "adapted")
        self.assertEqual(rc, 1, out)
        self.assertIn("unverifiable", str(out))

    def test_a_non_adapted_verdict_requires_an_attribution(self):
        """Which end the defect was on is the question these loops re-litigate
        every time; it is cheap to record and expensive to re-derive."""
        sid = self.opened()
        rc, out, _ = self.run_adapt("close", "--project", self.tmp, "--id", sid,
                                    "--verdict", "degraded_to_rework")
        self.assertEqual(rc, 1, out)

    def test_a_probe_that_did_not_run_leaves_a_caveat(self):
        sid = self.opened()
        self.run_adapt("round", "--project", self.tmp, "--id", sid,
                       "--probe", "not_run", "--audit", "not_run")
        rc, out, _ = self.run_adapt("close", "--project", self.tmp, "--id", sid,
                                    "--verdict", "unverifiable",
                                    "--attributed-to", "dataset")
        self.assertEqual(rc, 0, out)
        kinds = [c["kind"] for c in self.session(sid)["caveats"]]
        self.assertIn("probe_not_run", kinds)


class AHandedBackFindingIsNotAResolvedOne(AdaptCase):
    """CLAUDE.md -> "Never silently": "never let somebody's word become a checked
    fact", one domain over. "I cannot fix this" and "I disagree" are terminal for
    the responder and not for the finding — closing it there would record "we
    stopped discussing it" as "it was resolved", and the campaign would close
    over somebody's unanswered objection.
    """

    def test_cannot_fix_leaves_the_finding_open(self):
        sid = self.opened()
        self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                       "--against", "dataset", "--what", "no depth layer")
        rc, out, _ = self.run_adapt("respond", "--project", self.tmp, "--id", sid,
                                    "--n", "1", "--action", "cannot_fix")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.session(sid)["findings"][0]["state"], "blocked")

    def test_a_partial_fix_is_not_a_fix(self):
        sid = self.opened()
        self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                       "--against", "dataset", "--what", "half the boxes")
        self.run_adapt("respond", "--project", self.tmp, "--id", sid,
                       "--n", "1", "--action", "partially_fixed")
        self.assertEqual(self.session(sid)["findings"][0]["state"], "open")

    def test_adapted_is_refused_while_a_finding_is_still_open(self):
        sid = self.opened()
        self.run_adapt("round", "--project", self.tmp, "--id", sid,
                       "--probe", "pass", "--audit", "clean")
        self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                       "--against", "dataset", "--what", "still wrong")
        rc, out, _ = self.run_adapt("close", "--project", self.tmp, "--id", sid,
                                    "--verdict", "adapted")
        self.assertEqual(rc, 1, out)

    def test_overruled_responses_are_kept(self):
        """Whether one end's calls hold up is answerable only from how often the
        other end disagreed, so a later response never replaces an earlier one."""
        sid = self.opened()
        self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                       "--against", "dataset", "--what", "x")
        self.run_adapt("respond", "--project", self.tmp, "--id", sid,
                       "--n", "1", "--action", "disagree", "--detail", "not ours")
        self.run_adapt("respond", "--project", self.tmp, "--id", sid,
                       "--n", "1", "--action", "fixed", "--detail", "fine, ours")
        self.assertEqual(len(self.session(sid)["findings"][0]["responses"]), 2)


class AReversalOpensAFindingRatherThanRewritingOne(AdaptCase):
    """CLAUDE.md -> "Key design principles": the record is the deliverable, and a
    direction field that mutated in place would rewrite history so the issue reads
    as having always been the other end's. One schema flowing both ways instead:
    the blocked finding stays pointed where it was raised, and a NEW one carries
    the reversal with `reverses` naming its parent.
    """

    def test_the_original_keeps_its_direction(self):
        sid = self.opened()
        self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                       "--against", "dataset", "--what", "no depth layer")
        rc, out, _ = self.run_adapt("respond", "--project", self.tmp, "--id", sid,
                                    "--n", "1", "--action", "needs_from_other_side",
                                    "--detail", "consumer must drop the requirement")
        self.assertEqual(rc, 0, out)
        findings = self.session(sid)["findings"]
        self.assertEqual(findings[0]["raised_against"], "dataset")
        self.assertEqual(findings[0]["state"], "reversed")
        self.assertEqual(findings[1]["raised_against"], "consumer")
        self.assertEqual(findings[1]["reverses"], 1)

    def test_a_degraded_close_lists_every_unresolved_finding(self):
        """Not just the handed-back ones: a finding still `open` when the campaign
        degrades is exactly the work the rework round has to carry."""
        sid = self.opened()
        self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                       "--against", "dataset", "--what", "no depth layer")
        self.run_adapt("respond", "--project", self.tmp, "--id", sid,
                       "--n", "1", "--action", "needs_from_other_side")
        rc, out, _ = self.run_adapt("close", "--project", self.tmp, "--id", sid,
                                    "--verdict", "degraded_to_rework",
                                    "--attributed-to", "dataset")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(f["n"] for f in out["unresolved_findings"]), [1, 2])


class TheDistillationCarriesItsEvidence(AdaptCase):
    """CLAUDE.md -> "Never silently": the compressed form is the only part of this
    record safe to read alone, so a conclusion in it must name the rounds that
    established it. The `refuted` bucket is the half every summary drops, and
    dropping it is how round five re-tries what round two eliminated.
    """

    def test_a_conclusion_citing_nothing_is_refused(self):
        sid = self.opened()
        rc, out, _ = self.run_adapt("distill", "--project", self.tmp, "--id", sid,
                                    "--kind", "refuted", "--says", "no depth exists")
        self.assertEqual(rc, 1, out)

    def test_a_conclusion_citing_a_round_that_never_ran_is_refused(self):
        sid = self.opened()
        rc, out, _ = self.run_adapt("distill", "--project", self.tmp, "--id", sid,
                                    "--kind", "refuted", "--says", "x", "--cites", "4")
        self.assertEqual(rc, 1, out)

    def test_widening_the_bar_after_results_are_in_is_a_caveat_not_an_edit(self):
        """At round six, under a deadline, the definition of clean is under
        pressure from whoever has to meet it."""
        sid = self.opened()
        self.run_adapt("round", "--project", self.tmp, "--id", sid,
                       "--probe", "fail", "--audit", "not_run")
        rc, out, _ = self.run_adapt("relax", "--project", self.tmp, "--id", sid,
                                    "--audit", "advisory tolerated",
                                    "--why", "deadline")
        self.assertEqual(rc, 0, out)
        rec = self.session(sid)
        self.assertEqual([c["kind"] for c in rec["caveats"]], ["declared_clean_widened"])
        self.assertEqual(rec["declared_clean_at_open"]["audit"], "no fatal findings",
                         "the original declaration was overwritten")


class AClosedCampaignIsNotAppendedTo(AdaptCase):
    """CLAUDE.md -> "Never silently": a campaign that was concluded and then
    edited reads as though the conclusion covered the edit. The successor cites
    the closed one instead — the same rule `/ask-human` follows when it refuses to
    overwrite an answer somebody already acted on.
    """

    def test_raising_against_a_closed_session_is_refused(self):
        sid = self.opened()
        self.run_adapt("close", "--project", self.tmp, "--id", sid,
                       "--verdict", "unverifiable", "--attributed-to", "contract")
        rc, out, _ = self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                                    "--against", "dataset", "--what", "late finding")
        self.assertEqual(rc, 1, out)


class StatusIsSafeAtConversationStart(AdaptCase):
    """CLAUDE.md -> "Status": the conversation-start pass runs this, so it must
    answer on a project that has never opened a campaign rather than erroring —
    and it must never touch the network, which is what makes it safe there.
    """

    def test_a_project_with_no_adaptations_answers_empty(self):
        rc, out, _ = self.run_adapt("status", "--project", self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["adaptations"], [])
        self.assertEqual(out["open"], 0)

    def test_blocked_findings_are_counted_across_sessions(self):
        sid = self.opened()
        self.run_adapt("raise", "--project", self.tmp, "--id", sid,
                       "--against", "dataset", "--what", "x")
        self.run_adapt("respond", "--project", self.tmp, "--id", sid,
                       "--n", "1", "--action", "cannot_fix")
        rc, out, _ = self.run_adapt("status", "--project", self.tmp, "--open-only")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["blocked_anywhere"], 1)


if __name__ == "__main__":
    unittest.main()
