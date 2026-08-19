"""One enum, four files, and the value that went missing the first time.

`match` says whether a located asset is usable, and it is read by `/train-run`
and `/eval-run` to decide between launching, downloading, waiting for a vendor,
and refusing. It is declared in two schema references and two JSON templates,
which is three more places than it should be — but the templates are what a
manual fallback reproduces when a script breaks, so they have to carry it.

This file exists because the enum has already been wrong once. It shipped as
`ok | mismatch | absent | pending` with no value for "could not look", and
`/train-init` therefore instructed that an unreachable source "simply produces no
candidates". The result was an `input.json` in which data behind a missing
credential does not appear at all — and every later reader takes absence from
that file as proof the data does not exist. That is CLAUDE.md "Never report data
you could not look at", committed by a schema.

So two things are checked: that every declaration agrees, and that the value
whose absence caused the bug cannot quietly leave again.
"""
import os
import re
import unittest

from helpers import REPO_ROOT

# Every file that states the enum. Adding a fifth is a decision, and this list
# failing to include it is the drift — grep for `match`: before adding one.
DECLARES = [
    "skills/train-init/references/schemas.md",
    "lifecycle/training/input.json",
    "lifecycle/evaluation/input.json",
    "lifecycle/evaluation/artifacts.json",
]

# The one that must never silently leave, and why it is named here rather than
# left implicit in a set comparison: a future edit that drops it from all four
# files at once would satisfy an agreement check and reintroduce the original bug.
REQUIRED = "unreachable"

PATTERN = re.compile(r"`?match`?\s*:\s*([A-Za-z`| ]+?)\s*\.")


def read(rel):
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def enum_in(text):
    """-> the first `match: a | b | c.` declaration, as a list of bare values."""
    m = PATTERN.search(text)
    if not m:
        return None
    return [v.strip().strip("`") for v in m.group(1).split("|") if v.strip()]


class TheMatchEnumAgreesEverywhere(unittest.TestCase):
    """CLAUDE.md -> "Key design principles": JSON configs are the source of truth,
    fixed keys. A value one file allows and another does not is a config that
    validates in one stage and is rejected by the next, with the difference
    visible only to whoever reads both.
    """

    def setUp(self):
        self.found = {rel: enum_in(read(rel)) for rel in DECLARES}

    def test_every_listed_file_actually_declares_it(self):
        missing = [rel for rel, v in self.found.items() if v is None]
        self.assertEqual(missing, [],
                         "listed as declaring the match enum but does not — either "
                         "the wording changed or the declaration moved")

    def test_all_declarations_are_identical(self):
        sets = {rel: tuple(v) for rel, v in self.found.items() if v}
        distinct = set(sets.values())
        self.assertEqual(len(distinct), 1,
                         f"the match enum disagrees across files: {sets}")

    def test_could_not_look_is_still_a_value(self):
        """The bug this file was written after. `absent` is a conclusion —
        somebody looked. `unreachable` is a bucket with no key. Collapsing them
        makes a credential gap indistinguishable from data that does not exist,
        and only one of those lets you stop worrying.
        """
        for rel, values in self.found.items():
            self.assertIsNotNone(values, rel)
            self.assertIn(REQUIRED, values,
                          f"{rel} has no value for 'could not look' — an "
                          f"unreachable source will be recorded as absent, or "
                          f"omitted, which reads the same way downstream")

    def test_absent_and_unreachable_are_both_present_and_distinct(self):
        for rel, values in self.found.items():
            self.assertIn("absent", values, rel)
            self.assertNotEqual("absent", REQUIRED)
            self.assertEqual(len(values), len(set(values)), f"{rel} repeats a value")


class TheSchemaIsDocumentedOnceAndReferencedElsewhere(unittest.TestCase):
    """CLAUDE.md -> "Skills & Dependencies": the reason the source sweep became
    `/discover` instead of being copied into `/eval-init` was that a second
    copy drifts. The same reasoning applies to the table describing what it
    produces, so `/eval-init` cites `/train-init`'s `candidates` schema rather
    than restating it — and this check is what stops somebody helpfully pasting
    it in.
    """

    EVAL_SCHEMAS = "skills/eval-init/references/schemas.md"

    def test_eval_init_points_at_the_canonical_candidates_schema(self):
        text = read(self.EVAL_SCHEMAS)
        self.assertIn("candidates", text,
                      "eval-init gained candidates; its schemas file must say so")
        self.assertIn("train-init", text,
                      "the candidates schema is train-init's; eval-init must cite "
                      "it rather than carry a second copy")

    def test_eval_init_does_not_restate_the_location_enum(self):
        """The `location` enum is the long one and the one most likely to be
        pasted. If it appears here too, the next value added to it lands in one
        file and not the other."""
        text = read(self.EVAL_SCHEMAS)
        self.assertNotIn("`code_default` |", text,
                         "the location enum is restated in eval-init — cite "
                         "train-init's table instead")

    def test_eval_init_documents_only_its_own_deltas(self):
        """What is genuinely eval-specific must be here, or it lives nowhere: a
        checkpoint cited as a run, and the sample-count gate."""
        text = read(self.EVAL_SCHEMAS)
        self.assertIn("run:", text, "the run:<stage>/<run_id> location is eval's own")
        self.assertIn("samples", text, "the sample-count gate is eval's own")


if __name__ == "__main__":
    unittest.main()
