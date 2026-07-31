"""Regression tests for metric extraction.

One rule under test: **a metric that could not be extracted must never be
indistinguishable from a metric the run did not produce.** Both used to become
`null`, flow into run.json, and get silently skipped by every downstream
comparison. A broken regex and an absent number read identically.
"""
import unittest

from helpers import TempDirCase, load_script

em = load_script("shared/extract_metrics.py")


def cfg(**definitions):
    return {"metrics": {"watch": list(definitions), "definitions": definitions}}


class SuccessfulExtraction(TempDirCase):
    """run-mechanics.md -> "Record integrity": an extracted metric is only recorded
    when it was actually read.
    """
    def setUp(self):
        super().setUp()
        self.write("rd/logs/stdout.log", "epoch 1 mAP50: 0.71\nepoch 2 mAP50: 0.83\n")
        self.write_json("rd/res.json", {"summary": {"recall": 0.9}})

    def test_stdout_pattern(self):
        r = em.extract(cfg(map50={"source": "stdout", "pattern": r"mAP50: ([0-9.]+)"}),
                       self.path("rd"))
        self.assertEqual(r["metrics"]["map50"], 0.83)
        self.assertEqual(r["errors"], {})

    def test_nested_file_key(self):
        r = em.extract(cfg(recall={"source": "file", "path": "res.json", "key": "summary.recall"}),
                       self.path("rd"))
        self.assertEqual(r["metrics"]["recall"], 0.9)


class FailuresAreDistinguishable(TempDirCase):
    """run-mechanics.md -> "Record integrity": a metric that could not be extracted must
    never be indistinguishable from a metric the run did not produce. Both used to
    be written as `null`, flow into run.json, and be silently skipped by every
    downstream comparison — a broken regex and an absent number read identically.
    """

    def setUp(self):
        super().setUp()
        self.write("rd/logs/stdout.log", "epoch 1 mAP50: 0.71\n")
        self.write_json("rd/res.json", {"summary": {"recall": 0.9}})

    def assert_reason(self, definition, expected_reason, name="m"):
        r = em.extract(cfg(**{name: definition}), self.path("rd"))
        self.assertNotIn(name, r["metrics"],
                         "a failed extraction must not land in `metrics`")
        self.assertIn(name, r["errors"])
        self.assertEqual(r["errors"][name]["reason"], expected_reason)
        return r["errors"][name]

    def test_pattern_matched_nothing(self):
        self.assert_reason({"source": "stdout", "pattern": r"F1: ([0-9.]+)"},
                           "pattern_no_match")

    def test_log_file_absent(self):
        r = em.extract(cfg(m={"source": "stdout", "pattern": "x"}), self.path("no_such_run"))
        self.assertEqual(r["errors"]["m"]["reason"], "log_not_found")

    def test_invalid_regex(self):
        self.assert_reason({"source": "stdout", "pattern": "([unclosed"}, "pattern_invalid")

    def test_ambiguous_multi_group_pattern(self):
        """Two capture groups made findall return tuples; float(tuple) raised and
        was swallowed. Guessing which group holds the number is not acceptable."""
        self.assert_reason({"source": "stdout", "pattern": r"epoch ([0-9]+) mAP50: ([0-9.]+)"},
                           "pattern_ambiguous")

    def test_match_is_not_a_number(self):
        self.write("rd/logs/stdout.log", "status: converged\n")
        self.assert_reason({"source": "stdout", "pattern": r"status: (\w+)"},
                           "match_not_numeric")

    def test_result_file_absent(self):
        self.assert_reason({"source": "file", "path": "nope.json", "key": "a"},
                           "file_not_found")

    def test_result_file_is_not_json(self):
        self.write("rd/broken.json", "{not json")
        self.assert_reason({"source": "file", "path": "broken.json", "key": "a"},
                           "file_not_json")

    def test_key_absent_names_what_was_available(self):
        err = self.assert_reason(
            {"source": "file", "path": "res.json", "key": "summary.precision"}, "key_absent")
        self.assertIn("recall", err["detail"],
                      "the error should show what keys were actually there")

    def test_value_not_numeric(self):
        self.write_json("rd/res.json", {"summary": {"recall": "high"}})
        self.assert_reason({"source": "file", "path": "res.json", "key": "summary.recall"},
                           "value_not_numeric")

    def test_unknown_source(self):
        self.assert_reason({"source": "tensorboard"}, "source_unknown")

    def test_incomplete_definition(self):
        self.assert_reason({"source": "stdout"}, "definition_incomplete")
        self.assert_reason({"source": "file", "path": "res.json"}, "definition_incomplete")


class ConfigBugsSurface(TempDirCase):
    """run-mechanics.md -> "Record integrity": a metric named in `watch` with no definition
    is a config bug and must surface, not vanish.
    """
    def test_watched_metric_with_no_definition_is_reported(self):
        """Previously a bare `continue`: a typo in `watch` vanished without trace
        and looked exactly like a clean extraction."""
        r = em.extract({"metrics": {"watch": ["typoed_name"], "definitions": {}}},
                       self.path("rd"))
        self.assertEqual(r["undefined"], ["typoed_name"])
        self.assertEqual(r["metrics"], {})

class CommandLine(TempDirCase):
    """CLAUDE.md -> "Script Integration": exit 2 means the script broke and the
    caller should fall back to doing it by hand; exit 0/1 mean the script worked and
    the findings are the answer.
    """
    def test_findings_are_not_a_failure_exit(self):
        """Errors are the output, not a crash — a non-zero exit would trigger the
        skill's manual-fallback path and lose the structured findings."""
        from helpers import run_script
        self.write("rd/logs/stdout.log", "nothing here\n")
        out_json = self.write_json("out.json",
                                   cfg(m={"source": "stdout", "pattern": "zzz: ([0-9.]+)"}))

        rc, out, err = run_script("shared/extract_metrics.py", out_json, self.path("rd"))

        self.assertEqual(rc, 0)
        self.assertEqual(out["errors"]["m"]["reason"], "pattern_no_match")
        self.assertIn("pattern_no_match", err)     # and it is loud on stderr

    def test_unreadable_config_exits_nonzero_without_traceback(self):
        from helpers import run_script
        rc, out, err = run_script("shared/extract_metrics.py",
                                  self.path("missing.json"), self.path("rd"))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
