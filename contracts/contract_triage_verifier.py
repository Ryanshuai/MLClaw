"""The triage verifier: read-only, refutation-shaped, and its disagreement is a
dispute rather than an overrule.

Enforces `/eval-triage` Step 2b, and through it Step 3's standing rule that *"two
sources of equal authority disagreeing -> `disputed`, no standing verdict, and
`route` refuses it. There is no tie-break that is not a coin flip, and a coin flip
written into the record reads afterwards as a finding."*

Earns a check on CLAUDE.md -> "Contracts": the verdict it guards routes real work to
a team and cannot be silently undone once a labeling party has started on it.
"""
import os
import re
import unittest

from helpers import REPO_ROOT

AGENT = os.path.join(REPO_ROOT, "agents", "triage-verifier.md")
SKILL = os.path.join(REPO_ROOT, "skills", "eval-triage", "SKILL.md")
VERDICTS = ("label_wrong", "sample_hard", "model_wrong", "unclear")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter"
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


class TheVerifierCannotWriteWhatItJudges(unittest.TestCase):
    """A judge that can edit the record it rules on is not a check."""

    def test_tools_are_read_only(self):
        fm = frontmatter(read(AGENT))
        tools = [t.strip() for t in fm["tools"].split(",")]
        self.assertTrue(tools)
        for banned in ("Write", "Edit", "NotebookEdit", "Bash"):
            self.assertNotIn(banned, tools,
                             f"{banned} would let the verifier change the record, or run a "
                             f"command that does")
        self.assertIn("Read", tools)

    def test_it_says_it_never_writes(self):
        self.assertRegex(read(AGENT), r"never write|you never modify|You read; you never write")


class ItsAnswerSpaceIsThreeAndOnlyThree(unittest.TestCase):
    """`sustained` / `refuted` / `cannot_tell`. A fourth answer, or a missing
    `cannot_tell`, turns the verifier into a coin flip with a paragraph attached."""

    def test_the_three_answers_are_declared(self):
        t = read(AGENT)
        for a in ("sustained", "refuted", "cannot_tell"):
            self.assertIn(a, t)

    def test_cannot_tell_is_defended_as_a_real_answer(self):
        self.assertRegex(read(AGENT), r"`cannot_tell` is a real answer")

    def test_the_skill_maps_exactly_those_three(self):
        s = read(SKILL)
        block = s[s.index("Step 2b"):s.index("## Step 3")]
        for a in ("sustained", "refuted", "cannot_tell"):
            self.assertIn(a, block)


class RefutedIsADisputeNotAnOverrule(unittest.TestCase):
    """/eval-triage Step 3: two equal authorities disagreeing -> `disputed`. The
    verifier is an agent, not a person, so it may not overrule."""

    def test_the_skill_routes_refuted_to_disputed(self):
        s = read(SKILL)
        block = s[s.index("Step 2b"):s.index("## Step 3")]
        i = block.index("`refuted`")
        self.assertIn("disputed", block[i:i + 300].lower())

    def test_the_agent_states_it_never_overrules(self):
        t = read(AGENT)
        self.assertRegex(t, r"never overrule|You never overrule|you never overrule")
        self.assertRegex(t, r"proposal for a person|PROPOSAL for a person",
                         "IF_REFUTED_WHICH must be a proposal, not a decision")

    def test_the_skill_calls_the_proposal_a_proposal(self):
        s = read(SKILL)
        block = s[s.index("Step 2b"):s.index("## Step 3")]
        self.assertIn("IF_REFUTED_WHICH", block)
        self.assertRegex(block, r"proposal for a person")


class ItKnowsAllFourVerdictsAndTheAsymmetry(unittest.TestCase):
    def test_every_verdict_has_a_refutation_route(self):
        t = read(AGENT)
        for v in VERDICTS:
            self.assertIn(v, t, f"{v} has no refutation guidance")

    def test_it_scrutinises_the_two_piles_that_leave_the_model_line_harder(self):
        """`model_wrong` never leaves the model line; the other two spend somebody
        else's time and cannot be silently undone."""
        self.assertRegex(read(AGENT), r"leave the model line")

    def test_it_is_told_to_read_verdicts_md_rather_than_its_intuition(self):
        t = read(AGENT)
        self.assertIn("references/verdicts.md", t)
        self.assertTrue(os.path.isfile(os.path.join(
            REPO_ROOT, "skills", "eval-triage", "references", "verdicts.md")),
            "the agent cites a reference that must exist")


class ItIsWiredAndDispatchable(unittest.TestCase):
    def test_the_skill_dispatches_it_by_namespaced_name(self):
        s = read(SKILL)
        self.assertIn("triage-verifier", s)
        self.assertIn("mlclaw:triage-verifier", s,
                      "a plugin agent is namespaced; the bare name will not resolve")

    def test_it_sits_between_judge_and_confirm(self):
        s = read(SKILL)
        self.assertLess(s.index("## Step 2 — `judge`"), s.index("Step 2b"))
        self.assertLess(s.index("Step 2b"), s.index("## Step 3 — `confirm`"))

    def test_it_demands_a_basis_and_fails_closed_without_one(self):
        t = read(AGENT)
        self.assertRegex(t, r"fail closed|Preflight")
        self.assertIn("--basis", t)

    def test_evidence_must_name_something_opened(self):
        """A refutation with no path in it is an opinion, and the record cannot
        tell those apart later."""
        self.assertRegex(read(AGENT), r"EVIDENCE")
        self.assertRegex(read(AGENT), r"absolute path")


if __name__ == "__main__":
    unittest.main()
