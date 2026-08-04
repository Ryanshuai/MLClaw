"""Contract checks on the skill documents themselves.

Most of CLAUDE.md is not enforceable and says so. This file holds the exceptions
where a doc-level property has a mechanical test AND a known drift vector — a way
the property gets undone by ordinary editing rather than by anyone deciding to
undo it.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text()

ASKING = re.compile(r"one question at a time", re.IGNORECASE)
AUTONOMY = re.compile(r"Decide what evidence can decide")


class ASkillThatRestatesTheAskingRuleCarriesItsCounterpart(unittest.TestCase):
    """CLAUDE.md -> "Key design principles": "Decide what evidence can decide",
    which exists to bound "One question at a time" and "Confirm before saving".

    Fourteen skills restated the asking rule locally and none restated the
    counterpart, so at the point of use — a skill loaded, CLAUDE.md skimmed pages
    earlier — the only instruction in view was the one that says to ask. The
    observed result was stopping to ask permission for a `pip install`.

    The drift vector is what earns this a check rather than a note: every one of
    those fourteen lines is a copy of a sibling's, in slightly different words.
    A new skill is written by copying the nearest one, so the bias reproduces
    itself by the ordinary act of adding a skill, and nothing anywhere notices.
    A grep does.

    It deliberately does NOT require the three-bucket table to be copied in. A
    second copy of a table is the drift this repo keeps warning about; the
    requirement is one clause carrying the operative half plus the citation.
    """

    def test_the_counterpart_exists_in_claude_md(self):
        """The citation every skill points at has to be real, or fourteen files
        now reference a section that is not there."""
        self.assertIn("Decide what evidence can decide", CLAUDE_MD)
        for bucket in ("Just do it", "Ask", "Neither"):
            self.assertIn(bucket, CLAUDE_MD, f"the {bucket!r} bucket is the rule")
        self.assertIn("value you can read is never a question", CLAUDE_MD)

    def test_every_skill_that_says_ask_also_says_what_not_to_ask(self):
        offenders = [p.parent.name for p in SKILLS
                     if ASKING.search(p.read_text()) and not AUTONOMY.search(p.read_text())]
        self.assertEqual(offenders, [], (
            "these skills tell the agent to ask and never say what is its own to "
            "settle, so every gap defaults to a question: "
            f"{offenders}. Add the clause — one sentence pointing at CLAUDE.md -> "
            '"Decide what evidence can decide", not a copy of its table."'))

    def test_the_rule_is_not_only_in_skills_that_happen_to_ask(self):
        """Sanity: the population is non-empty. A future refactor that removed the
        asking line everywhere would make the check above vacuously pass."""
        asking = [p.parent.name for p in SKILLS if ASKING.search(p.read_text())]
        self.assertGreater(len(asking), 5,
                           "expected many skills to carry the asking rule; if this "
                           "fires, the check above has stopped testing anything")


class ConfirmBeforeSavingIsAboutTheRecord(unittest.TestCase):
    """CLAUDE.md -> "Key design principles": "Confirm before saving … **This is
    about the record, not the work that produces it**".

    Unscoped, that rule reads as covering every side effect, which is how
    extracting an archive in order to read it became something to ask about. The
    scoping clause is the fix and it has to stay attached to the rule it scopes.
    """

    def test_the_scope_clause_is_attached(self):
        line = next(l for l in CLAUDE_MD.splitlines()
                    if l.startswith("- **Confirm before saving**"))
        self.assertIn("about the record, not the work that produces it", line)


if __name__ == "__main__":
    unittest.main()
