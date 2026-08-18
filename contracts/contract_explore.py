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


if __name__ == "__main__":
    unittest.main()
