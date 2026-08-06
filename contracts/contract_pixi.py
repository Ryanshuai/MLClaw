"""The stdlib-only contract, now that there is a manifest that could break it.

CLAUDE.md -> "Contracts" states it: standard library only, so
`python -m unittest discover` runs on a bare interpreter and CI needs no install
step. That was self-enforcing while the repo had no manifest at all — there was
nowhere to add a dependency. `pixi.toml` is somewhere.

The property worth keeping is narrow and worth stating exactly: **the environment
the contract suite runs in declares no third-party package.** Not "the repo has no
manifest", which was never the point — pinning the interpreter is strictly better
than inheriting whatever `python3` happens to be, and the repo was already wrong
about that: CI declared a 3.8 floor while the code used `dict | dict` (PEP 584)
and `str.removeprefix` (PEP 616), both 3.9. Both are valid *syntax* on 3.8, so
`compileall` passed and the floor read as tested for as long as nobody looked.

So the line these checks draw is between the default environment (nothing) and
`[feature.probes]` (the vendor SDKs the tracking probes reach for, which is why
`discover.py` degrades to `unreachable: the package is not installed` rather than
to a false `gone` when they are absent).
"""
import os
import re
import unittest

from helpers import REPO_ROOT

PIXI_TOML = os.path.join(REPO_ROOT, "pixi.toml")
PIXI_LOCK = os.path.join(REPO_ROOT, "pixi.lock")
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")

# The one feature allowed to carry packages, and the reason it exists: the service
# tracking probes shell out to `sys.executable`, so a vendor SDK has to be
# importable by the interpreter running discover.py.
PROBES_TABLES = ("feature.probes.dependencies", "feature.probes.pypi-dependencies")


def toml_tables(text):
    """-> {table_name: {key: raw_value}}. A 40-line reader instead of `tomllib`,
    because these checks must keep running on an interpreter older than 3.11 —
    the suite's whole claim is that it needs nothing installed, and that claim
    should not quietly acquire a version floor of its own."""
    tables, cur = {}, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            cur = m.group(1).strip()
            tables.setdefault(cur, {})
            continue
        if cur is None:
            continue
        kv = re.match(r'^([A-Za-z0-9_.-]+|"[^"]+")\s*=\s*(.+)$', line)
        if kv:
            tables[cur][kv.group(1).strip('"')] = kv.group(2).strip()
    return tables


class TheManifestExists(unittest.TestCase):
    """pixi.toml -> the header. The lock is the load-bearing half: it is what makes
    the environment reconstructible from the repo rather than from a named env
    somebody still has to have on their machine."""

    def test_manifest_and_lock_are_both_present(self):
        self.assertTrue(os.path.isfile(PIXI_TOML), "pixi.toml is missing")
        self.assertTrue(os.path.isfile(PIXI_LOCK),
                        "pixi.lock is missing — without it the manifest only "
                        "describes an environment, it does not reproduce one")

    def test_the_materialised_env_is_not_committed_but_the_lock_is(self):
        with open(GITIGNORE) as fh:
            ignored = fh.read()
        self.assertRegex(ignored, r"(?m)^\.pixi/?$",
                         ".pixi/ must be gitignored — it is ~200 directories of "
                         "solved environment")
        self.assertNotRegex(ignored, r"(?m)^pixi\.lock$",
                            "pixi.lock must be TRACKED; ignoring it discards the "
                            "only thing that makes the env reconstructible")


class TheDefaultEnvironmentDeclaresNothing(unittest.TestCase):
    """CLAUDE.md -> "Contracts": standard library only, which is why the suite is
    trustworthy anywhere and CI needs no install step. One package in
    `[dependencies]` and every later reader has to wonder whether a green run
    proves anything about their machine.
    """

    def setUp(self):
        with open(PIXI_TOML) as fh:
            self.tables = toml_tables(fh.read())

    def test_dependencies_holds_the_interpreter_and_nothing_else(self):
        deps = self.tables.get("dependencies", {})
        self.assertEqual(sorted(deps), ["python"],
                         "the default environment must declare only the "
                         "interpreter; vendor packages belong in "
                         "[feature.probes]")

    def test_no_package_table_outside_probes_carries_anything(self):
        offenders = {}
        for name, body in self.tables.items():
            if not name.endswith(("dependencies", "pypi-dependencies")):
                continue
            if name in ("dependencies",) or name in PROBES_TABLES:
                continue
            if body:
                offenders[name] = sorted(body)
        self.assertEqual(offenders, {},
                         "a new package table appeared outside "
                         "[feature.probes]; if the suite now needs it, the "
                         "stdlib-only contract in CLAUDE.md changed and should "
                         "be edited first")

    def test_probes_is_the_only_extra_environment_and_it_is_opt_in(self):
        """`pixi run test` must not silently resolve to an environment carrying
        SDKs — otherwise the honest `unreachable: the package is not installed`
        path stops being what a default run exercises."""
        envs = self.tables.get("environments", {})
        self.assertEqual(sorted(envs), ["probes"], f"unexpected environments: {envs}")

    def test_the_suite_task_is_the_command_ci_runs(self):
        """A task that drifts from CI's invocation is a task that passes locally
        while CI runs something else. `-p` in particular: the default pattern is
        test*.py and would report a green run over zero checks."""
        task = self.tables.get("tasks", {}).get("test", "")
        self.assertIn("unittest discover", task)
        self.assertIn("-p", task)
        self.assertIn("contract_*.py", task)


class EveryThirdPartyImportIsOptional(unittest.TestCase):
    """The manifest is one half; what the code imports is the other. A dependency
    can arrive without touching pixi.toml at all — `import requests` at the top of
    a script, and the default environment stops being able to run it while the
    manifest still looks clean.

    But the contract is not "no third-party imports". The repo has two on purpose
    and they are correct: `capture_env.py` reaches for `torch` to record GPU facts,
    and `ingest.py` for `tensorboard` to read event files. Both sit inside a `try`
    and both degrade to a stated answer when absent — which is the same discipline
    `discover.py`'s service probes follow ("the package is not installed in this
    interpreter — the project may be perfectly fine", never a false `gone`).

    So what must hold is: **an import the default environment cannot satisfy is
    guarded.** Parsed with `ast` rather than by regex, because a line-based reader
    matched wrapped prose inside docstrings — "…from a machine holding zero
    scenes)." — and a check that cries wolf on documentation gets switched off.
    """

    @staticmethod
    def _repo_local_modules():
        """Every module name that resolves to a file **inside this repo**.

        Was a hand-written list of eight names, and a hand-written list of
        repo-internal facts is a list that goes stale on the next split: pulling
        the probe section out of `discover.py` created `_probes`, and the check
        reported it as an unguarded third-party import -- a defect in the code
        where there was none, and the suggested fix (wrap it in a try) would have
        been wrong.

        The criterion is computable and always current: a sibling `.py` under
        `lifecycle/scripts/` or `contracts/` is repo-local by construction, since
        that is exactly what `sys.path.insert` makes importable.
        """
        names = set()
        for base in ("lifecycle/scripts", "contracts"):
            for dirpath, _d, files in os.walk(os.path.join(REPO_ROOT, base)):
                if "__pycache__" in dirpath:
                    continue
                names |= {f[:-3] for f in files if f.endswith(".py")}
        return names

    @staticmethod
    def _guarded_and_bare(tree):
        """-> (guarded, bare) module names. Guarded = the import statement sits in
        the body of a `try`, so an ImportError is the author's to have handled.

        `visit` classifies the node it is GIVEN and only then recurses. The first
        version inspected `iter_child_nodes(node)` instead, so handing it an
        `ast.Import` looked at that import's children — of which there are none —
        and every try-guarded import vanished. The `optional` tally that rested on
        it was therefore vacuously satisfied. The negative controls in
        `test_the_walk_detects_what_it_claims_to` exist because of that.
        """
        import ast
        guarded, bare = set(), set()
        try_kinds = (ast.Try, getattr(ast, "TryStar", ast.Try))

        def visit(node, in_try):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    (guarded if in_try else bare).add(alias.name.split(".")[0])
                return
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    (guarded if in_try else bare).add(node.module.split(".")[0])
                return
            if isinstance(node, try_kinds):
                for sub in node.body:
                    visit(sub, True)
                for sub in (list(node.handlers) + list(node.orelse)
                            + list(node.finalbody)):
                    visit(sub, in_try)
                return
            for child in ast.iter_child_nodes(node):
                visit(child, in_try)

        visit(tree, False)
        return guarded, bare

    def test_the_walk_detects_what_it_claims_to(self):
        """A check that finds nothing passes for the same reason a correct one
        does. These three controls are what tell the difference — and one of them
        was already red: the guarded case returned nothing at all.
        """
        import ast
        cases = {
            "import requests":                          ((), ("requests",)),
            "def f():\n    import numpy":               ((), ("numpy",)),
            "try:\n    import torch\nexcept Exception:\n    pass":
                                                        (("torch",), ()),
            "def f():\n    try:\n        from tb.x import Y\n"
            "    except ImportError:\n        pass":     (("tb",), ()),
            "import os\nimport json":                   ((), ()),
        }
        for src, (want_g, want_b) in cases.items():
            with self.subTest(src=src.replace("\n", "\\n")[:44]):
                g, b = self._guarded_and_bare(ast.parse(src))
                self.assertEqual(sorted(x for x in g if x != "os"), sorted(want_g))
                self.assertEqual(sorted(x for x in b if x not in ("os", "json")),
                                 sorted(want_b))

    def test_an_import_the_default_env_cannot_satisfy_is_guarded(self):
        import ast
        import sys
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        if not stdlib:
            self.skipTest("sys.stdlib_module_names needs 3.10+")
        allowed = stdlib | self._repo_local_modules() | {"__future__"}

        unguarded, optional = {}, {}
        for base in ("lifecycle/scripts", "contracts"):
            for dirpath, _d, files in os.walk(os.path.join(REPO_ROOT, base)):
                if "__pycache__" in dirpath:
                    continue
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    rel = os.path.relpath(path, REPO_ROOT)
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read(), filename=path)
                    guarded, bare = self._guarded_and_bare(tree)
                    third_bare = sorted(m for m in bare if m not in allowed)
                    third_guarded = sorted(m for m in guarded if m not in allowed)
                    if third_bare:
                        unguarded[rel] = third_bare
                    if third_guarded:
                        optional[rel] = third_guarded

        self.assertEqual(unguarded, {},
                         "an unguarded third-party import appeared. Either wrap it "
                         "in a try and state what is lost when it is missing, or "
                         "edit CLAUDE.md's stdlib-only contract first — the "
                         "contract is upstream of the code")
        # Not an assertion, a record: these are the two the repo means to have, and
        # a third arriving silently is worth seeing in the failure output above.
        self.assertLessEqual(len(optional), 4,
                             f"optional third-party imports have grown: {optional}")


if __name__ == "__main__":
    unittest.main()
