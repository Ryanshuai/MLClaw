"""Contract checks on the skill documents themselves.

Most of CLAUDE.md is not enforceable and says so. This file holds the exceptions
where a doc-level property has a mechanical test AND a known drift vector — a way
the property gets undone by ordinary editing rather than by anyone deciding to
undo it.

WHAT WAS REMOVED, AND WHY, because the reasoning is worth more than the checks
were. This file used to hold four more checks, all of the same shape: assert that
a literal string is present in CLAUDE.md.

    assertIn("Decide what evidence can decide", CLAUDE_MD)
    assertIn("value you can read is never a question", CLAUDE_MD)
    assertIn("does **not** depend on the answer", CLAUDE_MD)   # markdown and all

Each was written to protect a real idea. What they actually pinned was the
*wording*, down to the bold markers — so rewording a principle, which is the
normal way a document like CLAUDE.md gets better, turned a test red. And the
failure said "put this exact phrase back", which is an instruction to undo the
edit rather than information about which side is right. That is precisely the
condition CLAUDE.md -> "Contracts" names for removal: *a check whose failure
doesn't tell you which side to change is itself a liability; delete it.*

One went further. `test_every_skill_that_says_ask_also_says_what_not_to_ask`
required that any skill mentioning "one question at a time" also carry the exact
phrase "Decide what evidence can decide". Writing a new skill in the natural way
produced a red test demanding boilerplate — a check that taxes the act of adding
a skill, which is the thing this repo most wants to be cheap.

The ideas survive where they belong: in CLAUDE.md, read by the agent, which is
the only place they ever did any work. What is gone is the claim that a grep over
prose is a contract.

The check kept below is a different kind, and the distinction is the whole point
of the split: it does not read prose at all. It checks that a flag the document
tells a person to type still exists in the program they will type it at. When it
fails, it names which side moved.
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENCODING = "utf-8"


class AFlagTheDocsTellPeopleToTypeStillExists(unittest.TestCase):
    """CLAUDE.md -> "Key design principles": "File the question; do not block on
    it", whose mechanism is `/ask-human`'s `ask.py open`.

    A rule citing flags that have been renamed is worse than no rule: it reads as
    an instruction and fails at the shell. This is checkable without pinning a
    word of the prose — the flags are an interface, and an interface is exactly
    the thing a contract check should hold.
    """

    def test_ask_open_still_has_the_flags_the_rule_tells_people_to_pass(self):
        src = (ROOT / "lifecycle" / "scripts" / "ask-human" / "ask.py").read_text(
            encoding=ENCODING)
        for flag in ("--asked", "--why", "--verify", "--to"):
            self.assertIn(flag, src, f"ask.py no longer offers {flag}")


if __name__ == "__main__":
    unittest.main()
