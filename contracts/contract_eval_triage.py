"""A bad case routed to the wrong owner makes two of the three problems worse.

Triage findings are the bar in CLAUDE.md -> "Contracts" twice over. They are a
record written now and read later by someone who can no longer verify it — the
person who receives "these 40 units need re-annotation" cannot re-derive whether
anybody actually looked at the images — and the action they trigger is expensive
and one-directional: a labeling round against the wrong pile costs weeks and
changes nothing.

The checks below are grouped by what would go wrong if the code drifted:

  * the amplification bug — an unreviewed or mislabeled pile routed as hard
    examples, which adds more of the annotation noise that produced the ranking
    (CLAUDE.md -> "Skills & Dependencies": `label_wrong` may never enter the
    hard-example pile)
  * one look asserting two — an agent's judgement recorded as `verified`
    (CLAUDE.md -> "Never silently": never let somebody's word become a checked
    fact — which does not exempt the agent)
  * a ranking invented rather than read — sorting with no declared direction, or
    an absent per-sample file read as "no bad cases" (CLAUDE.md -> "Never
    silently": never record a metric you did not read)
  * a finding nobody can address — a unit id that resolves to nothing, sent to a
    party whose manifest cannot look it up
"""
import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "eval-triage/triage.py"


class TriageCase(TempDirCase):
    """Builds an eval run plus its per-sample file by hand.

    These checks are about what `rank` and `route` will and will not accept, so
    the run record, the stage config and the per-sample dump all have to be
    settable to shapes a real pipeline takes an afternoon to reach.
    """

    RUN = "run_20260801_120000"

    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        os.makedirs(self.project, exist_ok=True)
        self.write_json("proj/project.json", {"name": "det"})
        self.declare_run()
        self.declare_output()
        self.write_samples()

    # -- fixtures ----------------------------------------------------------- #

    def declare_run(self, status="completed", parents=None):
        self.write_json(f"proj/stages/evaluation/runs/{self.RUN}/run.json", {
            "run_id": self.RUN, "stage": "evaluation", "status": status,
            "mode": "production", "scope": {"dataset": "boxes", "samples": 100},
            "lineage": {"parents": parents if parents is not None
                        else ["datasets/boxes@v1"]},
        })

    def declare_output(self, **over):
        ps = {"path": "per_sample.jsonl", "format": "jsonl", "records_at": None,
              "unit_key": "unit", "resolves_to": "unit_id",
              "score": {"field": "loss", "direction": "max"},
              "fields": ["pred", "gt"], "evidence": "eval.py:88"}
        ps.update(over)
        self.write_json("proj/stages/evaluation/output.json",
                        {"items": {}, "metrics": {"definitions": {}, "watch": []},
                         "per_sample": ps})

    def write_samples(self, n=5):
        lines = "".join(
            json.dumps({"unit": f"u{i}", "loss": 10.0 - i, "pred": "a", "gt": "b"}) + "\n"
            for i in range(n))
        self.write(f"proj/stages/evaluation/runs/{self.RUN}/output/per_sample.jsonl", lines)

    def freeze(self, units=("u0", "u1", "u2", "u3", "u4")):
        """A frozen snapshot so unit ids resolve. Its absence is not an error —
        it makes every case `unverifiable`, which is a different check below."""
        d = f"proj/datasets/boxes/snapshots/v1"
        self.write(f"{d}/manifest.jsonl",
                   json.dumps({"_manifest": {"count": len(units)}}) + "\n" +
                   "".join(json.dumps({"unit": u}) + "\n" for u in units))
        self.write_json(f"{d}/snapshot.json",
                        {"snapshot_id": "v1", "cite_as": "datasets/boxes@v1",
                         "dataset": "boxes"})

    # -- drivers ------------------------------------------------------------ #

    def rank(self, *extra):
        return run_script(SCRIPT, "rank", "--project", self.project,
                          "--run", self.RUN, "--json", *extra)

    def sid(self):
        code, out, err = self.rank("--name", "t1")
        self.assertEqual(code, 0, f"rank failed: {out} {err}")
        return "t1"

    def judge(self, unit, verdict, by, *extra):
        return run_script(SCRIPT, "judge", "--project", self.project,
                          "--run", self.RUN, "--session", "t1", "--unit", unit,
                          "--verdict", verdict, "--by", by,
                          "--basis", "looked at it", *extra)

    def confirm(self, unit, *extra):
        return run_script(SCRIPT, "confirm", "--project", self.project,
                          "--run", self.RUN, "--session", "t1", "--unit", unit,
                          "--basis", "second look", *extra)

    def route(self, *extra):
        return run_script(SCRIPT, "route", "--project", self.project,
                          "--run", self.RUN, "--session", "t1", *extra)

    def session(self):
        return self.read_json(
            f"proj/stages/evaluation/runs/{self.RUN}/triage/t1/session.json")


class Amplification(TriageCase):
    """CLAUDE.md -> "Skills & Dependencies": `label_wrong` may never enter the
    hard-example pile — feeding it back amplifies the annotation noise that
    ranked it in the first place.

    This is the failure the skill exists to prevent, so it is checked from both
    ends: routing before anybody looked, and a reviewed `label_wrong` unit
    reaching the pile that becomes more data.
    """

    def test_route_refuses_an_unreviewed_pile(self):
        self.freeze()
        self.sid()
        code, out, _ = self.route()
        self.assertEqual(code, 1, "an unreviewed pile must not route")
        self.assertIn("unreviewed", json.dumps(out))

    def test_label_wrong_never_enters_the_hard_example_pile(self):
        self.freeze()
        self.sid()
        self.judge("u0", "label_wrong", "agent")
        self.judge("u1", "sample_hard", "agent")
        for u in ("u2", "u3", "u4"):
            self.judge(u, "model_wrong", "agent")
        code, out, _ = self.route()
        self.assertEqual(code, 0, out)
        hard = out["routed"]["sample_hard"]["units"]
        self.assertNotIn("u0", hard,
                         "a wrong label routed as a hard example adds more of the "
                         "noise that put it at the top of the ranking")
        self.assertEqual(out["routed"]["label_wrong"]["units"], ["u0"])

    def test_the_exclusion_is_written_down_not_merely_omitted(self):
        """Whoever opens this record is looking for data to add. Something has to
        say which list is not it — an absent pile is indistinguishable from an
        empty one."""
        self.freeze()
        self.sid()
        for u in ("u0", "u1", "u2", "u3", "u4"):
            self.judge(u, "label_wrong", "agent")
        code, out, _ = self.route()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(out["not_hard_examples"]["units"]),
                         ["u0", "u1", "u2", "u3", "u4"])
        self.assertTrue(out["not_hard_examples"]["why"].strip())

    def test_route_refuses_a_disputed_case(self):
        self.freeze()
        self.sid()
        self.judge("u0", "label_wrong", "agent")
        self.judge("u0", "sample_hard", "agent", "--again")
        for u in ("u1", "u2", "u3", "u4"):
            self.judge(u, "model_wrong", "agent")
        code, out, _ = self.route()
        self.assertEqual(code, 1, "equal authority disagreeing has no tie-break")
        self.assertIn("disputed", json.dumps(out))


class OneLookIsNotTwo(TriageCase):
    """CLAUDE.md -> "Never silently": never let somebody's word become a checked
    fact — an answer is
    a claim until something OTHER than the person confirms it. The rule does not
    exempt the agent: one model's judgement is one source.
    """

    def test_a_single_agent_judgement_is_a_claim(self):
        self.freeze()
        self.sid()
        code, out, _ = self.judge("u0", "label_wrong", "agent")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["provenance"], "claim")

    def test_two_agent_passes_agreeing_stay_a_claim(self):
        """One source sampled twice. Their agreement measures the model's
        consistency, not the label."""
        self.freeze()
        self.sid()
        self.judge("u0", "label_wrong", "agent")
        code, out, _ = self.judge("u0", "label_wrong", "agent", "--again")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["provenance"], "claim",
                         "two passes of one model must not reach verified")
        self.assertTrue(any("agent_only" in c for c in out["caveats"]))

    def test_agent_plus_human_agreeing_is_verified(self):
        self.freeze()
        self.sid()
        self.judge("u0", "label_wrong", "agent")
        code, out, _ = self.confirm("u0", "--agree", "--by", "human")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["provenance"], "verified")

    def test_no_verb_can_write_verified_directly(self):
        """The provenance is derived. A flag that set it would let one look
        assert two, which is the substitution the rule exists to stop."""
        code, out, err = run_script(SCRIPT, "judge", "--project", self.project,
                                    "--run", self.RUN, "--session", "t1",
                                    "--unit", "u0", "--verdict", "label_wrong",
                                    "--by", "agent", "--basis", "x",
                                    "--provenance", "verified")
        self.assertEqual(code, 2, "there must be no --provenance flag")

    def test_a_human_overruling_the_agent_keeps_what_the_agent_said(self):
        """Whether the agent can be trusted on THIS dataset is answerable only
        from how often a person overruled it, and only if the overrules survive."""
        self.freeze()
        self.sid()
        self.judge("u0", "label_wrong", "agent")
        code, out, _ = self.confirm("u0", "--disagree", "--verdict", "model_wrong",
                                    "--by", "human")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["verdict"], "model_wrong")
        self.assertEqual(out["provenance"], "claim",
                         "one human holding a verdict the agent rejected is one "
                         "source, not two")
        self.assertTrue(any("label_wrong" in c for c in out["caveats"]),
                        f"the rejected verdict was dropped: {out['caveats']}")

    def test_confirm_refuses_when_nothing_has_been_judged(self):
        self.freeze()
        self.sid()
        code, out, _ = self.confirm("u0", "--agree", "--by", "human")
        self.assertEqual(code, 1, "confirmation with no first judgement is one "
                                 "look recorded as two")


class RankedNotInvented(TriageCase):
    """CLAUDE.md -> "Never silently": never record a metric you did not read, plus
    the `/eval-triage` entry's `score.direction` rule: which end means bad is never inferred from a
    field name, and a declared-but-absent file is not an empty one.
    """

    def test_no_declaration_refuses_and_routes_to_eval_init(self):
        self.declare_output(path=None)
        code, out, _ = self.rank()
        self.assertEqual(code, 1, "nothing to rank is a refusal, not an empty pile")
        self.assertIn("eval-init", json.dumps(out))

    def test_missing_direction_refuses(self):
        self.declare_output(score={"field": "loss", "direction": None})
        code, out, _ = self.rank()
        self.assertEqual(code, 1,
                         "sorting the wrong way yields the model's best "
                         "predictions reviewed as its worst, and nothing errors")

    def test_declared_but_absent_file_is_refused_not_treated_as_no_bad_cases(self):
        os.remove(self.path(
            f"proj/stages/evaluation/runs/{self.RUN}/output/per_sample.jsonl"))
        code, out, _ = self.rank()
        self.assertEqual(code, 1)
        self.assertIn("not on disk", json.dumps(out))

    def test_direction_decides_which_end_is_ranked_first(self):
        self.freeze()
        code, out, _ = self.rank("--name", "hi")
        self.assertEqual(out["cases"][0]["unit"], "u0", "max: highest loss first")
        self.declare_output(score={"field": "loss", "direction": "min"})
        code, out, _ = self.rank("--name", "lo")
        self.assertEqual(out["cases"][0]["unit"], "u4", "min: lowest first")

    def test_an_incomplete_run_is_refused(self):
        self.declare_run(status="running")
        code, out, _ = self.rank()
        self.assertEqual(code, 1, "a partial per-sample file ranks a truncated pass")

    def test_unscorable_records_are_counted_never_dropped_silently(self):
        """A systematic parse failure on one class looks exactly like that class
        having no bad cases."""
        self.freeze()
        self.write(f"proj/stages/evaluation/runs/{self.RUN}/output/per_sample.jsonl",
                   json.dumps({"unit": "u0", "loss": 9.0}) + "\n" +
                   json.dumps({"unit": "u1", "loss": "n/a"}) + "\n" +
                   json.dumps({"unit": "u2"}) + "\n")
        code, out, _ = self.rank()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["population"]["unscorable"], 2)

    def test_every_record_says_it_is_a_lower_bound_and_what_it_is_blind_to(self):
        """The pile reads as "the model's problems" and cannot be: the eval set
        was cut from the training distribution, so a region the training data
        never covered has no sample here to rank. That is /data-drift's."""
        self.freeze()
        self.sid()
        code, out, _ = self.rank("--name", "t2")
        self.assertTrue(out["lower_bound"])
        self.assertIn("data-drift", out["blind_to"])
        self.assertIn("drift", json.dumps(self.session()["blind_to"]),
                      "the caveat has to survive into the file, not just stdout")


class Addressable(TriageCase):
    """CLAUDE.md -> "Skills & Dependencies": a finding has to be addressable by
    whoever will act on it. A unit id no manifest can look up cannot be sent to a labeling
    party — and `unverifiable` is a third state that must not read as resolved.
    """

    def test_unresolvable_units_refuse_the_piles_that_leave_the_model_line(self):
        self.freeze(units=("x9",))          # the manifest knows nothing about u0..
        self.sid()
        for u in ("u0", "u1", "u2", "u3", "u4"):
            self.judge(u, "label_wrong", "agent")
        code, out, _ = self.route()
        self.assertEqual(code, 1)
        self.assertIn("unresolved", json.dumps(out))

    def test_model_wrong_needs_no_manifest(self):
        """It is acted on by editing a config in this repo, so addressability
        never applies to it."""
        self.freeze(units=("x9",))
        self.sid()
        for u in ("u0", "u1", "u2", "u3", "u4"):
            self.judge(u, "model_wrong", "agent")
        code, out, _ = self.route()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["routed"]["model_wrong"]["count"], 5)

    def test_no_cited_snapshot_is_unverifiable_not_resolved(self):
        self.declare_run(parents=[])
        code, out, _ = self.rank()
        self.assertEqual(code, 0, out)
        self.assertTrue(all(c["resolution"] == "unverifiable" for c in out["cases"]),
                        "nothing confirmed these mappings either way")

    def test_a_unit_nobody_ranked_cannot_be_judged(self):
        self.freeze()
        self.sid()
        code, out, _ = self.judge("not_in_the_pile", "label_wrong", "agent")
        self.assertEqual(code, 1, "a unit with no score behind it would enter the "
                                 "piles unmeasured")


class OwnersAreDistinct(TriageCase):
    """CLAUDE.md -> "Skills & Dependencies": three verdicts, three owners, and
    `model_wrong`
    "never leaves the model line". A routing table that named one owner twice
    would collapse the distinction the whole skill exists to draw.
    """

    def test_each_verdict_names_a_different_owner(self):
        self.freeze()
        self.sid()
        self.judge("u0", "label_wrong", "agent")
        self.judge("u1", "sample_hard", "agent")
        self.judge("u2", "model_wrong", "agent")
        self.judge("u3", "unclear", "agent")
        self.judge("u4", "model_wrong", "agent")
        code, out, _ = self.route()
        self.assertEqual(code, 0, out)
        owners = {v: out["routed"][v]["owner"] for v in
                  ("label_wrong", "sample_hard", "model_wrong")}
        self.assertEqual(len(set(owners.values())), 3, owners)
        self.assertIn("data-label", owners["label_wrong"])
        self.assertIn("param_injection", owners["model_wrong"])

    def test_unclear_is_reported_and_routed_nowhere(self):
        self.freeze()
        self.sid()
        for u in ("u0", "u1", "u2", "u3", "u4"):
            self.judge(u, "unclear", "agent")
        code, out, _ = self.route()
        self.assertEqual(code, 0, out)
        self.assertIsNone(out["routed"]["unclear"]["owner"])
        self.assertEqual(len(out["needs_another_look"]), 5)


if __name__ == "__main__":
    unittest.main()
