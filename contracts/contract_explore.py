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
    """SKILL.md -> Stage 3.5 rule 3: "近似版失败不能证伪原版" — a T4 exists to
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
    """references/experiment-graph.md -> §2: 🟪 已回填 and ✅ 已裁决 must stay
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
    """references/experiment-graph.md -> FILL: "不做传播的回填是这条流水线最贵的
    失败" — a result that invalidates another card's premise, unswept, leaves that
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
    """SKILL.md -> Stage 3: 「这一章是用户维护的执行队列，不是 agent 的记录本」.

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
        """SKILL.md Stage 3.5 rule 2: 便宜的检查能给你继续的理由，不能给你否掉的理由.

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


if __name__ == "__main__":
    unittest.main()
