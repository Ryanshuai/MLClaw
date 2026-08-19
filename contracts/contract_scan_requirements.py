#!/usr/bin/env python3
"""`infer-init/scan_requirements.py` — three init skills read this, and what it
returns becomes `config.json -> required_packages`.

CLAUDE.md -> "Contracts" admits a check for a record read later by someone who
can no longer verify it. This one feeds two: the package list a run is validated
against by `check_deps.py`, and — through `source` — the read-vs-guessed
distinction `provenance.json` carries and `provenance_gate.py` refuses production
runs over.

Three defects, all of which produced a plausible list:

  priority was inverted      the file says "Priority order: requirements.txt >
                             pyproject.toml > setup.py > conda yaml", and the
                             merge was `pkgs.update(parser(path))` -- LAST wins.
                             A stale `conda.yaml` silently overrode the
                             `requirements.txt` beside it
  the stdlib was a dependency the import fallback returned `os`, `json`, `re`
                             and the code's own neighbouring modules as required
                             packages, and `check_deps.py` then reported every
                             one as "required but NOT installed"
  prose was an import        the scanner was `^(?:import|from)\\s+(\\w+)` run over
                             raw lines, so a docstring sentence beginning "from
                             the caller" contributed a package named `the`
"""
import os
import unittest

from helpers import TempDirCase, load_script, run_script

sr = load_script("infer-init/scan_requirements.py")
SCRIPT = "infer-init/scan_requirements.py"


class CodeCase(TempDirCase):

    def code(self, **files):
        for name, body in files.items():
            self.write(f"code/{name.replace('__', '.')}", body)
        os.makedirs(self.path("code"), exist_ok=True)
        return self.path("code")

    def scan(self, **files):
        rc, out, _err = run_script(SCRIPT, self.code(**files))
        return rc, out


class TheDeclaredPriorityIsTheOneThatHolds(CodeCase):
    """The comment names an order and the merge did the opposite. Nothing raises:
    both files parse, both lists are plausible, and the recorded constraint is
    simply the wrong one."""

    def test_requirements_txt_wins_over_conda_yaml(self):
        _rc, out = self.scan(**{"requirements__txt": "torch==2.4.1\n",
                                "environment__yaml": "- pip:\n  - torch==1.9.0\n"})
        self.assertEqual(out["packages"]["torch"], "==2.4.1")

    def test_requirements_txt_wins_over_pyproject(self):
        _rc, out = self.scan(**{
            "requirements__txt": "numpy>=2.0\n",
            "pyproject__toml": 'dependencies = [\n  "numpy>=1.0",\n]\n'})
        self.assertEqual(out["packages"]["numpy"], ">=2.0")

    def test_a_lower_priority_file_still_contributes_what_is_only_in_it(self):
        """Priority decides conflicts; it must not throw away a package the
        higher-priority file never mentioned."""
        _rc, out = self.scan(**{"requirements__txt": "torch==2.4.1\n",
                                "environment__yaml": "- pip:\n  - timm==1.0.3\n"})
        self.assertEqual(sorted(out["packages"]), ["timm", "torch"])

    def test_every_file_it_read_is_named(self):
        """"From a file" without saying which is not provenance."""
        _rc, out = self.scan(**{"requirements__txt": "torch\n",
                                "setup__py": "install_requires=['timm']\n"})
        self.assertEqual(out["files"], ["requirements.txt", "setup.py"])


class ReadAndGuessedAreDifferentAnswers(CodeCase):
    """`source` is what tells a caller whether this was read or inferred. It is
    the same distinction `provenance.json` records as `guessed`, and
    `provenance_gate.py` refuses a production run over it."""

    def test_a_declared_file_says_file(self):
        _rc, out = self.scan(**{"requirements__txt": "torch==2.4.1\n"})
        self.assertEqual(out["source"], "file")

    def test_nothing_declared_says_imports(self):
        _rc, out = self.scan(**{"train__py": "import torch\n"})
        self.assertEqual(out["source"], "imports")
        self.assertEqual(out["files"], [])

    def test_an_unreadable_file_is_named_rather_than_read_as_empty(self):
        d = self.code(**{"train__py": "import torch\n"})
        with open(os.path.join(d, "requirements.txt"), "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        rc, out, _err = run_script(SCRIPT, d)
        self.assertEqual(out["unreadable"], ["requirements.txt"])
        self.assertEqual(out["source"], "imports",
                         "a file nothing could parse is not a file that declared "
                         "nothing, and it must not count as having been read")


class TheStdlibIsNotADependency(CodeCase):
    """Left in, every one became a `required_packages` entry that `check_deps.py`
    reported as "required but NOT installed" -- one per stdlib module, on exactly
    the project this fallback exists for."""

    def test_stdlib_imports_are_dropped(self):
        _rc, out = self.scan(**{"train__py": "import os, json, re\nimport torch\n"})
        self.assertEqual(sorted(out["packages"]), ["torch"])

    def test_the_codes_own_modules_are_dropped(self):
        _rc, out = self.scan(**{"train__py": "from model import Net\nimport torch\n",
                                "model__py": "class Net: pass\n"})
        self.assertEqual(sorted(out["packages"]), ["torch"])

    def test_a_module_in_a_subpackage_is_dropped_too(self):
        """A repo's own modules sit in subpackages; a top-level listing reported
        every one of them as a missing dependency."""
        self.write("code/pkg/util.py", "x = 1\n")
        _rc, out = self.scan(**{"train__py": "from util import x\nimport torch\n"})
        self.assertEqual(sorted(out["packages"]), ["torch"])

    def test_a_relative_import_is_never_a_package(self):
        _rc, out = self.scan(**{"train__py": "from . import sibling\nimport torch\n"})
        self.assertEqual(sorted(out["packages"]), ["torch"])


class ProseIsNotAnImport(CodeCase):
    """The regex fired on any line starting `import` or `from`, which includes
    ordinary English inside a docstring."""

    def test_a_docstring_sentence_does_not_become_a_package(self):
        body = '"""Read it.\n\nfrom the caller\'s point of view this is one call.\n"""\nimport torch\n'
        _rc, out = self.scan(**{"train__py": body})
        self.assertEqual(sorted(out["packages"]), ["torch"])

    def test_a_commented_out_import_is_not_an_import(self):
        _rc, out = self.scan(**{"train__py": "# import tensorflow\nimport torch\n"})
        self.assertEqual(sorted(out["packages"]), ["torch"])

    def test_an_unparseable_file_still_contributes_via_the_fallback(self):
        """A partial answer beats none for a best-effort scan that says it is
        best-effort."""
        _rc, out = self.scan(**{"broken__py": "import torch\ndef (:\n"})
        self.assertIn("torch", out["packages"])

    def test_the_common_import_aliases_are_still_mapped(self):
        _rc, out = self.scan(**{"train__py": "import cv2\nimport sklearn\n"})
        self.assertEqual(sorted(out["packages"]), ["opencv-python", "scikit-learn"])


class UsageIsNotAnAnswer(CodeCase):
    """CLAUDE.md -> "Script Integration". The three init SKILL.mds already say
    "if it fails, check requirements.txt manually", which is the exit-2 route.
    A missing directory is not a package list of zero."""

    def test_no_argument_is_exit_2(self):
        rc, out, _err = run_script(SCRIPT)
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_a_missing_directory_is_exit_2(self):
        rc, _out, _err = run_script(SCRIPT, self.path("nope"))
        self.assertEqual(rc, 2)

    def test_a_real_scan_is_exit_0(self):
        rc, _out = self.scan(**{"requirements__txt": "torch\n"})
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
