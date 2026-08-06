"""Shared fixtures for the MLClaw contract checks.

These are not unit tests. Each file here is the executable form of a contract
stated in CLAUDE.md, and every check class names the section it enforces. That
citation is the admission rule: a check that cannot point at a written contract
is either a missing line in CLAUDE.md or padding — decide which, don't leave it.

It also decides what to do when one fails. A unit test failing asks "how do I
make this pass"; a contract check failing asks "is the code in breach, or has
the contract changed?" If the contract changed, edit CLAUDE.md and let the check
follow. The contract is upstream; this directory is downstream.

Standard library only, which is the claim that matters and is narrower than the
one this said before. The repo now has a `pixi.toml`, and that does not weaken it:
pixi supplies the *interpreter*, while the suite still imports nothing outside the
standard library — so `pixi run test` installs a Python and nothing else, and the
same command works against any interpreter you point at it. `contract_pixi.py`
enforces the narrow claim (default environment declares only python; a
third-party import must be guarded by a `try`).

What pinning replaced was worse than an unpinned version: CI declared a 3.8 floor
the code had already left — `dict | dict` (PEP 584) and `str.removeprefix`
(PEP 616) are 3.9, and both are valid *syntax* on 3.8, so `compileall` passed and
the floor read as tested. Note the `-p`: the default pattern is `test*.py` and
would silently find nothing.

Some script directories have hyphens (`lifecycle/scripts/train-run/`), which are
not importable package names, so scripts are loaded by file path throughout.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "lifecycle", "scripts")


def load_script(relpath):
    """Import a script by path, e.g. load_script('shared/create_run.py')."""
    path = os.path.join(SCRIPTS, relpath)
    if not os.path.isfile(path):
        raise unittest.SkipTest(f"script not present yet: {relpath}")
    name = "mlclaw_" + relpath.replace("/", "_").replace("-", "_")[:-3]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_script(relpath, *args):
    """Run a script as a subprocess. -> (returncode, parsed_stdout_or_raw, stderr)."""
    path = os.path.join(SCRIPTS, relpath)
    p = subprocess.run(["python3", path, *[str(a) for a in args]],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        out = p.stdout
    return p.returncode, out, p.stderr


class TempDirCase(unittest.TestCase):
    """Gives each test an isolated scratch directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlclaw_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write(self, relpath, content):
        full = self.path(relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def write_json(self, relpath, obj):
        return self.write(relpath, json.dumps(obj, indent=2))

    def read(self, *parts):
        with open(self.path(*parts)) as f:
            return f.read()

    def read_json(self, relpath):
        with open(self.path(relpath)) as f:
            return json.load(f)


class GitRepoCase(TempDirCase):
    """A real git working tree, because the snapshot contract is about real git."""

    def git(self, *args, cwd=None):
        p = subprocess.run(["git", *args], cwd=cwd or self.repo,
                           capture_output=True, text=True, encoding="utf-8")
        return p

    def make_repo(self, name="repo", files=None, gitignore=None, code_subdir=None):
        """Build a git work tree; return the directory a snapshot should target.

        Default (`code_subdir=None`) is the `local` source mode: the code dir
        *is* the repo root, so `self.repo` and `self.repo_root` coincide.

        Passing `code_subdir` reproduces the layout /project-init creates for
        the `github`, `server` and `null` modes — the repo root is the
        *project* and the code sits at `stages/<stage>/code` inside it. Files
        and .gitignore go under that subdir, a `project.json` is seeded at the
        root to stand in for everything the code dir must not claim as its own,
        and `self.repo` (what a check hands to `capture()`) points at the
        subdir while `self.repo_root` points at the git root. `self.git()`
        still defaults to `self.repo`, which is what a reproduction check
        wants: the contract is `git apply` run from the code dir.
        """
        self.repo_root = self.path(name)
        os.makedirs(self.repo_root, exist_ok=True)
        for cfg in (("init", "-q", "."),
                    ("config", "user.email", "test@mlclaw.local"),
                    ("config", "user.name", "MLClaw Test"),
                    ("config", "commit.gpgsign", "false")):
            self.git(*cfg, cwd=self.repo_root)

        base = name if code_subdir is None else f"{name}/{code_subdir}"
        self.repo = self.path(name) if code_subdir is None else self.path(name, code_subdir)
        os.makedirs(self.repo, exist_ok=True)
        if code_subdir is not None:
            self.write(f"{name}/project.json", '{"name": "proj", "env_name": ""}\n')
        if gitignore:
            self.write(f"{base}/.gitignore", gitignore)
        for rel, content in (files or {"train.py": "print('v1')\n"}).items():
            self.write(f"{base}/{rel}", content)

        self.git("add", "-A", cwd=self.repo_root)
        self.git("commit", "-qm", "init", cwd=self.repo_root)
        self.base_sha = self.git("rev-parse", "HEAD", cwd=self.repo_root).stdout.strip()
        return self.repo
