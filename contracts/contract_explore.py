"""The experiment graph — what it must refuse to write down.

An exploration graph is written during one round and read by the NEXT one, by
somebody who can no longer re-run the arm: the branch is gone, the corpus has
rolled, and all that survives is what the card says. That is the bar in
CLAUDE.md -> "Conventions" for what earns a check.

The checks here are not about the graph being tidy. Each one covers a way the
record can read STRONGER than the evidence behind it — a share from another
corpus reading as this corpus's, a trend number reading as a controlled one, a
botched port reading as a refuted idea. Every one of those was paid for once in
the skill this was ported from; the point of the port was to make them raise.
"""

import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "explore/graph.py"
CORPUS = {"dataset_id": "boxes", "snapshot": "260731",
          "declared_at": "2026-08-01T00:00:00+00:00"}
BASE_SHA = "a" * 40
BASE = {"commit": BASE_SHA, "repo_subdir": None,
        "declared_at": "2026-08-01T00:00:00+00:00"}
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "lifecycle", "exploration")


def _template(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as f:
        return json.load(f)


class GraphCase(TempDirCase):
    """A project with a declared corpus and one complete, ready card."""

    def setUp(self):
        super().setUp()
        g = _template("graph.json")
        g["corpus"] = dict(CORPUS)
        g["base"] = dict(BASE)
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        for n in ("baseline.json", "state.json", "config.json"):
            self.write_json(os.path.join("stages", "exploration", n), _template(n))

    def g(self, *args):
        return run_script(SCRIPT, *args, "--project", self.tmp)

    def graph(self):
        return self.read_json(os.path.join("stages", "exploration", "graph.json"))

    def card(self, nid="N01"):
        for n in self.graph()["nodes"]:
            if n["id"] == nid:
                return n
        raise AssertionError(f"no card {nid}")

    def add_complete(self, **over):
        rc, out, err = self.g("add", "--title", "t", "--kind", "port",
                              "--criterion", "neg ratio down", "--guardrail", "AP50",
                              "--parent", "run_A", "--kill-condition", "flat at ep12")
        self.assertEqual(rc, 0, f"add failed: {out or err}")
        nid = out["id"]
        share = over.pop("share", {"value": 0.05, "measured_on": {
            "dataset_id": CORPUS["dataset_id"], "snapshot": CORPUS["snapshot"]}})
        self.g("set", "--id", nid, "--set", "premise=p",
               "--set", "premise_share=" + json.dumps(share),
               "--set", "oracle_ceiling=1.2")
        # ‼️ The fixture models the discipline rather than a violation of it: a
        # `port` card gets its own branch off the round's base, because that is
        # what a correct round looks like and an incorrect one must be built
        # deliberately by the test that is about it. A shared fixture that quietly
        # breaks an invariant leaves every OTHER test reading a critical it did
        # not ask for, which is how a suite stops saying anything.
        self.g("set", "--id", nid, "--set", "tree=" + json.dumps(
            {"branch": "explore/" + nid, "base": BASE_SHA}))
        return nid

    def run_it(self, nid, run_id="run_20260817_000000"):
        self.g("set", "--id", nid, "--set", "state=running", "--set", f"run_id={run_id}")

    def fill(self, nid, tier="T2", result='{"neg_ratio":0.3}'):
        return self.g("fill", "--id", nid, "--result", result, "--tier", tier)


class AShareFromAnotherCorpusIsNoShare(GraphCase):
    """SKILL.md -> Stage 3 rule 2.5, and CLAUDE.md -> "Never compare metrics
    across different `mode` or non-equivalent `scope`" one level down: there the
    rule stops two numbers being subtracted, here it stops a proposal existing.

    The recorded cost: a premise share quoted from another corpus predicted 47%
    where this corpus measured 4.62% — an order of magnitude, with five arms
    already queued behind it. A wrong share does not make a card weak, it makes
    the card about a different question, so it is treated as ABSENT rather than
    as a low-confidence value.
    """

    def test_a_foreign_share_leaves_the_card_incomplete(self):
        nid = self.add_complete(share={"value": 0.47, "measured_on": {
            "dataset_id": "coco", "snapshot": "2017"}})
        rc, out, _ = self.g("ready")
        self.assertEqual(out["ready"], [], "a foreign share must not reach the ready set")
        why = json.dumps(out["blocked"])
        self.assertIn("coco", why)
        self.assertEqual(self.card(nid)["state"], "draft")

    def test_a_share_with_no_corpus_at_all_is_refused_too(self):
        self.add_complete(share={"value": 0.47})
        rc, out, _ = self.g("ready")
        self.assertEqual(out["ready"], [])
        self.assertIn("measured_on", json.dumps(out["blocked"]))

    def test_the_same_corpus_passes(self):
        self.add_complete()
        rc, out, _ = self.g("ready")
        self.assertEqual(len(out["ready"]), 1)


class AKillMustSayHowItRevives(GraphCase):
    """references/experiment-graph.md -> CLOSE: the four deaths have differently
    shaped revival conditions, so one written without a condition is the same as
    not written — and the card comes back as a fresh proposal next round.

    This is the same failure `adaptation`'s `refuted` bucket exists to prevent
    ("round five re-tries what round two eliminated"), and the same one ARA's
    flat `dead_end` node cannot prevent, having no revival concept at all.
    """

    def setUp(self):
        super().setUp()
        self.nid = self.add_complete()
        self.run_it(self.nid)
        self.fill(self.nid, tier="T1")

    def test_a_kill_without_revive_if_is_refused(self):
        rc, out, _ = self.g("close", "--id", self.nid,
                            "--killed-by", "faithful_but_inert")
        self.assertEqual(rc, 1, "refusal, not breakage")
        self.assertIn("revive", json.dumps(out).lower())
        self.assertEqual(self.card(self.nid)["state"], "filled",
                         "a refused kill must not have moved the card")

    def test_a_typed_kill_with_a_condition_lands(self):
        rc, out, _ = self.g("close", "--id", self.nid,
                            "--killed-by", "share_too_small",
                            "--revive-if", "re-measure on the night corpus")
        self.assertEqual(rc, 0)
        c = self.card(self.nid)
        self.assertEqual(c["state"], "killed")
        self.assertEqual(c["killed_by"], "share_too_small")
        self.assertTrue(c["revive_if"])

    def test_check_flags_a_kill_that_lost_its_condition(self):
        self.g("close", "--id", self.nid, "--killed-by", "share_too_small",
               "--revive-if", "x")
        g = self.graph()
        g["nodes"][0]["revive_if"] = None
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("kill_without_revive",
                      [f["invariant"] for f in out["findings"]])


class ABotchedPortIsNotARefutedIdea(GraphCase):
    """references/experiment-graph.md -> CLOSE, the fourth `killed_by`:
    `unfaithful_port` is explicitly "not a death, go back and fix it".

    It is in the vocabulary so that the one thing that must never be recorded
    cannot be: "we implemented it wrong" filed as "the idea does not work". The
    next round reads a kill list, not a diff.
    """

    def test_an_unfaithful_port_returns_to_running_and_is_never_killed(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid)
        rc, out, _ = self.g("close", "--id", nid, "--killed-by", "unfaithful_port")
        self.assertEqual(rc, 0)
        c = self.card(nid)
        self.assertEqual(c["state"], "running", "a bad port is not a dead card")
        self.assertIsNone(c["killed_by"])
        self.assertIsNone(c["result"], "the void result must not stay on the card")


class EveryNumberCarriesItsTier(GraphCase):
    """SKILL.md -> Stage 3.5 rule 1: a `[T1 trend]` conclusion may not be cited
    next week as a `[T2 controlled]` one. Soft numbers being promoted to hard
    conclusions is named there as the most common way this pipeline dies, and it
    is how a false noise floor entered the record once already.
    """

    def test_fill_without_a_tier_is_a_usage_error(self):
        nid = self.add_complete()
        self.run_it(nid)
        rc, out, err = run_script(SCRIPT, "fill", "--id", nid, "--result", "{}",
                                  "--project", self.tmp)
        self.assertEqual(rc, 2, "argparse rejects it before the record is touched")

    def test_check_flags_a_result_whose_tier_was_stripped(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T1")
        g = self.graph()
        g["nodes"][0]["tier"] = None
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        rc, out, _ = self.g("check")
        self.assertIn("result_without_tier", [f["invariant"] for f in out["findings"]])


class AnApproximationCannotRefuteTheOriginal(GraphCase):
    """SKILL.md -> Stage 3.5 rule 3: an approximation failing cannot refute the
    original — a T4 exists to
    PRICE the full technique, and using it as a verdict kills a good idea with a
    bad proxy. The check sits on `close` because that is the only moment the
    approximation's result becomes a conclusion about something else.
    """

    def setUp(self):
        super().setUp()
        self.nid = self.add_complete()
        self.run_it(self.nid)
        self.fill(self.nid, tier="T4")

    def test_a_t4_cannot_be_closed_as_lost(self):
        rc, out, _ = self.g("close", "--id", self.nid, "--verdict", "lost")
        self.assertEqual(rc, 1)
        self.assertEqual(self.card(self.nid)["state"], "filled")

    def test_a_t4_cannot_kill_either(self):
        rc, out, _ = self.g("close", "--id", self.nid,
                            "--killed-by", "faithful_but_inert", "--revive-if", "x")
        self.assertEqual(rc, 1)

    def test_a_t4_may_be_downgraded(self):
        rc, _, _ = self.g("close", "--id", self.nid, "--verdict", "downgraded")
        self.assertEqual(rc, 0)


class AResultIsNotAConclusion(GraphCase):
    """references/experiment-graph.md -> §2: 🟪 filled and ✅ closed must stay
    separate, because a card's meaning often waits on ANOTHER card. The recorded
    instance: a mechanism criterion passed literally while the verdict was "the
    mechanism was verified and it is not worth anything" — a verdict that needed
    a different card's share measurement before it could be reached.

    Treating filled as closed propagates an unexplained number downstream as a
    finding, which is the same defect as `/ask-human` letting a `claim` be read
    as `verified`.
    """

    def test_a_card_that_never_ran_cannot_be_adjudicated(self):
        nid = self.add_complete()
        rc, out, _ = self.g("close", "--id", nid, "--verdict", "won")
        self.assertEqual(rc, 1)
        self.assertIn("filled", json.dumps(out))

    def test_filled_cards_are_reported_as_awaiting_a_verdict(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T1")
        rc, out, _ = self.g("status")
        self.assertEqual(out["awaiting_verdict"], [nid])


class AConclusionHasARunBehindIt(GraphCase):
    """references/experiment-graph.md -> §4 invariant 1: every ✅/❌ has a run id
    or a measurement source. A conclusion nobody can trace is one nobody can
    re-check, which is the whole failure this record layer exists to prevent —
    MLClaw's own form of it is "Never record a metric you did not read".
    """

    def test_check_flags_a_settled_card_with_no_run(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T1")
        self.g("close", "--id", nid, "--verdict", "won")
        g = self.graph()
        g["nodes"][0]["run_id"] = None
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("settled_without_source", [f["invariant"] for f in out["findings"]])


class TheGateCannotBeBypassed(GraphCase):
    """references/experiment-graph.md -> §2 illegal transitions and §4 invariant
    2: a running card's dependencies are all settled. An arm opened over an
    unsettled dependency produces a delta attributable to two things at once, and
    nothing about the result says so.
    """

    def test_an_arm_over_an_unsettled_dependency_is_flagged(self):
        a = self.add_complete()
        b = self.add_complete()
        self.g("set", "--id", b, "--set", f'depends_on=["{a}"]')
        rc, out, _ = self.g("ready")
        self.assertNotIn(b, [r["id"] for r in out["ready"]])
        self.run_it(b)
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("gate_bypassed", [f["invariant"] for f in out["findings"]])

    def test_an_empty_ready_set_with_a_full_queue_is_named_a_deadlock(self):
        a = self.add_complete()
        b = self.add_complete()
        self.g("set", "--id", a, "--set", f'depends_on=["{b}"]')
        self.g("set", "--id", b, "--set", f'depends_on=["{a}"]')
        rc, out, _ = self.g("ready")
        self.assertEqual(out["ready"], [])
        self.assertEqual(out["deadlock"]["kind"], "cycle",
                         "a cycle and a single blocker want opposite responses")


class WithoutAFloorThereIsNoNegativeResult(GraphCase):
    """SKILL.md -> Stage 0, and references/explore-or-stop.md -> §3 first row:
    with no measured noise floor, "no significant improvement" is UNDECIDABLE,
    not negative. The recorded cost: a floor reported as 0.06 that was really
    0.25, a "+0.30 is real" conclusion built on it, and the lot withdrawn.

    T1 is deliberately exempt — it does not claim to have cleared a floor. The
    check fires only where a result claims to be controlled.
    """

    def test_a_t2_result_with_no_floor_is_critical(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T2")
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("hard_result_without_noise_floor",
                      [f["invariant"] for f in out["findings"]])

    def test_a_t1_trend_does_not_need_one(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T1")
        rc, out, _ = self.g("check")
        self.assertNotIn("hard_result_without_noise_floor",
                         [f["invariant"] for f in out["findings"]])


class QueueNumbersAreNeverReused(GraphCase):
    """references/experiment-graph.md -> §1: `id` is stable and not recycled.
    A reused number makes every cross-reference written before the reuse point
    at a different proposal, silently — the kill list is the main reader of those
    references and it is the one that must not rot.
    """

    def test_a_killed_cards_number_stays_spent(self):
        a = self.add_complete()
        self.run_it(a)
        self.fill(a, tier="T1")
        self.g("close", "--id", a, "--killed-by", "wrong_mechanism",
               "--revive-if", "measure the effect directly")
        b = self.add_complete()
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.graph()["nodes"]), 2)


class SettledCardsAreNotEdited(GraphCase):
    """references/experiment-graph.md -> Layer note, and the same append-only
    rule MLClaw applies to a finalized run: the trace is how history is recovered
    once the logic layer has moved on. A conclusion that changed is a NEW card
    citing this one.
    """

    def test_editing_a_closed_card_is_refused(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T1")
        self.g("close", "--id", nid, "--verdict", "won")
        rc, out, _ = self.g("set", "--id", nid, "--set", "title=rewritten")
        self.assertEqual(rc, 1)
        self.assertEqual(self.card(nid)["title"], "t")


class FillEnumeratesWhatItMayHaveVoided(GraphCase):
    """references/experiment-graph.md -> FILL: "a fill with no propagation is the most
    expensive failure on this pipeline" — a result that invalidates another card's premise, unswept, leaves that
    arm on the queue for somebody to run.

    The script enumerates candidates and does not judge them; that split is the
    contract. A verb that decided which premises were void would be guessing, and
    a verb that returned nothing would be the markdown version again.
    """

    def test_dependents_are_listed_for_review(self):
        a = self.add_complete()
        b = self.add_complete()
        self.g("set", "--id", b, "--set", f'depends_on=["{a}"]')
        self.run_it(a)
        rc, out, _ = self.fill(a, tier="T1")
        self.assertEqual(rc, 0)
        self.assertIn(b, out["must_review"]["depends_on_this"])
        self.assertEqual(len(out["must_review"]["questions"]), 3)


class CheckReportsAndNeverRepairs(GraphCase):
    """CLAUDE.md -> "Contracts": a green run means the record layer is intact.
    The corollary for this script is that `check` must leave the record exactly
    as it found it — a graph that repairs itself hides that something wrote an
    illegal state, and every illegal state here reads as normal.
    """

    def test_a_failing_check_changes_nothing_on_disk(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T2")
        before = self.read(os.path.join("stages", "exploration", "graph.json"))
        rc, _, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertEqual(before,
                         self.read(os.path.join("stages", "exploration", "graph.json")))

    def test_a_missing_graph_is_a_refusal_not_a_traceback(self):
        os.remove(self.path("stages", "exploration", "graph.json"))
        rc, out, err = self.g("check")
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)


class TheQueueRemembersWhoAskedForIt(GraphCase):
    """SKILL.md -> Stage 3: this chapter is a queue maintained BY THE USER, not the
    agent's notebook.

    The provenance tag is borrowed from ARA (arXiv:2604.24658) and it is
    load-bearing for exactly that sentence: if the queue is the user's, then a
    card the user demanded and a card the agent invented cannot be the same row.
    The conservative default is `ai-suggested`, and it never upgrades on its own —
    the agent's confidence must not be able to earn a `user` tag, the same reason
    `/ask-human` refuses `verified` when only a person said so.
    """

    def test_the_default_is_the_conservative_one(self):
        nid = self.add_complete()
        self.assertEqual(self.card(nid)["provenance"], "ai-suggested")

    def test_a_user_card_is_recorded_as_one(self):
        rc, out, _ = self.g("add", "--title", "cross attention between SKUs?",
                            "--kind", "port", "--provenance", "user")
        self.assertEqual(rc, 0)
        self.assertEqual(out["provenance"], "user")

    def test_an_invented_provenance_is_a_usage_error(self):
        nid = self.add_complete()
        rc, out, _ = self.g("set", "--id", nid, "--set", "provenance=obviously-true")
        self.assertEqual(rc, 2, "a vocabulary this small must not accept free text")
        self.assertEqual(self.card(nid)["provenance"], "ai-suggested")

    def test_check_flags_a_card_whose_tag_went_missing(self):
        nid = self.add_complete()
        g = self.graph()
        del g["nodes"][0]["provenance"]
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        rc, out, _ = self.g("check")
        self.assertIn("untagged_provenance", [f["invariant"] for f in out["findings"]])

    def test_an_all_agent_queue_is_surfaced_not_forbidden(self):
        self.add_complete()
        rc, out, _ = self.g("check")
        finds = [f for f in out["findings"] if f["invariant"] == "no_human_proposal"]
        self.assertEqual(len(finds), 1)
        self.assertEqual(finds[0]["severity"], "minor",
                         "an agent-proposed queue is worth noticing, not refusing")


class ANumberCarriesTheLineItCameFrom(GraphCase):
    """run-mechanics.md -> "Record integrity", the grounding row; borrowed from
    ARA (arXiv:2604.24658) -> research-manager "Number grounding".

    CLAUDE.md's "Never record a metric you did not read" is a prohibition, and a
    prohibition leaves no trace when broken — a value typed from memory and one
    read off a log are the same JSON, and the remembered one is likelier to be
    round and plausible. The quote is what makes the difference visible.

    The digit check is a FLOOR, not a proof: containing the digits does not show
    the source was open, but NOT containing them shows it was not. That asymmetry
    is the whole design, and it is why `pending` is not a finding while a bare
    plausible path is.
    """

    def constants(self, *entries):
        st = _template("state.json")
        st["constants"] = list(entries)
        self.write_json(os.path.join("stages", "exploration", "state.json"), st)

    def invariants(self):
        rc, out, _ = self.g("check")
        return [f["invariant"] for f in out["findings"]], out

    def test_a_bare_number_with_no_source_is_critical(self):
        self.constants({"name": "neg_share", "value": 0.0462})
        inv, out = self.invariants()
        self.assertIn("ungrounded_number", inv)
        self.assertEqual([f["severity"] for f in out["findings"]
                          if f["invariant"] == "ungrounded_number"], ["critical"])

    def test_a_path_with_no_quote_is_not_grounding(self):
        self.constants({"name": "neg_share", "value": 0.0462,
                        "sources": [{"ref": "logs/scan.txt:214", "kind": "result"}]})
        inv, out = self.invariants()
        self.assertIn("ungrounded_number", inv)
        self.assertIn("quote", " ".join(f["detail"] for f in out["findings"]))

    def test_a_quote_that_does_not_contain_the_number_is_refused(self):
        self.constants({"name": "neg_share", "value": 0.0462, "sources": [
            {"ref": "logs/scan.txt:214", "kind": "result",
             "quote": "G>=52 frames: 254/5495 (47.0%)"}]})
        inv, out = self.invariants()
        self.assertIn("ungrounded_number", inv,
                      "47.0% cannot be the source of 0.0462 — this is the exact "
                      "failure the corpus rule cost an order of magnitude on")

    def test_the_percentage_form_of_the_same_number_matches(self):
        self.constants({"name": "neg_share", "value": 0.0462, "sources": [
            {"ref": "logs/scan.txt:214", "kind": "result",
             "quote": "G>=52 frames: 254/5495 (4.62%)"}]})
        inv, _ = self.invariants()
        self.assertNotIn("ungrounded_number", inv,
                         "records keep the fraction and logs print the percentage; "
                         "a check that cannot see through that would be turned off")

    def test_a_source_must_say_input_or_result(self):
        self.constants({"name": "lr", "value": 0.0003, "sources": [
            {"ref": "config.yaml:12", "quote": "lr: 0.0003"}]})
        inv, out = self.invariants()
        self.assertIn("ungrounded_number", inv)
        self.assertIn("`input`", " ".join(f["detail"] for f in out["findings"]))

    def test_pending_is_honest_and_not_a_finding(self):
        self.constants({"name": "neg_share", "value": None,
                        "pending": "loader scan not run on this corpus yet"})
        inv, _ = self.invariants()
        self.assertNotIn("ungrounded_number", inv,
                         "an admitted gap must cost less than a plausible citation, "
                         "or the record fills up with plausible citations")

    def test_a_value_and_a_pending_note_together_is_a_contradiction(self):
        """And it is reported AS a contradiction, not as a missing source.

        The two are different instructions to whoever reads this next: "decide
        which of these is true" versus "go open the log". Collapsing them sends
        the reader to the wrong repair, so the message is part of the contract —
        without this assertion the `pending` branch is dead code that happens to
        be reachable through the bare-number path.
        """
        self.constants({"name": "neg_share", "value": 0.0462, "pending": "not measured"})
        inv, out = self.invariants()
        self.assertIn("ungrounded_number", inv)
        detail = " ".join(f["detail"] for f in out["findings"])
        self.assertIn("decide which", detail)
        self.assertNotIn("no source", detail)

    def test_the_noise_floor_is_grounded_like_everything_else(self):
        b = _template("baseline.json")
        b.update(value=0.25, unit="AP", metric="AP50", runs=["a", "b"])
        self.write_json(os.path.join("stages", "exploration", "baseline.json"), b)
        inv, out = self.invariants()
        self.assertIn("ungrounded_number", inv,
                      "the floor is what every later verdict rests on — it is the "
                      "last number that may be typed from memory")


class TwoRecordsMayDisagreeWithoutOneBeingErased(GraphCase):
    """run-mechanics.md -> "Record integrity"; borrowed from ARA
    (arXiv:2604.24658) -> research-manager "Contradiction trigger".

    MLClaw had no way to express this. A result contradicting a settled verdict
    left two options and both destroy the record: rewrite the old card, losing
    exactly what the next round reads, or say nothing. The third state — these
    two disagree and nobody has ruled — is the true one, and these checks are
    what keep it expressible.
    """

    def two_cards(self, t1="T2", t2="T2"):
        a, b = self.add_complete(), self.add_complete()
        for nid, tier in ((a, t1), (b, t2)):
            self.run_it(nid, run_id="run_" + nid)
            self.fill(nid, tier=tier)
        return a, b

    def test_a_dispute_marks_both_and_reverts_neither(self):
        a, b = self.two_cards()
        self.g("close", "--id", a, "--verdict", "won")
        rc, out, _ = self.g("dispute", "--id", b, "--against", a,
                            "--detail", "opposite sign on the same corpus")
        self.assertEqual(rc, 0)
        first = self.card(a)
        self.assertEqual(first["state"], "closed")
        self.assertEqual(first["verdict"], "won", "the verdict must survive the dispute")
        self.assertIn(out["dispute"], first["disputed_by"])
        self.assertIn(out["dispute"], self.card(b)["disputed_by"])

    def test_a_cheaper_check_cannot_refute_a_dearer_one(self):
        """SKILL.md Stage 3.5 rule 2: a cheap check can give you a reason to continue,
        not a reason to rule something out.

        Most apparent contradictions between a short run and a controlled one are
        not disagreements at all — they are incomparabilities. Adjudicating one
        as a disagreement is how a good result gets thrown away by a cheap probe.
        """
        a, b = self.two_cards(t1="T2", t2="T1")
        rc, out, _ = self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        self.assertEqual(rc, 1)
        self.assertIsNone(self.card(a).get("disputed_by"),
                          "a refused dispute must leave no mark")

    def test_a_t4_approximation_cannot_dispute_anything(self):
        a, b = self.two_cards(t1="T2", t2="T4")
        rc, _, _ = self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        self.assertEqual(rc, 1, "an approximation failing never refutes the original")

    def test_a_dearer_check_may_dispute_a_cheaper_one(self):
        a, b = self.two_cards(t1="T1", t2="T2")
        rc, _, _ = self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        self.assertEqual(rc, 0)

    def test_check_reports_an_open_dispute_without_blocking_the_graph(self):
        a, b = self.two_cards()
        self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        rc, out, _ = self.g("check")
        sev = {f["invariant"]: f["severity"] for f in out["findings"]}
        self.assertEqual(sev.get("open_dispute"), "major",
                         "a contested pair elsewhere must not stop unrelated arms, "
                         "or check becomes the thing people route around")

    def test_building_on_a_contested_card_is_critical(self):
        a, b = self.two_cards()
        self.g("close", "--id", a, "--verdict", "won")
        self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        c = self.add_complete()
        self.g("set", "--id", c, "--set", f'depends_on=["{a}"]')
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("built_on_contested", [f["invariant"] for f in out["findings"]])

    def test_upholding_marks_the_loser_and_still_does_not_rewrite_it(self):
        a, b = self.two_cards()
        self.g("close", "--id", a, "--verdict", "won")
        rc, out, _ = self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        did = out["dispute"]
        rc, _, _ = self.g("resolve", "--id", did, "--outcome", "upheld",
                          "--note", "re-measured at T2 on the same corpus, 3 seeds")
        self.assertEqual(rc, 0)
        loser = self.card(a)
        self.assertEqual(loser["verdict"], "won",
                         "superseded is a forward pointer, not an erasure — the next "
                         "round needs to know what was concluded AND that it fell")
        self.assertEqual(loser["superseded_by"], b)

    def test_a_resolution_must_say_why(self):
        a, b = self.two_cards()
        rc, out, _ = self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        rc, _, err = run_script(SCRIPT, "resolve", "--id", out["dispute"],
                                "--outcome", "rejected", "--project", self.tmp)
        self.assertEqual(rc, 2, "the outcome alone does not say why, and why is the "
                                "only part the next round can check")

    def test_a_resolved_dispute_stays_resolved(self):
        a, b = self.two_cards()
        rc, out, _ = self.g("dispute", "--id", b, "--against", a, "--detail", "x")
        did = out["dispute"]
        self.g("resolve", "--id", did, "--outcome", "rejected", "--note", "different gate")
        rc, _, _ = self.g("resolve", "--id", did, "--outcome", "upheld", "--note", "n")
        self.assertEqual(rc, 1, "a reopened dispute is a NEW dispute citing this one")


class ABindingResolvesBothWays(GraphCase):
    """run-mechanics.md -> "Record integrity"; ARA (arXiv:2604.24658) ->
    research-manager "Forensic Binding Checklist" and its D2 dimension.

    `run.json -> hypothesis` is free text that run-mechanics says tools must not
    require, so a run states an expectation and nothing ever closes it. The
    optional `verifies` sibling names the card and what would refute it — and
    both halves are checked, because a one-way pointer reads exactly like a
    binding until somebody follows it, and a criterion nobody can execute passes
    every run.
    """

    def arm(self, verifies="auto", falsified="val_loss at ep20 not 0.01 below base"):
        nid = self.add_complete()
        run_id = "run_2026_" + nid
        self.run_it(nid, run_id=run_id)
        rec = {"run_id": run_id, "hypothesis": "warmup should hold lr=3e-4"}
        if verifies is not None:
            card = (f"stages/exploration/graph.json#{nid}"
                    if verifies == "auto" else verifies)
            rec["verifies"] = {"card": card, "criterion": "val_loss at ep20",
                               "falsified_if": falsified}
        self.write_json(os.path.join("stages", "training", "runs", run_id, "run.json"), rec)
        return nid

    def invariants(self):
        rc, out, _ = self.g("check")
        return [f["invariant"] for f in out["findings"]], out

    def test_a_resolving_binding_is_clean(self):
        self.arm()
        inv, _ = self.invariants()
        self.assertNotIn("binding_unresolved", inv)

    def test_a_run_pointing_at_a_different_card_is_critical(self):
        self.arm(verifies="stages/exploration/graph.json#N99")
        inv, out = self.invariants()
        self.assertIn("binding_unresolved", inv)
        self.assertIn("critical", [f["severity"] for f in out["findings"]
                                   if f["invariant"] == "binding_unresolved"])

    def test_verifies_without_a_falsification_criterion_is_critical(self):
        self.arm(falsified="")
        inv, out = self.invariants()
        self.assertIn("binding_unresolved", inv)
        self.assertIn("wish", " ".join(f["detail"] for f in out["findings"]))

    def test_a_tautology_is_not_a_criterion(self):
        """ARA D2 non-triviality: 'if the method does not work' is trivial."""
        self.arm(falsified="if the change does not help")
        inv, out = self.invariants()
        self.assertIn("binding_unresolved", inv)
        self.assertIn("execute", " ".join(f["detail"] for f in out["findings"]))

    def test_naming_the_metric_without_a_number_is_accepted(self):
        self.arm(falsified="val_loss fails to fall relative to the base run")
        inv, _ = self.invariants()
        self.assertNotIn("binding_unresolved", inv,
                         "a criterion an independent reader can execute need not "
                         "carry a threshold in the sentence")

    def test_pending_is_a_legitimate_binding(self):
        self.arm(verifies="[pending]")
        inv, out = self.invariants()
        crit = [f for f in out["findings"]
                if f["invariant"] == "binding_unresolved" and f["severity"] == "critical"]
        self.assertEqual(crit, [], "an impossible binding written down beats a guessed one")

    def test_a_run_with_no_verifies_is_a_minor_note_not_a_failure(self):
        self.arm(verifies=None)
        inv, out = self.invariants()
        sev = [f["severity"] for f in out["findings"] if f["invariant"] == "binding_unresolved"]
        self.assertEqual(sev, ["minor"],
                         "absent is normal — a run that claimed nothing is honest, and "
                         "different from one that claimed something nobody closed")

    def test_a_run_staged_on_another_machine_says_nothing_about_the_binding(self):
        nid = self.add_complete()
        self.run_it(nid, run_id="run_elsewhere")
        inv, _ = self.invariants()
        self.assertNotIn("binding_unresolved", inv,
                         "absent and broken are different facts; reporting the first as "
                         "the second trains the reader to ignore the finding")

    def test_an_unreadable_run_record_is_unverifiable_not_absent(self):
        nid = self.add_complete()
        self.run_it(nid, run_id="run_broken")
        self.write(os.path.join("stages", "training", "runs", "run_broken", "run.json"),
                   "{ this is not json")
        inv, out = self.invariants()
        self.assertIn("binding_unresolved", inv)
        self.assertIn("unverifiable", " ".join(f["detail"] for f in out["findings"]))


class TheChangeThatRanIsTheChangeDeclared(GraphCase):
    """SKILL.md -> the run-card hard rule 1: delta is COMPUTED, not described.

    Evidenced by a real round (e2e_3D_detection, 2026-08-14): a baseline at AP50
    26.76 was read against a new arm at 7.88 and the gap credited to the
    technique. Four unintended differences sat between them — rot_aug_deg 0->15,
    num_points 40000->145000, no_thin_cloud on->off, warm_lr_epochs 9->4 — plus
    8 GPUs down to 1. Prose said one thing; the config said five.
    """

    def armed(self, declared, varied, parent_wl=None, wl=None):
        base = self.add_complete()
        self.run_it(base, run_id="run_base")
        rec = {"run_id": "run_base"}
        if parent_wl: rec["workload"] = parent_wl
        self.write_json(os.path.join("stages", "training", "runs", "run_base", "run.json"), rec)
        self.fill(base, tier="T1"); self.g("close", "--id", base, "--verdict", "won")
        nid = self.add_complete()
        self.g("set", "--id", nid, "--set", f"parent={base}",
               "--set", "delta=" + json.dumps(declared))
        self.run_it(nid, run_id="run_arm")
        arm = {"run_id": "run_arm", "lineage": {"variation_summary": varied}}
        if wl: arm["workload"] = wl
        self.write_json(os.path.join("stages", "training", "runs", "run_arm", "run.json"), arm)
        return nid

    def invariants(self):
        rc, out, _ = self.g("check")
        return [f["invariant"] for f in out["findings"]], out

    def test_one_declared_key_and_one_actual_key_is_clean(self):
        self.armed({"cdn": True}, {"cdn": True})
        inv, _ = self.invariants()
        self.assertNotIn("delta_not_as_declared", inv)

    def test_an_undeclared_key_that_also_moved_is_critical(self):
        self.armed({"cdn": True}, {"cdn": True, "rot_aug_deg": 15.0})
        inv, out = self.invariants()
        self.assertIn("delta_not_as_declared", inv)
        self.assertIn("rot_aug_deg", " ".join(f["detail"] for f in out["findings"]))

    def test_a_declared_key_the_run_never_varied_is_flagged(self):
        self.armed({"cdn": True}, {})
        inv, out = self.invariants()
        self.assertIn("delta_not_as_declared", inv)
        self.assertIn("dropped", " ".join(f["detail"] for f in out["findings"]))

    def test_the_gpu_count_is_caught_even_though_variation_summary_cannot_see_it(self):
        """The half a variation_summary-only check would pass while missing.

        world_size lives in `workload`, not `runtime_params`. A guard that read
        only the first would have cleared the round it exists to prevent — a
        guard reporting the very conclusion it was built to stop.
        """
        self.armed({"cdn": True}, {"cdn": True},
                   parent_wl={"world_size": 8}, wl={"world_size": 1})
        inv, out = self.invariants()
        self.assertIn("delta_not_as_declared", inv)
        detail = " ".join(f["detail"] for f in out["findings"])
        self.assertIn("world_size", detail)
        self.assertIn("variation_summary", detail)

    def test_a_card_declaring_no_delta_is_not_second_guessed(self):
        base = self.add_complete()
        self.run_it(base, run_id="run_x")
        self.write_json(os.path.join("stages", "training", "runs", "run_x", "run.json"),
                        {"run_id": "run_x", "lineage": {"variation_summary": {"a": 1}}})
        inv, _ = self.invariants()
        self.assertNotIn("delta_not_as_declared", inv,
                         "a measurement card has no single-key delta to declare")


class OneNumberCannotDescribeTwoSettings(GraphCase):
    """SKILL.md -> Stage 6; evidenced by the same round.

    One-to-one matching needs no NMS, and it was being compared against
    one-to-many UNDER NMS. Holding the setting fixed makes the contrast clean and
    at the same time measures the setting rather than the technique — said out
    loud only after the comparison had been read once: "one-to-one HAS to be measured
    with nms off".

    The resolution is the rule: hold it fixed for the contrast AND re-evaluate at
    the arm's own setting. The second costs no training and is the only number
    that describes what would ship. That arm read 84.16 held, 92.15 at its own.
    """

    def arm_that_changes_the_setting(self, result):
        nid = self.add_complete()
        self.g("set", "--id", nid, "--set",
               'eval_setting={"held_at":"test_nms=on","own":"test_nms=off"}')
        self.run_it(nid, run_id="run_e13h")
        self.g("fill", "--id", nid, "--result", json.dumps(result), "--tier", "T2")
        return nid

    def invariants(self):
        rc, out, _ = self.g("check")
        return [f["invariant"] for f in out["findings"]], out

    def test_a_single_number_is_refused(self):
        self.arm_that_changes_the_setting({"AP50": 84.16})
        inv, out = self.invariants()
        self.assertIn("one_number_two_settings", inv)
        self.assertIn("FREE", " ".join(f["detail"] for f in out["findings"]))

    def test_both_numbers_pass(self):
        self.arm_that_changes_the_setting({"at_held": 84.16, "at_own": 92.15})
        inv, _ = self.invariants()
        self.assertNotIn("one_number_two_settings", inv)

    def test_an_arm_that_does_not_move_the_setting_needs_only_one(self):
        nid = self.add_complete()
        self.run_it(nid, run_id="run_plain")
        self.g("fill", "--id", nid, "--result", '{"AP50": 88.0}', "--tier", "T2")
        inv, _ = self.invariants()
        self.assertNotIn("one_number_two_settings", inv)


class ANumberIsNotAConclusion(GraphCase):
    """references/experiment-graph.md -> §2, at the moment it is hardest to honour.

    "So what new conclusions are there?" was asked roughly fifteen times across the six days
    of the round this skill came from. It is the moment that most tempts the
    filled/closed collapse: an arm has finished, its numbers are on the card, and
    the natural sentence reports the number as a conclusion. Often the verdict
    genuinely is not reachable yet, because it waits on another arm.

    So `new` answers in two lists and labels the second one. A verb that merged
    them would be a convenience that undoes the state machine.
    """

    def settled(self, tier="T2"):
        nid = self.add_complete()
        self.run_it(nid, run_id="run_" + nid)
        self.fill(nid, tier=tier)
        self.g("close", "--id", nid, "--verdict", "won")
        return nid

    def unsettled(self, tier="T2"):
        nid = self.add_complete()
        self.run_it(nid, run_id="run_" + nid)
        self.fill(nid, tier=tier)
        return nid

    def test_a_result_without_a_verdict_is_not_listed_as_a_conclusion(self):
        c, u = self.settled(), self.unsettled()
        rc, out, _ = self.g("new")
        self.assertEqual(rc, 0)
        self.assertEqual([x["id"] for x in out["conclusions"]], [c])
        self.assertEqual([x["id"] for x in out["results_without_a_verdict"]], [u])

    def test_the_second_list_says_what_it_is(self):
        self.unsettled()
        rc, out, _ = self.g("new")
        self.assertIn("\u203c\ufe0f", out)
        self.assertIn("not", out["\u203c\ufe0f"].lower())
        self.assertIn("waiting", out["\u203c\ufe0f"].lower())

    def test_a_kill_is_a_conclusion_and_carries_its_revival(self):
        nid = self.unsettled(tier="T1")
        self.g("close", "--id", nid, "--killed-by", "share_too_small",
               "--revive-if", "re-measure on the night corpus")
        rc, out, _ = self.g("new")
        row = out["conclusions"][0]
        self.assertEqual(row["killed_by"], "share_too_small")
        self.assertTrue(row["revive_if"], "a kill with no revival is not a conclusion "
                                          "anybody can act on next round")

    def test_a_window_with_nothing_in_it_says_so_instead_of_reaching(self):
        self.settled()
        rc, out, _ = self.g("new", "--since", "2099-01-01T00:00:00+00:00")
        self.assertEqual(out["conclusions"], [])
        self.assertIn("real answer", out["note"],
                      "nothing happened is an answer; hunting for something to report "
                      "is how a filled card gets promoted")

    def test_disputes_show_up_as_events_too(self):
        a, b = self.settled(), self.unsettled()
        self.g("dispute", "--id", b, "--against", a, "--detail", "opposite sign")
        rc, out, _ = self.g("new")
        self.assertEqual(len(out["disputes_opened"]), 1)


class ASettledRoundLeavesSomethingToHandOver(GraphCase):
    """CLAUDE.md -> `/ara`, and the user's requirement that every round from
    here on produce one.

    `graph.json` is a MACHINE record. `verdict: won`, `killed_by:
    faithful_but_inert`, a card id — none of it is what a person reads six weeks
    later and none of it survives a handover. Until this fired, a round could
    close with every invariant green and leave behind a directory of runs.

    ‼️ Staleness counts as much as absence: an artifact built before the last arm
    settled describes a DIFFERENT round, and reads as current. Both facts are
    already recorded (`built_at` vs the settlement in `history`), so the check
    opens no network and walks no tree.
    """

    def _settled(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid)
        rc, out, err = self.g("close", "--id", nid, "--verdict", "won")
        self.assertEqual(rc, 0, f"close failed: {out or err}")
        return nid

    def _artifact(self, built_at):
        self.write_json(os.path.join("ara", "ara_20260101_000000", "ara.json"),
                        {"built_at": built_at, "layers": {"src": 1}})

    def _findings(self):
        rc, out, err = self.g("check")
        payload = out if isinstance(out, dict) else {}
        return [f for f in (payload.get("findings") or [])
                if f.get("invariant") == "artifact"]

    def test_a_settled_round_with_no_artifact_is_flagged(self):
        self._settled()
        blob = " ".join(f["detail"] for f in self._findings())
        self.assertIn("no artifact", blob)
        self.assertIn("/ara build", blob)

    def test_an_unsettled_round_is_not_nagged(self):
        """A gate that fires before there is anything to hand over is a gate
        people learn to ignore."""
        self.add_complete()
        self.assertEqual(self._findings(), [])

    def test_an_artifact_older_than_the_last_settlement_is_flagged(self):
        self._settled()
        self._artifact("2020-01-01T00:00:00+00:00")
        blob = " ".join(f["detail"] for f in self._findings())
        self.assertIn("different round", blob)

    def test_a_current_artifact_clears_it(self):
        self._settled()
        self._artifact("2099-01-01T00:00:00+00:00")
        self.assertEqual(self._findings(), [])

    def test_it_does_not_block_the_graph(self):
        """Major, not critical: a missing cover page must not stop the next arm.
        CLAUDE.md reserves the refusal for what makes the NEXT measurement wrong,
        and a round with no artifact measures fine — it just cannot be handed to
        anybody.

        Asserted on the finding's own severity rather than on the exit code,
        because this fixture legitimately trips an unrelated critical (no
        measured noise floor) and a test depending on the graph being otherwise
        clean would break every time a new invariant landed."""
        self._settled()
        self.assertTrue(self._findings(), "the artifact finding should be there")
        for f in self._findings():
            self.assertEqual(f["severity"], "major",
                             "a missing artifact is a worklist item, not a refusal")

    def test_a_round_with_arms_still_open_is_not_nagged(self):
        """‼️ `SETTLED` is per-CARD, and this used to key on "some card settled" --
        so it fired the moment the FIRST of five arms landed, telling a round that
        is mid-flight to go build its handover document. Same defect as
        `test_an_unsettled_round_is_not_nagged` guards against, one card later,
        and the same cost: a gate that fires while the work is still running is a
        gate people learn to skip."""
        self._settled()
        self.add_complete()
        self.assertEqual(self._findings(), [])

    def test_a_stale_artifact_mid_round_is_a_note_not_a_handover_item(self):
        """Staleness is still true mid-round -- an artifact that reads as current
        while a card settled past it is wrong NOW -- so it is said, at the
        severity of a note. It becomes a handover item when the round closes."""
        self._settled()
        self.add_complete()
        self._artifact("2020-01-01T00:00:00+00:00")
        found = self._findings()
        self.assertTrue(found, "a stale artifact is still worth saying")
        for f in found:
            self.assertEqual(f["severity"], "minor")

class AnHonestNoiseFloorMustBeWritable(GraphCase):
    """lifecycle/exploration/baseline.json -> `_comment_value`, and CLAUDE.md ->
    "Never record a metric you did not read" through `graph.py -> _grounding`.

    The floor is a SPREAD between two repeat measurements, so no log anywhere
    prints it. Grounding demanded every source quote the value, which no honest
    citation of the endpoints can do — so the only cheap way to pass `check` was
    to invent a quote like "31.09 - 28.16 = 2.93", which IS the fabrication
    grounding exists to catch. A record layer whose sole passing path is the
    forbidden one teaches its reader to fake the field, and the next field faked
    is one nobody is watching. `derived` is the honest path; these tests hold it
    open AND hold it shut as an escape hatch.
    """

    RULER = {"ref": "stages/exploration/scripts/seed_floor.py",
             "command": "python stages/exploration/scripts/seed_floor.py",
             "quote": "mAP0.85  e135h 31.09  e135i 28.16  spread 2.93",
             "kind": "derived"}
    E1 = {"ref": "runs/e135h/eval.log:12",
          "quote": "Test model; Metrics mAP0.85: 31.09", "kind": "result"}
    E2 = {"ref": "runs/e135i/eval.log:12",
          "quote": "Test model; Metrics mAP0.85: 28.16", "kind": "result"}

    def _floor(self, **over):
        b = _template("baseline.json")
        b.update({"value": 2.93, "unit": "AP", "metric": "mAP0.85"})
        b.update(over)
        self.write_json(os.path.join("stages", "exploration", "baseline.json"), b)
        rc, out, _ = self.g("check")
        return [f for f in out["findings"] if f["invariant"] == "ungrounded_number"]

    def test_citing_only_the_endpoints_cannot_ground_a_spread(self):
        """The reported symptom. Kept as a test because it is the honest attempt:
        both logs are real, both were open, and neither contains the difference.
        It must fail — but the failure has to have somewhere to go, which is what
        the next test asserts."""
        bad = self._floor(sources=[self.E1, self.E2])
        self.assertEqual(len(bad), 2, "each endpoint fails to attest the spread")

    def test_a_ruler_plus_its_endpoints_is_clean(self):
        self.assertEqual(self._floor(sources=[self.E1, self.E2, self.RULER]), [])

    def test_the_ruler_alone_is_clean_too(self):
        self.assertEqual(self._floor(sources=[self.RULER]), [])

    def test_derived_does_not_excuse_the_derived_source_itself(self):
        """‼️ The escape hatch, and the reason `derived` moves the digit check
        rather than dropping it: if a `derived` source were exempt too, the kind
        would be a one-word licence to write any number against any path."""
        ruler = dict(self.RULER, quote="spread computed, see above")
        bad = self._floor(sources=[self.E1, self.E2, ruler])
        self.assertTrue(any("does not contain" in f["detail"] for f in bad),
                        "the derivation must still print its own answer")

    def test_a_derivation_nobody_can_rerun_is_refused(self):
        ruler = dict(self.RULER)
        ruler.pop("command")
        bad = self._floor(sources=[self.E1, self.E2, ruler])
        self.assertTrue(any("command" in f["detail"] for f in bad))
        self.assertTrue(all(f["severity"] == "critical" for f in bad))

    def test_a_transcribed_number_is_unaffected(self):
        """No `derived` source present -> every source attests, exactly as before.
        The relaxation must not reach the ordinary case, which is most of them."""
        b = _template("state.json")
        b["constants"] = [{"name": "neg_share", "value": 0.0462,
                           "sources": [{"ref": "logs/scan.txt:214",
                                        "quote": "G>=52 frames: 254/5495 (4.62%)",
                                        "kind": "result"},
                                       {"ref": "logs/other.txt:3",
                                        "quote": "no number here", "kind": "result"}]}]
        self.write_json(os.path.join("stages", "exploration", "state.json"), b)
        rc, out, _ = self.g("check")
        bad = [f for f in out["findings"] if f["invariant"] == "ungrounded_number"]
        self.assertTrue(any("does not contain" in f["detail"] for f in bad),
                        "a second source that attests nothing is still a finding")


class StatusAndReadyAnswerTheSameQuestion(GraphCase):
    """references/experiment-graph.md -> TAKE, and `graph.py -> _derive_state`.

    ⬜🟨🟩 are a function of the card plus its dependencies; 🔵🟪✅❌ are acts
    somebody performed. Storing the first three and recomputing them elsewhere is
    CLAUDE.md's DOUBLE PROTOCOL inside one file: `status` read the stored label and `ready`
    recomputed, so three complete cards with no dependencies were reported
    `blocked: 3` and handed out as `ready: [N01, N02, N03]` at the same instant —
    and `status` is the one a person reads, so the round looks stalled.

    Written and read by the NEXT round, which is the bar in CLAUDE.md ->
    "Conventions" for what earns a check.
    """

    def test_a_complete_card_with_no_dependencies_is_ready_not_blocked(self):
        self.add_complete()
        rc, ready, _ = self.g("ready")
        rc, status, _ = self.g("status")
        self.assertEqual(len(ready["ready"]), 1)
        self.assertEqual(status["counts"]["ready"], 1,
                         "the summary a person reads must not say the queue is stalled")
        self.assertEqual(status["counts"]["blocked"], 0)

    def test_blocked_means_what_the_schema_says_it_means(self):
        """`blocked` 🟨 is defined as "card complete, dependencies unmet". A card
        with `depends_on: []` cannot be blocked on anything, so labelling it so
        contradicts the vocabulary the next reader is handed."""
        nid = self.add_complete()
        self.assertEqual(self.card(nid)["state"], "ready")

    def test_an_unmet_dependency_is_what_blocked_is_for(self):
        first = self.add_complete()
        second = self.add_complete()
        self.g("set", "--id", second, "--set", f'depends_on=["{first}"]')
        rc, status, _ = self.g("status")
        self.assertEqual(status["counts"]["blocked"], 1)
        self.assertEqual(status["counts"]["ready"], 1)
        rc, ready, _ = self.g("ready")
        self.assertEqual([b["id"] for b in ready["blocked"]], [second])
        self.assertEqual(ready["blocked"][0]["state"], "blocked")

    def test_an_incomplete_card_is_draft_in_both_verbs(self):
        rc, out, _ = self.g("add", "--title", "t", "--kind", "port")
        rc, status, _ = self.g("status")
        self.assertEqual(status["counts"]["draft"], 1)
        rc, ready, _ = self.g("ready")
        self.assertEqual(ready["blocked"][0]["state"], "draft",
                         "'finish the card' and 'wait for N01' are different instructions")

    def test_a_stored_label_behind_its_derivation_is_reported_not_hidden(self):
        """blocked -> ready fires when ANOTHER card settles and no `set` runs on
        this one, so drift is normal. It is reported because the same field is
        what a hand-edited graph corrupts, and repairing it silently would hide
        both."""
        first = self.add_complete()
        second = self.add_complete()
        self.g("set", "--id", second, "--set", f'depends_on=["{first}"]')
        self.run_it(first)
        self.fill(first)
        self.g("close", "--id", first, "--verdict", "won")
        self.assertEqual(self.card(second)["state"], "blocked", "the stored label is stale")
        rc, status, _ = self.g("status")
        self.assertEqual(status["counts"]["ready"], 1, "the derivation is the truth")
        self.assertEqual([d["id"] for d in status["state_drift"]], [second])


class AnArmIsItsRunIdNotItsLabel(GraphCase):
    """`graph.py -> _derive_state`, and invariant 4 (`duplicate_arm`).

    The dangerous half of the same drift. `ready` skipped 🔵 by the stored label
    and `duplicate_arm` only walked cards labelled `running`, so a card whose
    `run_id` was set while the label was left behind stayed in the ready set —
    and the one invariant meant to catch the second arm being opened on it was
    walking past it. A `run_id` is set at the moment an arm opens; the label is
    the part a caller forgets.
    """

    def _armed_without_the_label(self):
        nid = self.add_complete()
        g = self.graph()
        for n in g["nodes"]:
            if n["id"] == nid:
                n["run_id"] = "run_20260817_000000"
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        return nid

    def test_a_card_with_an_open_arm_leaves_the_ready_set(self):
        nid = self._armed_without_the_label()
        rc, out, _ = self.g("ready")
        self.assertEqual([r["id"] for r in out["ready"]], [],
                         "a second arm on the same card is duplicated GPU-weeks")
        self.assertEqual(out["running"], [nid])

    def test_duplicate_arm_still_sees_it(self):
        a = self._armed_without_the_label()
        b = self.add_complete()
        self.run_it(b, run_id="run_20260817_111111")
        rc, out, _ = self.g("check")
        dupes = [f for f in out["findings"] if f["invariant"] == "duplicate_arm"]
        self.assertTrue(dupes, "same parent and delta, one label missing")

    def test_a_settled_card_is_not_dragged_back_to_running(self):
        """The derivation is by content, so its ORDER matters: every filled and
        closed card carries a `run_id` too."""
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid)
        rc, status, _ = self.g("status")
        self.assertEqual(status["counts"]["filled"], 1)
        self.assertEqual(status["counts"]["running"], 0)

class AFloorFromAnotherCorpusIsNotAFloor(GraphCase):
    """`baseline.json -> _comment_measured_on` ("Must equal `graph.json -> corpus`"),
    `_comment_runs` ("`graph.py check` re-reads their `run.json` and refuses a floor
    whose runs disagree on `mode`, `scope`, or dataset identity") and CLAUDE.md ->
    "Never silently": 「Never let a share measured somewhere else stand for this
    corpus」, one level up.

    ‼️ Every one of those was written and NONE was enforced: `runs`, `measured_on`
    and `measured_at` had no reader anywhere, so `check` asked the floor exactly
    two questions — is it grounded, and is it null. The asymmetry is the defect:
    `_share_scope` kills a CARD whose share came from another corpus, while a
    FLOOR from another corpus was accepted in silence and went on gating the
    wording of every result in the round. The floor is the more expensive side to
    be wrong on, being the thing every later verdict rests on.

    A retired floor gates identically to an absent one because it IS the same
    fact, which `retires_on: [..., dataset_snapshot]` already said in the record.
    """

    RUN = {"mode": "production", "scope": {"samples": 500, "dataset": "boxes"}}

    def _floor(self, **over):
        b = _template("baseline.json")
        b.update({"value": 2.93, "unit": "AP", "metric": "mAP0.85",
                  "runs": ["run_a", "run_b"],
                  "measured_on": {"dataset_id": CORPUS["dataset_id"],
                                  "snapshot": CORPUS["snapshot"]},
                  "sources": [{"ref": "scripts/seed_floor.py", "command": "python x.py",
                               "quote": "spread 2.93", "kind": "derived"}]})
        b.update(over)
        self.write_json(os.path.join("stages", "exploration", "baseline.json"), b)

    def _runs(self, a=None, b=None):
        for rid, rec in (("run_a", a), ("run_b", b)):
            if rec is not None:
                self.write_json(os.path.join("stages", "evaluation", "runs", rid,
                                             "run.json"), rec)

    def _t2(self):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier="T2")

    def _find(self):
        rc, out, _ = self.g("check")
        return {f["invariant"]: f for f in out["findings"]}

    def test_a_matching_corpus_with_two_agreeing_runs_is_clean(self):
        self._floor()
        self._runs(dict(self.RUN), dict(self.RUN))
        self._t2()
        f = self._find()
        self.assertNotIn("noise_floor_unusable", f)
        self.assertNotIn("hard_result_without_noise_floor", f)

    def test_a_floor_from_another_snapshot_is_treated_as_not_measured(self):
        self._floor(measured_on={"dataset_id": "boxes", "snapshot": "260601"})
        self._runs(dict(self.RUN), dict(self.RUN))
        self._t2()
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "critical")
        self.assertIn("RETIRED", f["noise_floor_unusable"]["detail"])
        self.assertIn("hard_result_without_noise_floor", f,
                      "a retired floor must gate exactly like an absent one")

    def test_a_floor_with_no_corpus_at_all_is_treated_as_not_measured(self):
        self._floor(measured_on={"dataset_id": None, "snapshot": None})
        self._runs(dict(self.RUN), dict(self.RUN))
        self._t2()
        self.assertIn("hard_result_without_noise_floor", self._find())

    def test_one_run_cannot_produce_a_spread(self):
        self._floor(runs=["run_a"])
        self._runs(dict(self.RUN))
        self._t2()
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "critical")
        self.assertIn("SPREAD", f["noise_floor_unusable"]["detail"])

    def test_runs_disagreeing_on_mode_are_not_measuring_noise(self):
        self._floor()
        self._runs(dict(self.RUN), dict(self.RUN, mode="debug"))
        self._t2()
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "critical")
        self.assertIn("mode", f["noise_floor_unusable"]["detail"])
        self.assertIn("hard_result_without_noise_floor", f)

    def test_runs_disagreeing_on_scope_are_not_measuring_noise(self):
        self._floor()
        self._runs(dict(self.RUN), dict(self.RUN, scope={"samples": 20,
                                                        "dataset": "boxes"}))
        self._t2()
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "critical")
        self.assertIn("scope", f["noise_floor_unusable"]["detail"])

    def test_a_run_record_that_could_not_be_read_is_its_own_fact(self):
        """‼️ CLAUDE.md -> "Never silently": 「Never report data you could not look
        at」. Unverified agreement is not agreement — but it is not a disagreement
        either, so it is a major that names what went unchecked, and the floor
        still stands."""
        self._floor()
        self._runs(dict(self.RUN))
        self._t2()
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "major")
        self.assertIn("run_b", f["noise_floor_unusable"]["detail"])
        self.assertIn("UNVERIFIED", f["noise_floor_unusable"]["detail"])
        self.assertNotIn("hard_result_without_noise_floor", f,
                         "could-not-look must not be reported as a void floor")

    def test_two_unrecorded_scopes_are_a_gap_not_a_verdict(self):
        self._floor()
        self._runs(dict(self.RUN, scope={}), dict(self.RUN, scope={}))
        self._t2()
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "major")
        self.assertNotIn("hard_result_without_noise_floor", f)

    def test_a_floor_older_than_the_corpus_is_stale(self):
        self._floor(measured_at="2026-07-01T00:00:00+00:00")
        self._runs(dict(self.RUN), dict(self.RUN))
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "major")
        self.assertIn("before this corpus was declared",
                      f["noise_floor_unusable"]["detail"])


class AnEdgeSaysWhatItBlocks(GraphCase):
    """references/experiment-graph.md -> TAKE, "an edge states what it blocks", and
    SKILL.md -> Stage 3, "running first is not reading first".

    A bare id could only say "wait", so every dependency that was really about
    ATTRIBUTION -- B's number cannot be read without A's -- was paid for as
    SCHEDULING, in GPU hours. The only way to parallelise was to delete the edge,
    which threw away the real half. These checks hold the two apart: the launch
    gate keeps its old strictness, and a `reading` edge buys the parallelism
    without letting the verdict travel unqualified.
    """

    def _pair(self, blocks=None):
        a, b = self.add_complete(), self.add_complete()
        dep = json.dumps([{"id": a, "blocks": blocks} if blocks else a])
        self.g("set", "--id", b, "--set", "depends_on=" + dep)
        return a, b

    def test_a_bare_id_still_blocks_the_launch(self):
        """The permissive default would silently unblock every graph already
        written. An untyped edge keeps meaning exactly what it meant."""
        a, b = self._pair()
        rc, out, _ = self.g("ready")
        self.assertEqual([r["id"] for r in out["ready"]], [a])
        self.assertEqual([x["id"] for x in out["blocked"]], [b])

    def test_a_blocked_card_is_asked_which_kind_of_edge_it_is(self):
        """The retype prompt fires at the one moment the edge costs something.
        Without it the wait reads as a fact of nature and nobody re-examines it."""
        a, b = self._pair()
        rc, out, _ = self.g("ready")
        self.assertIn("ask", out["blocked"][0])
        self.assertIn("reading", out["blocked"][0]["ask"])

    def test_a_reading_edge_does_not_hold_the_arm(self):
        """The whole point: N07 needed N06's sigma, not N06's verdict."""
        a, b = self._pair("reading")
        rc, out, _ = self.g("ready")
        self.assertEqual(sorted(r["id"] for r in out["ready"]), sorted([a, b]))
        self.assertEqual(out["blocked"], [])

    def test_a_verdict_ahead_of_what_it_rests_on_is_stamped_not_refused(self):
        """Refusing `close` would move the same stall one state right: a pile of
        filled cards nobody may adjudicate is the same waiting, minus the GPU
        hours. What must not happen is the verdict travelling without its clause."""
        a, b = self._pair("reading")
        self.run_it(b)
        self.fill(b)
        rc, out, _ = self.g("close", "--id", b, "--verdict", "won")
        self.assertEqual(rc, 0)
        self.assertEqual(out["conditional_on"], [a])
        self.assertEqual(self.card(b)["conditional_on"], [a])
        rc, st, _ = self.g("status")
        self.assertEqual([c["id"] for c in st["conditional_verdicts"]], [b],
                         "the screen the sentence gets quoted off must carry the clause")

    def test_an_open_condition_is_reported_and_a_landed_one_escalates(self):
        """Nothing clears `conditional_on` on its own, which is the failure this
        exists to catch: CLAUDE.md -> "Never silently", the re-reading rule."""
        a, b = self._pair("reading")
        self.run_it(b)
        self.fill(b)
        self.g("close", "--id", b, "--verdict", "won")
        rc, out, _ = self.g("check")
        f = {x["invariant"]: x for x in out["findings"] if x["card"] == b}
        self.assertEqual(f["verdict_is_conditional"]["severity"], "minor")
        self.assertNotIn("condition_resolved_unreviewed", f)

        self.run_it(a)
        self.fill(a)
        rc, closed, _ = self.g("close", "--id", a, "--verdict", "won")
        self.assertEqual(closed["re_read"], [b],
                         "closing the upstream must name the verdicts hanging on it")
        rc, out, _ = self.g("check")
        f = {x["invariant"]: x for x in out["findings"] if x["card"] == b}
        self.assertEqual(f["condition_resolved_unreviewed"]["severity"], "major")

    def test_two_cards_reading_each_other_are_not_a_deadlock(self):
        """"Run both, adjudicate together" has exactly the shape of a cycle. The
        old walk would have reported the healthiest use of the new kind as the
        thing you must break."""
        a, b = self._pair("reading")
        self.g("set", "--id", a, "--set",
               "depends_on=" + json.dumps([{"id": b, "blocks": "reading"}]))
        rc, out, _ = self.g("ready")
        self.assertEqual(sorted(r["id"] for r in out["ready"]), sorted([a, b]))
        self.assertNotIn("deadlock", out)

    def test_a_launch_cycle_is_still_a_deadlock(self):
        a, b = self._pair()
        self.g("set", "--id", a, "--set", "depends_on=" + json.dumps([b]))
        rc, out, _ = self.g("ready")
        self.assertEqual(out["deadlock"]["kind"], "cycle")

    def test_an_edge_whose_kind_nobody_can_read_is_refused(self):
        """It would gate like `launch`, so the symptom is a card that never
        becomes takeable and no stated reason."""
        a, b = self.add_complete(), self.add_complete()
        rc, out, err = self.g("set", "--id", b, "--set",
                              "depends_on=" + json.dumps([{"id": a, "blocks": "soon"}]))
        self.assertNotEqual(rc, 0)
        self.assertIn("blocks", json.dumps(out or err))

    def test_the_launch_gate_keeps_its_old_strictness(self):
        """A `reading` edge buys parallelism; it must not become a way to open an
        arm whose premise measurement has not landed."""
        a, b = self._pair()
        self.run_it(b)
        rc, out, _ = self.g("check")
        f = [x for x in out["findings"] if x["invariant"] == "gate_bypassed"]
        self.assertEqual([x["card"] for x in f], [b])
        self.assertEqual(f[0]["severity"], "critical")


class AFloorMeasuredBeforeMLClawMustBeWritable(GraphCase):
    """`baseline.json -> _comment_origin`, and `SKILL.md -> Where you come in`, whose own
    heading says users essentially never start from scratch: enter at any Stage, backfill
    what that Stage owes. The row for "it finished — is this improvement real" says the
    backfill is Stage 0 --
    the noise floor.

    ‼️ There was no way to write one. `runs` was the only door and it takes
    MLClaw run ids that `check` re-reads out of `stages/*/runs/<id>/run.json`,
    which a project taken over last week does not have: the floor was measured on
    a box since released, from a checkpoint on a powered-down disk, by a pipeline
    that never wrote a `run.json`. Measured on the code, the three writable forms
    ranked exactly backwards --

        honest    value + sources, `runs: []`            2 criticals, check REFUSES
        silent    value: null                            critical, every T2/T3 void
        invented  `runs: [two ids that do not resolve]`  one major, floor USABLE

    -- so the record layer paid for the invented one. The defect is not the
    missing feature, it is the INVERTED incentive, and the fix has to close both
    halves: an external floor is writable and worth what a claim is worth, and
    invented ids buy nothing it does not.
    """

    RUN = {"mode": "production", "scope": {"samples": 500, "dataset": "boxes"}}
    WHY = "measured on the released Nebius box before MLClaw; the ckpt disk is down"

    def _floor(self, **over):
        b = _template("baseline.json")
        b.update({"value": 2.93, "unit": "AP", "metric": "mAP0.85",
                  "measured_on": {"dataset_id": CORPUS["dataset_id"],
                                  "snapshot": CORPUS["snapshot"]},
                  "sources": [{"ref": "scripts/seed_floor.py", "command": "python x.py",
                               "quote": "spread 2.93", "kind": "derived"}]})
        b.update(over)
        self.write_json(os.path.join("stages", "exploration", "baseline.json"), b)

    def _external(self, **over):
        over.setdefault("origin", "external")
        over.setdefault("runs", [])
        over.setdefault("unchecked", self.WHY)
        self._floor(**over)

    def _card(self, tier):
        nid = self.add_complete()
        self.run_it(nid)
        self.fill(nid, tier=tier)
        return nid

    def _find(self):
        rc, out, _ = self.g("check")
        return {f["invariant"]: f for f in out["findings"]}

    def test_an_external_floor_is_writable_and_gates_t2(self):
        self._external()
        self._card("T2")
        f = self._find()
        self.assertNotIn("hard_result_without_noise_floor", f,
                         "an external floor that gates nothing is a door onto a wall")
        self.assertNotIn("t3_on_an_unverified_floor", f)
        self.assertEqual(f["noise_floor_is_a_claim"]["severity"], "minor",
                         "legitimate, and said out loud -- not silent, not a defect")

    def test_it_can_never_carry_a_t3(self):
        """T3 is the last check before a full run and the one tier whose row makes
        blind human review mandatory. A floor this pipeline did not measure on
        itself cannot hold that line -- promoting a soft number across it is the
        failure the whole ladder exists to stop."""
        self._external()
        self._card("T3")
        f = self._find()
        self.assertEqual(f["t3_on_an_unverified_floor"]["severity"], "critical")
        self.assertIn("claim", f["t3_on_an_unverified_floor"]["detail"])

    def test_without_unchecked_it_is_not_a_floor(self):
        """The door is narrow on purpose: what earns it is the sentence saying
        what nobody here could confirm and why it cannot be re-measured."""
        self._external(unchecked=None)
        self._card("T2")
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "critical")
        self.assertIn("unchecked", f["noise_floor_unusable"]["detail"])
        self.assertIn("hard_result_without_noise_floor", f)

    def test_a_number_nobody_can_quote_still_cannot_be_written(self):
        """‼️ `origin: external` is not an amnesty on grounding. The floor is the
        last number that may be typed from memory, and `_grounding` is what says
        so; an external door that also dropped `sources` would be the fabrication
        route the check was built to close."""
        self._external(sources=[])
        self._card("T2")
        self.assertIn("ungrounded_number", self._find())

    def test_it_still_has_to_be_this_corpus(self):
        """The corpus rule does NOT relax for an outside floor -- where a number
        was measured is precisely what an outside floor is least able to prove."""
        self._external(measured_on={"dataset_id": "boxes", "snapshot": "260601"})
        self._card("T2")
        f = self._find()
        self.assertIn("RETIRED", f["noise_floor_unusable"]["detail"])
        self.assertIn("hard_result_without_noise_floor", f)

    def test_naming_runs_as_well_is_two_accounts(self):
        self._external(runs=["run_a", "run_b"])
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "major")
        self.assertIn("two accounts", f["noise_floor_unusable"]["detail"])

    def test_invented_ids_buy_nothing_the_honest_door_does_not(self):
        """‼️ The regression that matters, and the reason the fix is two halves.

        This fixture -- `origin: mlclaw` with ids nothing can resolve -- was the
        only form of a pre-MLClaw floor that `check` let through, and it gated T2
        AND T3 on a single major while the honest form was refused outright.
        Reading an unreadable run record as `major` rather than as a void floor is
        still right (CLAUDE.md: 「Never report data you could not look at」 -- it is
        not agreement, but it is not disagreement either). What is not right is it
        outranking a declared external floor. It now gates exactly as far, and no
        further."""
        self._floor(runs=["wandb_run_aaa", "wandb_run_bbb"])
        self._card("T3")
        f = self._find()
        self.assertEqual(f["noise_floor_unusable"]["severity"], "major")
        self.assertIn("UNVERIFIED", f["noise_floor_unusable"]["detail"])
        self.assertIn("t3_on_an_unverified_floor", f,
                      "invented ids must not buy a tier the honest door cannot")

    def test_status_never_prints_the_floor_as_a_bare_number(self):
        """‼️ `status` is the one-screen summary a person reads and the screen the
        sentence gets quoted off. A `claim` floor printed as `0.25` is
        indistinguishable there from one this project measured -- the same
        promotion the tier ladder exists to stop, one layer out. CLAUDE.md: the
        qualifier travels with the number, in every file and every sentence."""
        self._external()
        rc, out, _ = self.g("status")
        self.assertEqual(rc, 0)
        floor = out["noise_floor"]
        self.assertEqual(floor["value"], 2.93)
        self.assertEqual(floor["origin"], "external")
        self.assertEqual(floor["status"], "claim")
        self.assertIn("never a T3", floor["gates"])
        self.assertEqual(floor["unchecked"], self.WHY)

    def test_status_says_measured_when_it_was_measured(self):
        self._floor(runs=["run_a", "run_b"])
        for rid in ("run_a", "run_b"):
            self.write_json(os.path.join("stages", "evaluation", "runs", rid,
                                         "run.json"), dict(self.RUN))
        rc, out, _ = self.g("status")
        self.assertEqual(out["noise_floor"]["status"], "verified")
        self.assertEqual(out["noise_floor"]["origin"], "mlclaw")

class AConditionIsRetiredOnTheRecord(GraphCase):
    """references/experiment-graph.md -> CLOSE, `conditional_on`; and §3.5's own reason
    for reporting a dispute as `major` rather than `critical` — a finding that stops the
    whole graph is one people learn to route around.

    `close` stamps `conditional_on` and nothing clears it automatically, which is the
    design. That left it with no way to be cleared at all — `set` refuses every settled
    card and a card carrying this field is settled by construction — so
    `condition_resolved_unreviewed` would report `major` for the rest of the round about
    a verdict somebody had already re-read and had no way to say so about. A permanently
    red check is not a strict check; it is a check with no readers.
    """

    def _conditional(self):
        a, b = self.add_complete(), self.add_complete()
        self.g("set", "--id", b, "--set",
               "depends_on=" + json.dumps([{"id": a, "blocks": "reading"}]))
        self.run_it(b)
        self.fill(b)
        self.g("close", "--id", b, "--verdict", "won")
        return a, b

    def test_a_condition_cannot_be_retired_while_it_is_still_open(self):
        """Re-reading a verdict against something that has not landed is not a
        re-reading. `check` calling it `minor` is already the correct state."""
        a, b = self._conditional()
        rc, out, err = self.g("reread", "--id", b, "--condition", a, "--note", "n")
        self.assertNotEqual(rc, 0)
        self.assertIn(b, [x["card"] for x in self.g("check")[1]["findings"]])
        self.assertEqual(self.card(b)["conditional_on"], [a], "nothing was retired")

    def test_retiring_it_records_who_looked_and_what_they_found(self):
        a, b = self._conditional()
        self.run_it(a)
        self.fill(a)
        self.g("close", "--id", a, "--verdict", "won")
        rc, out, _ = self.g("reread", "--id", b, "--condition", a,
                            "--note", "the sigma was calibrated; the delta stands")
        self.assertEqual(rc, 0)
        self.assertEqual(self.card(b)["conditional_on"], [])
        h = self.card(b)["history"][-1]
        self.assertEqual(h["reread"], a)
        self.assertEqual(h["note"], "the sigma was calibrated; the delta stands")
        self.assertEqual(h["condition_verdict"], "won",
                         "what it was re-read AGAINST is half the record")

    def test_the_major_finding_clears_only_by_that_route(self):
        a, b = self._conditional()
        self.run_it(a)
        self.fill(a)
        self.g("close", "--id", a, "--verdict", "won")
        before = [x["invariant"] for x in self.g("check")[1]["findings"] if x["card"] == b]
        self.assertIn("condition_resolved_unreviewed", before)
        self.g("reread", "--id", b, "--condition", a, "--note", "checked")
        after = [x["invariant"] for x in self.g("check")[1]["findings"] if x["card"] == b]
        self.assertNotIn("condition_resolved_unreviewed", after)
        self.assertNotIn("verdict_is_conditional", after)

    def test_the_field_has_one_author(self):
        """Two spellings for retiring a condition is how one of them stops recording
        anything. `set` refuses it even where the settled guard would not."""
        a, b = self._conditional()
        rc, out, err = self.g("set", "--id", b, "--set", "conditional_on=[]")
        self.assertEqual(rc, 1, "exit 2 would send the skill to hand-edit the field")
        self.assertEqual(self.card(b)["conditional_on"], [a])

    def test_retiring_a_condition_never_revises_the_verdict(self):
        """If re-reading changed the answer that is a `dispute`: the losing card KEEPS
        its verdict and gains `superseded_by`. An overturned conclusion and one that
        never existed are different information (§3.5)."""
        a, b = self._conditional()
        self.run_it(a)
        self.fill(a)
        self.g("close", "--id", a, "--verdict", "won")
        rc, out, _ = self.g("reread", "--id", b, "--condition", a, "--note", "n")
        self.assertEqual(self.card(b)["verdict"], "won")
        self.assertIn("dispute", out["note"])


class TwoArmsMayNotShareAWorkingTree(GraphCase):
    """references/experiment-graph.md -> §1.5 and §4, and CLAUDE.md -> "Never let a
    value have two authors" read on the code axis.

    SKILL.md Stage 3 says to compute the ready set and open all of it at once,
    while run-mechanics.md resolves ONE `code_dir` per stage. So parallel arms
    share a working tree by construction, and `code_snapshot.py` reads that tree
    at launch: with two ports half-written in it, arm A's `code_dirty.patch`
    carries arm B's edits.

    ‼️ Every other check on this graph is blind to it. `_delta` compares
    `runtime_params` + `workload`, and an uncommitted edit to a model file moves
    neither -- so the declared single-key delta is honoured, the patch applies,
    the run reproduces exactly, and the number belongs to a binary nobody
    described. That is the rubber-stamp shape: a guard reporting the conclusion
    it exists to exclude.
    """

    def _run(self, nid, started, dur=3600, dirty=0, branch=None, head=None):
        rec = {"run_id": "run_" + nid, "status": "completed",
               "started_at": started, "duration_s": dur,
               "code": {"branch": branch or ("explore/" + nid),
                        "origin_commit": head or ("c" * 40),
                        "dirty_files_count": dirty}}
        self.write_json(os.path.join("stages", "training", "runs",
                                     "run_" + nid, "run.json"), rec)

    def _two_arms(self):
        a, b = self.add_complete(), self.add_complete()
        self.run_it(a, run_id="run_" + a)
        self.run_it(b, run_id="run_" + b)
        return a, b

    def _hand_write_tree(self, ids, tree):
        """‼️ By hand, because `set` REFUSES to write two arms onto one branch --
        the collision is caught where it is written, which is better than
        reporting it a round later. `check` is the backstop for the graph that
        was edited around the tool, exactly as it is for `depends_on`, and that
        is the state this constructs."""
        g = self.graph()
        for n in g["nodes"]:
            if n["id"] in ids:
                if tree is None:
                    n.pop("tree", None)
                else:
                    n["tree"] = dict(tree)
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)

    def test_distinct_branches_off_the_round_base_are_clean(self):
        a, b = self._two_arms()
        self._run(a, "2026-08-17T01:00:00+00:00")
        self._run(b, "2026-08-17T01:30:00+00:00")
        rc, out, _ = self.g("check")
        names = [f["invariant"] for f in out["findings"]]
        self.assertNotIn("concurrent_arms_one_tree", names)
        self.assertNotIn("arm_tree_unrecorded", names)

    def test_overlapping_arms_on_one_branch_are_critical(self):
        a, b = self._two_arms()
        self._hand_write_tree((a, b), {"branch": "explore/shared", "base": BASE_SHA})
        self._run(a, "2026-08-17T01:00:00+00:00", branch="explore/shared")
        self._run(b, "2026-08-17T01:30:00+00:00", branch="explore/shared")
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("concurrent_arms_one_tree",
                      [f["invariant"] for f in out["findings"]])

    def test_silence_is_not_cheaper_than_the_truth(self):
        """‼️ The severity may not reward not writing the field down. The noise
        floor's four states were forced by exactly the opposite arrangement --
        the honest form cost two criticals and an invented reference cost one
        major -- and a record layer that pays a bonus to the least honest route
        gets what it pays for. Two arms naming one branch and two arms naming
        none are the same finding at the same severity; what clears it is two
        DISTINCT branches, which is also the only thing that fixes the round.
        """
        a, b = self._two_arms()
        self._hand_write_tree((a, b), None)
        self._run(a, "2026-08-17T01:00:00+00:00", branch=None)
        self._run(b, "2026-08-17T01:30:00+00:00", branch=None)
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1, "not recording the tree must not be the cheap route")
        sev = {f["invariant"]: f["severity"] for f in out["findings"]}
        self.assertEqual(sev.get("concurrent_arms_one_tree"), "critical")

    def test_arms_that_did_not_overlap_are_not_flagged(self):
        """One tree used SERIALLY is how this pipeline has always worked and is not
        a defect. The finding is about overlap, not about the directory."""
        a, b = self._two_arms()
        self.g("fill", "--id", a, "--result", '{"x":1}', "--tier", "T1")
        self.g("fill", "--id", b, "--result", '{"x":1}', "--tier", "T1")
        self._hand_write_tree((a, b), {"branch": "explore/shared", "base": BASE_SHA})
        self._run(a, "2026-08-17T01:00:00+00:00", dur=3600, branch="explore/shared")
        self._run(b, "2026-08-17T03:00:00+00:00", dur=3600, branch="explore/shared")
        rc, out, _ = self.g("check")
        self.assertNotIn("concurrent_arms_one_tree",
                         [f["invariant"] for f in out["findings"]])

    def test_an_unknown_overlap_is_a_third_answer(self):
        """A run record that is not here cannot say when the arm was open, and
        `unknown` is not `did not overlap` -- the same discipline `census.py`
        keeps between `gone` and `unreachable`."""
        a, b = self._two_arms()
        for nid in (a, b):
            self.g("fill", "--id", nid, "--result", '{"x":1}', "--tier", "T1")
        self._hand_write_tree((a, b), {"branch": "explore/shared", "base": BASE_SHA})
        g = self.graph()
        for n in g["nodes"]:            # no run record, and no `running` history
            n["history"] = [h for h in n["history"] if h.get("to") != "running"]
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        rc, out, _ = self.g("check")
        sev = {f["invariant"]: f["severity"] for f in out["findings"]}
        self.assertEqual(sev.get("concurrent_arms_one_tree"), "major")

    def test_an_arm_off_another_base_shares_no_control(self):
        """Stage 6: the control must be re-run on today's code. Once "today"
        differs per arm the sentence has no referent -- so this is `_share_scope`
        one axis over, and the same verdict: not weak evidence, a different
        question."""
        a = self.add_complete()
        self.run_it(a, run_id="run_" + a)
        self.g("set", "--id", a, "--set",
               'tree={"branch":"explore/x","base":"' + "b" * 40 + '"}')
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("arm_base_drift", [f["invariant"] for f in out["findings"]])

    def test_the_card_and_the_snapshot_must_agree(self):
        """Both ends, as invariant 13 does it -- and here the two ends have
        different authors. The card is written from intent before the arm opens;
        the snapshot is read off a disk at launch. When they disagree the disk is
        right, and the card describes a run that did not happen."""
        a = self.add_complete()
        self.run_it(a, run_id="run_" + a)
        self._run(a, "2026-08-17T01:00:00+00:00", branch="explore/somewhere-else")
        rc, out, _ = self.g("check")
        self.assertEqual(rc, 1)
        self.assertIn("arm_tree_disagrees_with_run",
                      [f["invariant"] for f in out["findings"]])

    def test_a_branch_name_is_as_unrecyclable_as_a_queue_number(self):
        """It is what a settled card's evidence resolves THROUGH. Pointing a
        second arm at it moves the ref, and the first card's `head` stops naming
        anything -- silently, because a reused branch and one never written read
        identically."""
        a, b = self.add_complete(), self.add_complete()
        rc, out, err = self.g("set", "--id", b, "--set",
                              'tree={"branch":"explore/' + a + '","base":"'
                              + BASE_SHA + '"}')
        self.assertEqual(rc, 1, "a policy refusal is exit 1 -- exit 2 tells the skill "
                                "to fall back and do it BY HAND, which here means "
                                "writing the collision in anyway")
        self.assertEqual((self.card(b).get("tree") or {}).get("branch"),
                         "explore/" + b)

    def test_a_measurement_card_needs_no_tree_of_its_own(self):
        """It reads the corpus or a checkpoint that already exists. Requiring a
        branch of it would put a second SHA into the round for nothing to
        reconcile."""
        rc, out, _ = self.g("add", "--title", "count the premise share",
                            "--kind", "measurement", "--criterion", "share",
                            "--guardrail", "n", "--kill-condition", "n/a")
        nid = out["id"]
        self.g("set", "--id", nid, "--set", "oracle_ceiling=1.2")
        self.run_it(nid, run_id="run_" + nid)
        rc, out, _ = self.g("check")
        self.assertNotIn(nid, [f["card"] for f in out["findings"]
                               if f["invariant"] == "arm_tree_unrecorded"])

    def test_the_claim_comes_before_a_line_of_code_not_at_launch(self):
        """‼️ The `run_id` gate is too late for the case it was built for. `run_id`
        is set at LAUNCH; the contamination happens in the hours before it, while
        two agents each edit for one card in one directory. A refusal then arrives
        after both have written into it, and declaring a tree at that point does
        not unmix the directory. Taking the work and taking the tree are one act.
        """
        a = self.add_complete()
        self._hand_write_tree((a,), None)
        rc, out, _ = self.g("claim", "--id", a, "--by", "agent-A")
        self.assertEqual(rc, 0)
        self.assertIn("git worktree add", out["cmd"])
        self.assertEqual(self.card(a)["tree"]["claimed_by"], "agent-A")
        self.assertIsNone(self.card(a).get("run_id"), "a claim is not a launch")

    def test_a_second_agent_is_told_the_card_is_taken(self):
        """The case an orchestrator cannot cover: two agents dispatched
        SEPARATELY, with no shared context and nothing in common but this file.
        ‼️ Its limit is worth stating with it -- two agents that never call
        `graph.py` cannot be protected by `graph.py`. What this buys is that the
        discipline sits at the one place both must pass through to take work."""
        a = self.add_complete()
        self._hand_write_tree((a,), None)
        self.g("claim", "--id", a, "--by", "agent-A")
        rc, out, _ = self.g("claim", "--id", a, "--by", "agent-B")
        self.assertEqual(rc, 1)
        self.assertIn("agent-A", out["refused"])
        rc, ready, _ = self.g("ready")
        taken = {r["id"]: r["claimed_by"] for r in ready["ready"]}
        self.assertEqual(taken[a], "agent-A",
                         "an unmarked ready set hands the same card out twice")

    def test_a_claim_while_somebody_is_in_the_shared_dir_is_refused(self):
        """A second claim while one code-writing card is unsettled and nameless is
        exactly the collision, so it is refused rather than noted. The fix names
        the other card, because the repair is to give THAT one a tree too."""
        a, b = self.add_complete(), self.add_complete()
        self._hand_write_tree((a, b), None)
        self.run_it(a, run_id="run_" + a)
        rc, out, _ = self.g("claim", "--id", b, "--by", "agent-B")
        self.assertEqual(rc, 1)
        self.assertIn(a, out["refused"])
        self.assertIn(a, out["fix"])

    def test_a_claim_needs_the_round_base_frozen(self):
        """This is the point where the base actually starts mattering: the
        worktree forks FROM it."""
        g = self.graph()
        g["base"] = {"commit": None, "repo_subdir": None, "declared_at": None}
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        a = self.add_complete()
        self._hand_write_tree((a,), None)
        rc, out, _ = self.g("claim", "--id", a)
        self.assertEqual(rc, 1)
        self.assertIn("base.commit", out["refused"])

    def test_a_measurement_card_is_refused_a_tree_rather_than_given_one(self):
        rc, out, _ = self.g("add", "--title", "count it", "--kind", "measurement",
                            "--criterion", "share", "--guardrail", "n",
                            "--kill-condition", "n/a")
        nid = out["id"]
        self.g("set", "--id", nid, "--set", "oracle_ceiling=1.2")
        rc, out, _ = self.g("claim", "--id", nid)
        self.assertEqual(rc, 1)
        self.assertIn("writes no code", out["refused"])

    def test_a_card_that_is_not_ready_cannot_be_claimed(self):
        """A claim on a card nobody can take reserves a tree for work that may
        never start -- and the branch name is unrecyclable."""
        rc, out, _ = self.g("add", "--title", "half a card", "--kind", "port",
                            "--criterion", "c", "--guardrail", "g",
                            "--kill-condition", "k")
        rc, out, _ = self.g("claim", "--id", out["id"])
        self.assertEqual(rc, 1)
        self.assertIn("not ready", out["refused"])

    def test_the_second_concurrent_arm_is_refused_at_the_moment_it_opens(self):
        """‼️ `check` reports this correctly and reports it LATE -- by then the GPU
        hours are spent and the number cannot be attributed to anything. The gate
        belongs at the act, which is the same layering `set` already uses for a
        malformed edge and a reused branch: refuse where it is written, keep
        `check` as the backstop for a graph edited around the tool.
        """
        a, b = self.add_complete(), self.add_complete()
        self._hand_write_tree((b,), None)
        self.run_it(a, run_id="run_" + a)
        rc, out, err = self.g("set", "--id", b, "--set", "run_id=run_" + b)
        self.assertEqual(rc, 1, "a policy refusal is exit 1; exit 2 would send the "
                                "skill to open the arm by hand")
        self.assertIsNone(self.card(b).get("run_id"))
        self.assertIn("worktree", json.dumps(out))

    def test_one_tree_used_serially_is_never_blocked(self):
        """This is how the pipeline has always worked and it is not a defect. The
        gate is about two arms at once, never about the directory."""
        a, b = self.add_complete(), self.add_complete()
        self._hand_write_tree((a, b), None)
        self.run_it(a, run_id="run_" + a)
        self.g("fill", "--id", a, "--result", '{"x":1}', "--tier", "T1")
        self.g("close", "--id", a, "--verdict", "won")
        rc, out, _ = self.g("set", "--id", b, "--set", "run_id=run_" + b)
        self.assertEqual(rc, 0, "a serial round must never meet this gate")

    def test_the_ready_set_hands_out_the_isolation_with_the_work(self):
        """The ready set is WHERE PARALLEL ARMS ARE HANDED OUT, so it is where the
        instruction belongs. At `check` time it arrives after the contamination."""
        a, b = self.add_complete(), self.add_complete()
        self._hand_write_tree((a, b), None)      # nothing claimed yet
        rc, out, _ = self.g("ready")
        self.assertEqual(len(out["trees"]), 2)
        self.assertIn("git worktree add", out["trees"][0]["cmd"])
        self.assertIn(BASE_SHA, out["trees"][0]["cmd"])
        self.assertIn("parallel_arms", out)

    def test_a_round_with_no_base_says_so_before_an_arm_opens(self):
        g = self.graph()
        g["base"] = {"commit": None, "repo_subdir": None, "declared_at": None}
        self.write_json(os.path.join("stages", "exploration", "graph.json"), g)
        a = self.add_complete()
        self._hand_write_tree((a,), None)
        rc, out, _ = self.g("ready")
        self.assertIn("base_undeclared", out)
        self.assertIn("declare base.commit first", out["trees"][0]["cmd"])

    def test_a_declared_transition_lands_in_the_history(self):
        """‼️ `prev` used to be read AFTER the set loop, so `--set state=running`
        compared `running` against `running` and appended nothing. The card then
        recorded when it became `ready` and when it was `filled`, and never when
        its ARM OPENED -- which is precisely the instant the overlap question is
        about."""
        a = self.add_complete()
        self.run_it(a, run_id="run_" + a)
        tos = [h.get("to") for h in self.card(a)["history"]]
        self.assertIn("running", tos)



if __name__ == "__main__":
    unittest.main()
