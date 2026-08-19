#!/usr/bin/env python3
"""CLAUDE.md -> "Never silently": *Never let a value have two authors.*

The ratchet `/agent-refactor` Step 5b asks for and this repo did not have. One pass of
that scan found the machine shape with THREE authors inside `pool.py` alone, `TIERS` and
`PROVENANCE` with two each, `SSH_OPTS` with two, three atomic writers drifted three ways,
and `DEFAULT_TTL_S` copied under a comment claiming it matched. None of those raised
anything: both ends stay internally consistent, so what surfaces is a legal wrong answer.

TWO CHECKS, AND THEY CATCH DIFFERENT SHAPES.

  structural   another module-level constant, anywhere, holding an EQUAL value. This is
               `protocols.py`'s VOCAB: `TIERS` in two files, `TERMINAL` restated by its
               reader. Compares parsed values, not text, so it does not care about naming.
  textual      the author's CURRENT value written as a bare literal in another `.py`.
               This is the one reachability, zero-reference and coverage are all blind to
               -- it is not a symbol, and a prose copy is never executed.

‼️ THE SCANNER READS EACH VALUE FROM ITS AUTHOR AND HARD-CODES NONE OF THEM. A check
carrying its own copy of the value would be the worst kind of second author: one that
always reports agreement.

WHAT IS DELIBERATELY OUT OF SCOPE, because saying so is the difference between a gap and
a lie:

  * `.md` prose. A specification document naming the format it specifies is the
    declaration, not a copy -- `skills/lease/references/contract.md` writing
    `mlclaw-<lease_id>` is where that convention is stated. `/agent-refactor` Step 5b
    sweeps prose once per round, by hand, against the value read from the author.
  * Values too common to attribute: short strings and small integers. `2` and `255` match
    over a thousand lines apiece, and a check whose output is noise is a check people
    route around.
  * The machine shape. Its author is `_common.SHAPE_ARGS`, a table of argparse kwargs
    holding type objects, so `literal_eval` cannot read it and this check cannot know its
    value. `pool.py` carried three copies of that table until this round; nothing here
    would have caught them, and Step 5b's by-hand sweep is what did.
  * `DEFAULT_STALE_DAYS`. It genuinely HAS two authors today -- `phase.py` and
    `handoff.py:669`'s `--stale-days` default -- and `board.py:73`'s comment predicted it
    in as many words ("the way two independently-typed `14.0` literals eventually would").
    It is absent here because fixing it needs a decision nobody has made: handoff staleness
    is arguably `handoff.py`'s, and `handoff.py` importing `phase.py` inverts the layering.
    A check kept red over an open design question is a check that teaches people to ignore
    a red. Registering it is the fix, once that call is made.
"""
import ast
import os
import re
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The registry: (author file, symbol). Adding a row is how a value declares it has one
# author. Nothing here records the VALUE -- that is read from the author at check time.
SINGLE_AUTHOR = [
    ("scripts/lease/_common.py", "DEFAULT_TTL_S"),
    ("scripts/lease/_common.py", "TAG_PREFIX"),
    ("scripts/lease/_common.py", "SSH_OPTS"),
    ("scripts/lease/_common.py", "SSH_UNREACHABLE"),
    ("scripts/lease/_common.py", "ERROR_CLASSES"),
    ("scripts/shared/_vocab.py", "TIERS"),
    ("scripts/shared/_vocab.py", "PROVENANCE"),
    ("scripts/shared/_vocab.py", "HANDOFF_TERMINAL"),
    ("scripts/shared/_dataset_paths.py", "CENSUS_PREFIX"),
    ("scripts/shared/_dataset_paths.py", "DEFAULT_MIN_SOURCE_COPIES"),
]

# A second occurrence that was looked at and ruled legitimate. The reason is required:
# an allowlist without one is just a list of things somebody once wanted to stop failing.
ALLOWED = {
    ("contracts/contract_fleet.py", "TAG_PREFIX"):
        "test-side second author. A check that restates the subject's value independently "
        "IS the check; importing it would delete what the assertion is for -- the same "
        "reason `contract_triage_verifier.VERDICTS` restates `triage.VERDICTS`.",
    ("contracts/contract_ara.py", "CENSUS_PREFIX"):
        "a docstring CITING `references/layout.md`'s declared filename format "
        "(`census/census_{ts}.json`). Same carve-out as `.md` prose, which lands inside a "
        "`.py` here: code must import the name, documentation may name the format it "
        "declares. What this does not excuse is `census.py` BUILDING an id from a literal, "
        "which is what this check just caught.",
    ("scripts/lease/provider_ssh.py", "TAG_PREFIX"):
        "`ControlPath=~/.ssh/mlclaw-%C` is an ssh control-socket path, not a lease tag. "
        "One string, two concepts: `sweep` filters on the `mlclaw_tag` label and never on "
        "this. Renaming the tag namespace must NOT move the socket path.",
}

# Below these, a literal matches too much of the repo to attribute to anyone.
MIN_STR_LEN = 4
MIN_INT_ABS = 1000


def tracked(*globs):
    out = subprocess.run(["git", "ls-files", *globs], cwd=REPO_ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    return [p for p in out.stdout.split() if os.path.isfile(os.path.join(REPO_ROOT, p))]


def module_constants(rel):
    """-> {NAME: value} for module-level `NAME = <literal>` only.

    Anything `literal_eval` cannot take (a comprehension, a dict of type objects) is
    skipped rather than guessed at -- `SHAPE_ARGS` is one, and a wrong value here would
    make every downstream comparison meaningless.
    """
    path = os.path.join(REPO_ROOT, rel)
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return {}
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                pass
    return found


def author_value(rel, name):
    return module_constants(rel).get(name, KeyError)


def attributable(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return len(value) >= MIN_STR_LEN
    if isinstance(value, int):
        return abs(value) >= MIN_INT_ABS
    return False


class EveryRegisteredValueHasOneAuthor(unittest.TestCase):
    """CLAUDE.md -> "Never silently": *Never let a value have two authors.*"""

    def test_the_registry_resolves(self):
        """A row naming a symbol that no longer exists silently stops checking it, and a
        registry that quietly checks nothing looks exactly like a clean one."""
        missing = [f"{f}:{n}" for f, n in SINGLE_AUTHOR
                   if author_value(f, n) is KeyError]
        self.assertEqual(missing, [], "registered symbol not found at its author")

    def test_no_other_module_level_constant_holds_an_equal_value(self):
        """The VOCAB shape: `TIERS` defined twice, a reader restating its writer's
        statuses. Compares parsed values, so a rename does not hide it."""
        everywhere = {rel: module_constants(rel) for rel in tracked("*.py")}
        dupes = []
        for af, name in SINGLE_AUTHOR:
            want = author_value(af, name)
            if want is KeyError or isinstance(want, bool) or want in (None, 0, 1, "", (), []):
                continue
            for rel, consts in everywhere.items():
                for other, value in consts.items():
                    if (rel, other) == (af, name):
                        continue
                    if type(value) is type(want) and value == want:
                        dupes.append(f"{af}:{name} == {rel}:{other}")
        self.assertEqual(sorted(dupes), [],
                         "a second module-level definition holds the same value; import it instead")

    def test_no_python_file_spells_the_current_value(self):
        """The VALUE shape (`/agent-refactor` Step 5b). The value is read from the author
        here and nowhere written down -- a scanner carrying its own copy is a second
        author that always agrees."""
        offenders = []
        for af, name in SINGLE_AUTHOR:
            want = author_value(af, name)
            if want is KeyError or not attributable(want):
                continue
            pattern = re.compile(rf"(?<![\w.]){re.escape(str(want))}(?![\w])")
            for rel in tracked("*.py"):
                if rel == af or ALLOWED.get((rel, name)):
                    continue
                try:
                    text = open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        offenders.append(f"{rel}:{i} spells {af}:{name}")
        self.assertEqual(sorted(offenders), [],
                         "import the name instead of writing the value; if this second "
                         "occurrence is deliberate, add it to ALLOWED with a reason")

    def test_every_allowlist_entry_still_applies(self):
        """An allowlist outlives what it excused. A row whose file no longer contains the
        value is a standing exemption for a problem that no longer exists -- and the next
        real copy in that file would inherit it."""
        stale = []
        for (rel, name), reason in ALLOWED.items():
            self.assertTrue(reason.strip(), f"{rel}:{name} has no reason")
            want = None
            for af, n in SINGLE_AUTHOR:
                if n == name:
                    want = author_value(af, n)
                    break
            self.assertIsNotNone(want, f"ALLOWED names {name}, which is not registered")
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.isfile(path):
                stale.append(f"{rel} (gone)")
                continue
            if str(want) not in open(path, encoding="utf-8").read():
                stale.append(f"{rel}:{name} (no longer present)")
        self.assertEqual(sorted(stale), [], "allowlist entry no longer excuses anything")


if __name__ == "__main__":
    unittest.main()
