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

Some script directories have hyphens (`scripts/train-run/`), which are
not importable package names, so scripts are loaded by file path throughout.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")

# The harness reads and writes utf-8 regardless of the host's codepage. `-X utf8`
# on the child (see run_script) covers the scripts run as subprocesses; this covers
# the ones `load_script` calls IN-PROCESS, whose `print()` goes to the runner's own
# stdout. On a cp1252 console that raises UnicodeEncodeError on the first arrow or
# em-dash a script prints — a check failing because of the terminal it ran in, which
# tells the reader nothing about the contract it was supposed to be checking.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def requires_posix_shims():
    """Skip a check whose fixture fakes a CLI with a `#!/bin/sh` file on PATH.

    That technique needs a kernel that honours a shebang and an executable bit,
    so on Windows the shim is never run — the probe finds no `aws` at all and the
    check fails describing a credential precedence that was never exercised.

    Skip, not fail, and the distinction is the repo's own: a check that could not
    run has *not* found a breach, and reporting it as one trains the reader to
    dismiss red. Same shape as `unreachable` vs `gone` in `/discover`, which is
    incidentally what several of these checks are about.
    """
    if os.name != "posix":
        raise unittest.SkipTest(
            "fixture fakes a CLI with a #!/bin/sh file on PATH; needs POSIX")


def requires_symlinks(target, link):
    """`os.symlink`, or skip when the OS refuses to let this process make one.

    Windows needs Developer Mode or an elevated process (WinError 1314). The
    contract being checked — an output path must not reach its own input, even
    through a link — is real everywhere; the *fixture* is what the host declines.
    Skip rather than fail, for the same reason as `requires_posix_shims`.
    """
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError) as exc:
        raise unittest.SkipTest(f"cannot create a symlink on this host: {exc}")


def requires_rsync_accepting_native_paths(tmpdir):
    """Skip unless this host's rsync moves a file between two native paths.

    Probed rather than keyed on `os.name`, because the thing that breaks is a
    *combination*: an MSYS rsync spawned from a native Windows interpreter gets
    `D:\\data\\src` and reads `D:` as a remote host, so it tries ssh and exits 1.
    The same rsync driven from a shell that rewrites paths works fine, which is
    why "is there an rsync" is the wrong question — `shutil.which` says yes on
    exactly the host where this fails.

    The limitation is real and belongs to `/data-collect`, whose design puts the
    transfer in rsync's hands on purpose. Recording it as a skip says the check
    did not run; letting it fail would say the contract is broken, and it is not.
    """
    src = os.path.join(tmpdir, "_rsync_probe_src")
    dst = os.path.join(tmpdir, "_rsync_probe_dst")
    os.makedirs(src, exist_ok=True)
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(src, "probe"), "w", encoding="utf-8") as fh:
        fh.write("probe\n")
    try:
        p = subprocess.run(["rsync", "-a", src + os.sep, dst + os.sep],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise unittest.SkipTest(f"rsync unusable on this host: {exc}")
    if p.returncode != 0:
        raise unittest.SkipTest(
            "this host's rsync does not accept native paths from a spawned "
            f"process (exit {p.returncode}): {(p.stderr or '').strip()[:200]}")


def load_script(relpath):
    """Import a script by path, e.g. load_script('shared/create_run.py').

    **A fresh module every call, siblings included.** A script that imports a
    sibling out of its own directory gets that sibling from `sys.modules`, so a
    plain re-exec hands back a module whose top half is new and whose bottom half
    is whatever a previous test left there. A test that mutates a module-level
    table then leaks into every later test, and the leak reads as a real failure
    in an unrelated place -- which is how it was found: splitting the probe
    section out of `discover.py` made a `TRACKING["futurething"]` written by one
    test show up in another's assertion.

    Half-fresh is the worse answer, not the safer one: it looks exactly like
    fresh right up to the first module-level mutation.
    """
    path = os.path.join(SCRIPTS, relpath)
    if not os.path.isfile(path):
        raise unittest.SkipTest(f"script not present yet: {relpath}")
    for cached in [k for k, m in sys.modules.items()
                   if getattr(m, "__file__", None)
                   and os.path.dirname(os.path.abspath(m.__file__))
                   == os.path.dirname(os.path.abspath(path))]:
        del sys.modules[cached]
    name = "mlclaw_" + relpath.replace("/", "_").replace("-", "_")[:-3]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_script(relpath, *args):
    """Run a script as a subprocess. -> (returncode, parsed_stdout_or_raw, stderr).

    `sys.executable`, never a name off PATH. A hardcoded `python3` does not exist
    on Windows, so every check that shells out errored at spawn — 400 of 666, and
    they read as errors rather than skips, which is why the suite had been failing
    loudly enough to stop being read. It is also the wrong interpreter even where
    it resolves: the suite pins one via pixi, and `python3` off PATH is whatever
    the shell finds. Same fix as the one already made in the code-snapshot checks;
    this is the shared helper that was missed.

    `-X utf8` because the parent decodes utf-8 and the child must therefore emit
    it. Without it the child's stdio follows the ambient codepage — cp1252 on this
    machine — and the scripts print em-dashes, so the decode blows up in
    subprocess's reader *thread*. That failure mode is worth naming: the exception
    lands off the main thread, `p.stdout` silently becomes `None`, and every check
    then dies at `.strip()` on a line that has nothing to do with encoding. Forcing
    the child's mode is what makes the checks read the same bytes on every host.

    It does not fix the underlying thing, and it should not: MLClaw's scripts
    printing non-ASCII to a cp1252 console is a real defect on Windows, owned by
    the scripts. Pinning it here keeps the harness from depending on the host's
    codepage, which is a separate contract from what the scripts print.
    """
    path = os.path.join(SCRIPTS, relpath)
    p = subprocess.run([sys.executable, "-X", "utf8", path, *[str(a) for a in args]],
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

    # Fixtures are utf-8 on both sides, always. Left to the platform default the
    # scripts and the checks disagree about the same bytes: a fixture written here
    # as cp1252 hands `0x97` to a script that reads utf-8, and a brief the script
    # wrote as utf-8 reads back here as `â€”`. Both surfaced as check failures
    # about carried sections and dagger markers — nothing about encoding.
    def write(self, relpath, content):
        full = self.path(relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return full

    def write_json(self, relpath, obj):
        return self.write(relpath, json.dumps(obj, indent=2))

    def read(self, *parts):
        with open(self.path(*parts), encoding="utf-8") as f:
            return f.read()

    def read_json(self, relpath):
        with open(self.path(relpath), encoding="utf-8") as f:
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
