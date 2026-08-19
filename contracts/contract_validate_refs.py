#!/usr/bin/env python3
"""`infer-init/validate_refs.py` — the `${}` gate, and the one whose own history
is written into its source as a warning.

Two comments in that file record real incidents, and both are the SAME failure
shape from opposite sides: a validator that cannot SEE a reference and a
validator that sees one it should not own.

  the union comment   an evaluation stage declares annotations under
                      `ground_truth.items` and references them as `${input.gt}`.
                      A validator reading only `items` reports every correct eval
                      config as broken, and /eval-init's "don't save if there are
                      broken references" then blocks a save that should have gone
                      through
  the regex comment   the narrower `\\$\\{(\\w+\\.\\w+)\\}` matched neither
                      `${resources.servers.gpu.host}` nor `${artifact.my-model}`.
                      It did not FLAG them -- it could not see them, so they were
                      also counted as never referenced, and the unused-item
                      warning fired on items that were in use

Both are one-character edits away from returning, and nothing was checking either.
The rest of this file holds the error/warning line, which is what decides whether
`/infer-init` and `/eval-init` are allowed to save.
"""
import os
import unittest

from helpers import TempDirCase, load_script, run_script

vr = load_script("infer-init/validate_refs.py")
SCRIPT = "infer-init/validate_refs.py"


class StageCase(TempDirCase):

    def stage(self, **files):
        for name, obj in files.items():
            self.write_json(f"proj/stages/s/{name.replace('_', '.')}", obj)
        return self.path("proj", "stages", "s")

    def check(self, **files):
        return vr.validate(self.stage(**files))

    def joined(self, findings):
        return "\n".join(findings)


class TheInputNamespaceIsTheUnionOfTwoBlocks(StageCase):
    """The incident the source records: reading only `items` reports every
    correct EVAL config as broken, and the init skill then refuses to save."""

    def test_a_ground_truth_item_resolves_under_the_input_prefix(self):
        r = self.check(
            config_json={"runtime_params": {"ann": "${input.gt_ann}"}},
            input_json={"items": {}, "ground_truth": {"items": {"gt_ann": {}}}},
        )
        self.assertEqual(r["errors"], [],
                         "there is no ${ground_truth.x} prefix; gt items ARE input items")

    def test_a_plain_input_item_still_resolves(self):
        r = self.check(config_json={"runtime_params": {"d": "${input.val}"}},
                     input_json={"items": {"val": {}}})
        self.assertEqual(r["errors"], [])

    def test_a_name_in_neither_block_is_still_an_error(self):
        """The union must widen what resolves, not stop the gate resolving."""
        r = self.check(config_json={"runtime_params": {"d": "${input.nope}"}},
                     input_json={"items": {"val": {}},
                                 "ground_truth": {"items": {"gt": {}}}})
        self.assertEqual(len(r["errors"]), 1)

    def test_a_name_in_both_blocks_is_ambiguous_and_warned(self):
        """`${input.x}` could mean either, and which one it means changes what
        the run reads."""
        r = self.check(config_json={"runtime_params": {"d": "${input.val}"}},
                     input_json={"items": {"val": {}},
                                 "ground_truth": {"items": {"val": {}}}})
        self.assertIn("Ambiguous", self.joined(r["warnings"]))


class AReferenceItCannotSeeIsWorseThanOneItRejects(StageCase):
    """The regex incident. An unmatched reference is not reported as broken --
    it is invisible, so it ALSO makes its target look unreferenced, and the
    unused-item warning then fires on an item that is in use."""

    def test_a_deep_dotted_reference_is_seen(self):
        r = self.check(config_json={"runtime_params": {"h": "${resources.servers.gpu.host}"}})
        self.assertTrue(any("servers.gpu.host" in i for i in r["info"]),
                        "an external reference must be RECOGNISED and reported as "
                        "run-time-resolved, not silently skipped")

    def test_a_hyphenated_item_name_is_seen_and_resolves(self):
        r = self.check(config_json={"runtime_params": {"m": "${artifact.my-model}"}},
                     artifacts_json={"items": {"my-model": {}}})
        self.assertEqual(r["errors"], [])
        self.assertNotIn("Unused item", self.joined(r["warnings"]),
                         "the item IS referenced; a regex that cannot see the "
                         "reference reports it unused, which is the incident")

    def test_a_hyphenated_name_that_is_not_declared_is_an_error(self):
        r = self.check(config_json={"runtime_params": {"m": "${artifact.my-model}"}},
                     artifacts_json={"items": {"other": {}}})
        self.assertEqual(len(r["errors"]), 1)

    def test_the_entry_command_is_scanned_too(self):
        """It is one of the two places a reference actually reaches the code."""
        r = self.check(config_json={"entry_command": "python run.py --w ${artifact.w}"},
                     artifacts_json={"items": {}})
        self.assertEqual(len(r["errors"]), 1)


class ProseIsNotUsage(StageCase):
    """`_comment*` keys are documentation. A reference NAMED in a comment is not
    a reference USED, and counting it turns every template's own explanation of
    the `${}` syntax into a broken reference -- which is every lifecycle template
    in the repo, since that is where the syntax is documented.

    The comment must sit in a file that IS scanned whole. `config.json` is not:
    only its `runtime_params` and `entry_command` are read, which the class
    below pins.
    """

    def test_a_reference_inside_a_comment_key_is_not_a_finding(self):
        r = self.check(input_json={"_comment": "write it as ${input.something}",
                                   "items": {}})
        self.assertEqual(r["errors"], [])

    def test_a_comment_reference_does_not_count_as_using_an_item(self):
        r = self.check(config_json={"runtime_params": {}},
                       artifacts_json={"_comment_x": "pass it as ${artifact.w}",
                                       "items": {"w": {}}})
        self.assertIn("Unused item: artifact.w", self.joined(r["warnings"]))

    def test_a_real_reference_in_the_same_file_is_still_seen(self):
        """So the skip is scoped to comment KEYS and not to the file."""
        r = self.check(input_json={"_comment": "prose", "items": {},
                                   "sources": {"d": {"path": "${input.nope}"}}})
        self.assertEqual(len(r["errors"]), 1)


class TheScanSurfaceIsWhatReachesTheCode(StageCase):
    """`config.json` is deliberately NOT scanned whole. `/{stage}-run` builds the
    command from `runtime_params` and `entry_command`; a `${}` anywhere else in
    that file is a value the code never receives, and flagging it would make the
    gate answer about something it does not govern."""

    def test_runtime_params_is_scanned(self):
        r = self.check(config_json={"runtime_params": {"d": "${input.nope}"}},
                       input_json={"items": {}})
        self.assertEqual(len(r["errors"]), 1)

    def test_a_reference_elsewhere_in_config_json_is_not_the_gates_business(self):
        r = self.check(config_json={"env_snapshot": {"note": "${input.nope}"},
                                    "runtime_params": {}},
                       input_json={"items": {}})
        self.assertEqual(r["errors"], [])


class NotDeclaredAndNotYetWrittenAreDifferentAnswers(StageCase):
    """Three facts. A reference to an item whose declaring FILE does not exist
    yet is an artifact of a half-finished init, not a broken config -- erroring
    on it would make /infer-init unable to save at every intermediate step."""

    def test_a_missing_declaring_file_warns_rather_than_errors(self):
        r = self.check(config_json={"runtime_params": {"m": "${artifact.w}"}})
        self.assertEqual(r["errors"], [])
        self.assertIn("does not exist yet", self.joined(r["warnings"]))

    def test_a_present_declaring_file_without_the_item_errors(self):
        r = self.check(config_json={"runtime_params": {"m": "${artifact.w}"}},
                     artifacts_json={"items": {}})
        self.assertEqual(len(r["errors"]), 1)

    def test_an_external_prefix_is_info_not_a_finding(self):
        r = self.check(config_json={"runtime_params": {"p": "${project.name}"}})
        self.assertEqual(r["errors"], [])
        self.assertNotIn("project.name", self.joined(r["warnings"]))
        self.assertEqual(len(r["info"]), 1)

    def test_an_unknown_prefix_is_a_warning_not_an_error(self):
        """It may be a namespace this validator has not learned; blocking a save
        on that is worse than saying so."""
        r = self.check(config_json={"runtime_params": {"x": "${weights.best}"}})
        self.assertEqual(r["errors"], [])
        self.assertIn("Unknown prefix", self.joined(r["warnings"]))


class AnItemNothingPassesIsWorthSaying(StageCase):
    """`/{stage}-run` builds the command from `runtime_params` and
    `entry_command`. An item declared and referenced from neither is one the code
    will never receive, however carefully it was located."""

    def test_an_unreferenced_artifact_is_warned(self):
        r = self.check(config_json={"runtime_params": {}},
                     artifacts_json={"items": {"w": {}}})
        self.assertIn("Unused item: artifact.w", self.joined(r["warnings"]))

    def test_an_unreferenced_ground_truth_item_says_convention_may_be_fine(self):
        """Annotations are routinely found by parallel-directory convention, so
        this one warning needs its exception attached or it teaches people to
        ignore the whole class."""
        r = self.check(config_json={"runtime_params": {}},
                     input_json={"items": {}, "ground_truth": {"items": {"gt": {}}}})
        self.assertIn("convention", self.joined(r["warnings"]))

    def test_an_unreferenced_output_item_is_not_warned(self):
        """Outputs are written by the code, not passed to it."""
        r = self.check(config_json={"runtime_params": {}}, output_json={"items": {"m": {}}})
        self.assertNotIn("Unused item", self.joined(r["warnings"]))


class TheExitCodesSayWhichKindOfNo(StageCase):
    """CLAUDE.md -> "Script Integration". 2 means fall back and check by hand,
    which for a reference gate means not checking."""

    def test_a_clean_stage_is_exit_0(self):
        d = self.stage(config_json={"runtime_params": {"d": "${input.val}"}},
                       input_json={"items": {"val": {}}})
        self.assertEqual(run_script(SCRIPT, d)[0], 0)

    def test_a_broken_reference_is_exit_1(self):
        d = self.stage(config_json={"runtime_params": {"d": "${input.nope}"}},
                       input_json={"items": {}})
        rc, out, _err = run_script(SCRIPT, d)
        self.assertEqual(rc, 1)
        self.assertEqual(out["summary"]["errors"], 1)

    def test_warnings_alone_are_still_exit_0(self):
        d = self.stage(config_json={"runtime_params": {}},
                       artifacts_json={"items": {"w": {}}})
        rc, out, _err = run_script(SCRIPT, d)
        self.assertEqual(rc, 0)
        self.assertTrue(out["summary"]["warnings"])

    def test_a_missing_stage_dir_is_exit_2(self):
        rc, out, _err = run_script(SCRIPT, self.path("nothing"))
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_a_stage_with_no_config_files_at_all_is_exit_2(self):
        os.makedirs(self.path("empty"))
        self.assertEqual(run_script(SCRIPT, self.path("empty"))[0], 2)


if __name__ == "__main__":
    unittest.main()
