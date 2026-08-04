#!/usr/bin/env python3
"""Capture a self-contained snapshot of a code directory at run-time.

Records origin SHA + branch + repo URL for the working tree, plus a patch
holding everything that differs from that SHA. The SHA + patch pair is the
reproduction contract, and it is applied **from `code_dir`**:

    cd <code_dir> && git checkout <origin_commit> && git apply <run_dir>/code_dirty.patch

**Everything is scoped to `code_dir`, which is usually not the repo root.**
`/project-init` places code at `stages/<stage>/code/` for the `github`,
`server` and `null` source modes, and that path lives inside the *project's
own* git repo — only `local` symlinks out to a standalone one. An unscoped
`git diff HEAD` there sweeps the whole project into the patch: `project.json`,
the other stages, whatever else is dirty. `git apply` from `code_dir` then
silently *skips* every one of those entries (it resolves patch paths against
the repo root and discards what lies outside cwd) while still exiting 0, and
the run record counts project config as part of the code — so both halves of
the failure are silent. The diff, the untracked scan and the intent-to-add
therefore all run under a `.` pathspec, and the offset is recorded as
`repo_subdir` (null when `code_dir` IS the repo root) so a reader can tell
which of the two layouts produced the record.

Patch paths stay repo-relative rather than being rewritten with
`git diff --relative`: `git apply` reads them against the repo root whichever
directory it is invoked from, so the repo-relative form applies cleanly from
`code_dir` *and* from the repo root, while a `--relative` patch applies nothing
at all from `code_dir` and reports success.

`origin_commit` stays repository-wide — a subdirectory has no history of its
own. Scoping the patch does not make the SHA stable against commits that touch
only other parts of the project repo, so two runs over identical code can still
land in different `--commit` buckets if the project repo was committed to in
between.

**Untracked files are part of the diff.** A brand-new `model_v2.py` that was
never `git add`ed is invisible to `git diff HEAD`, so a naive snapshot records
`dirty_files_count: 0` and reproduces a *different* code tree without ever
erroring. To include them without touching the user's index, the untracked
set is intent-to-added into a throwaway copy of the index and the diff is
taken against that.

Files too large to embed (default > 5 MB) are NOT put in the patch. They are
listed in `untracked_skipped` and `reproducible` flips to false — a snapshot
that silently drops a file is the exact failure this script exists to prevent,
so it is reported rather than hidden.

**Some stages have no code tree at all.** When the thing being evaluated is a
deployed artifact and the "eval code" is an installed framework's CLI
(`yolo val model=… data=…`), there is nothing to `git init`: the code lives in
site-packages and is owned by the package manager. Refusing there would be
correct about the tree and wrong about the world — the run is perfectly
reproducible, just under a DIFFERENT CONTRACT. `--framework <pkg>==<version>`
records that contract instead:

    git tree   ->  git checkout <sha> && git apply <patch>
    framework  ->  <env_manager> install <pkg>==<version>

`kind` says which one the record is, and it is the whole point of the field. A
`framework` record has `origin_commit: null` **by construction**, and without
`kind` a later reader cannot tell that from a capture that failed. (A record with
no `kind` at all predates this and is a git-tree record.)

The framework contract is weaker in one specific way and the warning says so:
**a local edit is invisible.** A monkeypatched site-packages file or a stray
`sitecustomize.py` leaves no trace, where a git tree would at least produce a
dirty patch. A pinned version is therefore necessary and not sufficient, which is
also why an unversioned `--framework` is refused rather than recorded.

`code_dir` stays required even in framework mode, so run-mechanics' "the same call
applies to all run skills — no per-skill variant" keeps holding. It is not read.

Output is a JSON dict ready to merge into run.json -> code:
{
  "kind": "git" | "framework",     # WHICH reproduction contract this record is
  "repo": "<git url, or the repo root path when there is no origin>",
  "branch": "<branch or null>",
  "origin_commit": "<SHA>",
  "repo_subdir": "stages/training/code" | null,
  "dirty_patch_path": "code_dirty.patch" | null,
  "dirty_files_count": int,        # files in the patch, tracked + untracked
  "untracked_skipped": [{"path": str, "size_bytes": int}],
  "reproducible": bool,            # patch + SHA fully reconstruct the tree
  "warnings": [str]
}

No field here restates another. `is_clean`, `tracked_dirty_count` and
`untracked_files_count` used to be returned as well and were removed: all
three are derivable, and CLAUDE.md -> "Script Integration" has an agent
hand-filling run.json whenever a script fails, where a written-by-hand
`is_clean: true` sitting next to `dirty_files_count: 3` is a contradiction
that nothing raises on. The tracked/untracked split survives in the warning
strings, which state the counts in prose where they cannot be mistaken for
independent facts.

Exit codes, per CLAUDE.md -> "Script Integration":
  0  snapshot captured; warnings, if any, on stderr
  1  refused — not a git work tree, or a repo with no commits, or a
     `--framework` with no pinned version. The script worked and the answer is
     no. Do not hand-write a `code` block instead; offer `git init` plus an
     initial commit — or, when the stage genuinely has no tree, `--framework`.
  2  broke — bad arguments, or a code_dir that is missing or unreadable.
     Fall back to doing the work by hand.

Usage:
    python code_snapshot.py <code_dir> <run_dir> [--max-untracked-mb N]
                            [--framework <pkg>==<version>]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

DEFAULT_MAX_UNTRACKED_MB = 5.0

# Scoping to code_dir: a `.` pathspec, evaluated against cwd, which excludes
# everything outside the code directory from both the walk and the patch. It is
# deliberately *not* `git diff --relative`: that rewrites the emitted paths to be
# code_dir-relative, and `git apply` run from a subdirectory of a repo resolves
# patch paths against the **repo root**, then skips whatever falls outside cwd —
# so a --relative patch applies nothing and still exits 0. Repo-relative paths
# apply cleanly from code_dir *and* from the repo root. At the repo root the
# pathspec is a no-op and the two forms coincide.
SCOPE = ["--", "."]


class SnapshotError(Exception):
    """A condition that makes an honest snapshot impossible. Refusal, exit 1."""


class SnapshotUsageError(SnapshotError):
    """The script was called wrong or the path is unusable. Breakage, exit 2."""


def _run(cmd, cwd, env=None):
    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=full_env)


def _run_z(cmd, cwd):
    """Run a git command with -z output, return list of NUL-separated fields."""
    out = _run(cmd, cwd).stdout
    return [f for f in out.split("\0") if f]


def _strip_prefix(path, prefix):
    """Repo-root-relative -> code_dir-relative. None if outside code_dir."""
    if not prefix:
        return path
    if path.startswith(prefix):
        return path[len(prefix):]
    return None


def _scan_status(code_dir, prefix):
    """One `git status --porcelain -z -uall -- .` -> (tracked_dirty, untracked).

    Replaces a `git diff HEAD --name-only` + `git ls-files --others` pair with a
    single working-tree walk. `-uall` lists untracked files one by one instead
    of collapsing directories, and still honours .gitignore — ignored files
    require `--ignored`, which is deliberately not passed.

    `-z` framing: each record is `XY<space><path>`, NUL-terminated, and a rename
    or copy (X in "RC") is followed by a *second* NUL-terminated field holding
    the source path. The status code is always the first two characters, so a
    path can never be misread as a record header.

    Porcelain v1 paths are relative to the repo root whatever the cwd is, and
    are not affected by status.relativePaths — hence `prefix`, the code_dir
    offset, is stripped here to match the cwd-relative form that `git add -N`
    pathspecs, `os.path.getsize` and the patch itself all use.

    Two record shapes cannot be counted from the status code alone, and both are
    resolved with a single extra `git diff HEAD --name-only` over just those
    paths — so `dirty_files_count` can never disagree with the patch it
    describes. Clean and ordinarily-dirty trees never pay for it:

    - Both X and Y non-space (`MM`, `AD`, `UU`): the index differs from HEAD
      *and* the worktree differs from the index, which does not settle whether
      the worktree differs from HEAD. Staging an edit and then reverting the
      file lands here, and `git diff HEAD` rightly shows nothing for it.
    - X in "RC": whether a rename shows up in the patch as one entry
      (`a/old b/new`) or as a delete plus an add depends on git's similarity
      detection, which is a property of the content, not of the status code.
    """
    out = _run(["git", "status", "--porcelain", "-z", "-uall"] + SCOPE, code_dir).stdout
    fields = out.split("\0")

    tracked, untracked, ambiguous = [], [], []
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 4:
            continue
        x, y, path = rec[0], rec[1], rec[3:]
        source = None
        if x in "RC" and i < len(fields):
            source = fields[i]
            i += 1

        paths = [path] if x != "R" else [path, source]
        rel = [_strip_prefix(p, prefix) for p in paths if p]
        rel = [p for p in rel if p is not None]
        if not rel:
            continue

        if x == "?" and y == "?":
            untracked.extend(rel)
        elif x in "RC" or (x != " " and y != " "):
            ambiguous.extend(rel)
        else:
            tracked.extend(rel)

    if ambiguous:
        # `--relative` here, unlike on the patch diff, because this output is
        # only ever compared against `ambiguous` and never written to disk.
        ambiguous = list(dict.fromkeys(ambiguous))
        differs = set(_run_z(
            ["git", "diff", "HEAD", "--name-only", "-z", "--relative", "--"] + ambiguous,
            code_dir))
        tracked.extend(p for p in ambiguous if p in differs)

    return list(dict.fromkeys(tracked)), list(dict.fromkeys(untracked))


def _patch_with_untracked(code_dir, untracked, index_path, toplevel, prefix):
    """`git diff HEAD --binary`, scoped to code_dir, with `untracked` added -N.

    Uses a throwaway copy of the index (GIT_INDEX_FILE) so the user's real
    index is never modified — for `local` code sources this is their working
    repo, open in their editor.

    Falls back to per-file `git diff --no-index` if the index copy fails, so a
    weird git setup degrades to a still-applicable patch rather than to a
    silently incomplete one. The fallback runs from the repo root with the
    offset re-attached, so its synthesized new-file entries carry the same
    repo-relative paths as the tracked half rather than a second convention
    inside one patch file.

    The plain tracked diff is only computed on that fallback. It used to be
    taken unconditionally at the top and then discarded whenever the temp-index
    path succeeded, which is the normal case — one full tree traversal per
    snapshot, thrown away.
    """
    diff_cmd = ["git", "diff", "HEAD", "--binary"] + SCOPE
    if not untracked:
        return _run(diff_cmd, code_dir).stdout

    tmpdir = tempfile.mkdtemp(prefix="mlclaw_snap_")
    try:
        tmp_index = os.path.join(tmpdir, "index")
        if index_path and os.path.isfile(index_path):
            shutil.copy2(index_path, tmp_index)
            env = {"GIT_INDEX_FILE": tmp_index}
            add = _run(["git", "add", "-N", "--"] + untracked, code_dir, env=env)
            if add.returncode == 0:
                full = _run(diff_cmd, code_dir, env=env)
                if full.returncode == 0:
                    return full.stdout

        # Fallback: synthesize new-file patches one at a time.
        parts = []
        tracked_diff = _run(diff_cmd, code_dir).stdout
        if tracked_diff:
            parts.append(tracked_diff)
        for rel in untracked:
            # --no-index exits 1 when files differ, which is the normal case here.
            d = _run(["git", "diff", "--no-index", "--binary", os.devnull, prefix + rel],
                     toplevel)
            if d.stdout:
                parts.append(d.stdout)
        return "".join(parts)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def capture(code_dir, run_dir, max_untracked_mb=DEFAULT_MAX_UNTRACKED_MB):
    code_dir = os.path.realpath(os.path.expanduser(code_dir))
    if not os.path.isdir(code_dir):
        raise SnapshotUsageError(f"code_dir does not exist: {code_dir}")

    # One call answers three questions: is this a work tree, where is its root
    # (so the snapshot can be scoped to code_dir), and where is its index (so
    # the intent-to-add can be done on a copy).
    probe = _run(["git", "rev-parse", "--show-toplevel", "--git-path", "index"], code_dir)
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) < 2 or not lines[0].strip():
        raise SnapshotError(
            f"{code_dir} is not a git working tree. Initialize with `git init` "
            f"before launching a run — snapshot needs SHA + dirty patch to be reproducible."
        )
    toplevel = os.path.realpath(lines[0].strip())
    index_path = lines[1].strip()
    if index_path and not os.path.isabs(index_path):
        index_path = os.path.join(code_dir, index_path)

    # Fails exactly when the repo has no commits, which is the check; the same
    # call yields both the SHA and the branch.
    head = _run(["git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD"], code_dir)
    if head.returncode != 0:
        raise SnapshotError(
            f"{code_dir} is a git repo with no commits. There is no SHA to pin the "
            f"snapshot to — make an initial commit before launching a run."
        )
    head_lines = head.stdout.splitlines()
    sha = head_lines[0].strip()
    branch = (head_lines[1].strip() if len(head_lines) > 1 else "") or None
    if branch == "HEAD":  # detached
        branch = None

    offset = os.path.relpath(code_dir, toplevel).replace(os.sep, "/")
    repo_subdir = None if offset in (".", "") else offset
    prefix = "" if repo_subdir is None else repo_subdir + "/"

    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    origin = _run(["git", "config", "--get", "remote.origin.url"], code_dir).stdout.strip()
    repo = origin or toplevel

    tracked, untracked_all = _scan_status(code_dir, prefix)

    limit = max_untracked_mb * 1024 * 1024
    included, skipped = [], []
    for rel in untracked_all:
        try:
            size = os.path.getsize(os.path.join(code_dir, rel))
        except OSError:
            # Broken symlink or a file that vanished mid-scan. Record it as
            # skipped rather than dropping it — it still differs from HEAD.
            skipped.append({"path": rel, "size_bytes": None})
            continue
        if size <= limit:
            included.append(rel)
        else:
            skipped.append({"path": rel, "size_bytes": size})

    warnings = []
    dirty_patch_path = None
    if tracked or included:
        diff = _patch_with_untracked(code_dir, included, index_path, toplevel, prefix)
        if diff:
            (run_dir / "code_dirty.patch").write_text(diff, encoding="utf-8")
            dirty_patch_path = "code_dirty.patch"
        else:
            warnings.append(
                f"{len(tracked)} tracked + {len(included)} untracked file(s) differ from "
                f"{sha[:8]} but git produced an empty diff — patch not written, run is "
                f"NOT reproducible."
            )

    if skipped:
        names = ", ".join(s["path"] for s in skipped[:5])
        more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
        warnings.append(
            f"{len(skipped)} untracked file(s) exceed the {max_untracked_mb} MB embed limit "
            f"and are NOT in the patch: {names}{more}. Commit them, gitignore them, or "
            f"raise --max-untracked-mb — otherwise this run cannot be reproduced from the snapshot."
        )
    if included:
        warnings.append(
            f"{len(included)} untracked file(s) were embedded in the patch as new files. "
            f"They are invisible to `git diff HEAD` and would otherwise have been lost."
        )

    return {
        "repo": repo,
        "branch": branch,
        "origin_commit": sha,
        "repo_subdir": repo_subdir,
        "dirty_patch_path": dirty_patch_path,
        "dirty_files_count": len(tracked) + len(included),
        "untracked_skipped": skipped,
        "kind": "git",
        "reproducible": not skipped and (dirty_patch_path is not None or (not tracked and not included)),
        "warnings": warnings,
    }


FRAMEWORK_BLIND_SPOT = (
    "a framework record pins the package and CANNOT see a local edit: a "
    "monkeypatched site-packages file or a stray sitecustomize.py leaves no "
    "trace here, where a git tree would have produced a dirty patch. The pin is "
    "necessary and not sufficient"
)


def capture_framework(spec, run_dir):
    """-> the `code` block for a stage whose code is an installed package.

    Refuses an unpinned spec. `ultralytics` names the framework; only
    `ultralytics==8.4.40` is a reproduction contract, and recording the first as
    though it were the second produces a run that reads as reproducible and is
    not. Same bar as the SHA a git record refuses to invent.

    The version is taken from the caller, never resolved here: the version that
    matters is the one in the environment the run will execute in, and this
    script does not know which interpreter that is. Guessing from its own
    `sys.path` would record a fact about the wrong machine.
    """
    if not os.path.isdir(os.path.expanduser(run_dir)):
        raise SnapshotUsageError(f"run_dir does not exist: {run_dir}")
    name, sep, version = str(spec).partition("==")
    name, version = name.strip(), version.strip()
    if not name:
        raise SnapshotUsageError("--framework needs a package name")
    if not sep or not version:
        raise SnapshotError(
            f"--framework {spec!r} has no pinned version. `{name}` names a "
            f"framework; `{name}==<version>` is a reproduction contract. Resolve "
            f"it in the environment the run will use (pip show / importlib."
            f"metadata.version) and pass that."
        )
    return {
        "kind": "framework",
        "framework": name,
        "framework_version": version,
        # Null by construction, and `kind` is what says so. A framework stage has
        # no repo, no branch and no SHA — there is nothing that failed here.
        "repo": None,
        "branch": None,
        "origin_commit": None,
        "repo_subdir": None,
        "dirty_patch_path": None,
        # NULL, NOT ZERO. "There is no tree" and "the tree was clean" are
        # different facts, and 0 here would assert the second — the same rule as
        # a byte count that was never measured.
        "dirty_files_count": None,
        "untracked_skipped": [],
        # The contract does rebuild the code: install that version. True is the
        # honest value, and the warning carries what it cannot cover.
        "reproducible": True,
        "warnings": [FRAMEWORK_BLIND_SPOT],
    }


def _fail(message, code):
    json.dump({"error": message}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stderr.write(f"code_snapshot: {message}\n")
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("code_dir")
    ap.add_argument("run_dir")
    ap.add_argument("--max-untracked-mb", type=float, default=DEFAULT_MAX_UNTRACKED_MB)
    ap.add_argument("--framework", default=None, metavar="PKG==VERSION",
                    help="this stage has no code tree — its code is an installed "
                         "package and its entry point that package's CLI. Records "
                         "the install-a-version contract instead of SHA + patch.")
    args = ap.parse_args()

    try:
        snap = (capture_framework(args.framework, args.run_dir) if args.framework
                else capture(args.code_dir, args.run_dir, args.max_untracked_mb))
    except SnapshotUsageError as e:
        # Broken input, not a verdict — the caller should fall back to doing
        # the snapshot by hand.
        _fail(str(e), 2)
    except SnapshotError as e:
        # Graceful, parseable refusal. The caller must not "fall back" past it.
        _fail(str(e), 1)
    except Exception:  # noqa: BLE001 — anything unforeseen is breakage, not a verdict
        sys.stderr.write(traceback.format_exc())
        _fail("code_snapshot crashed; see stderr for the traceback", 2)

    json.dump(snap, sys.stdout, indent=2)
    sys.stdout.write("\n")
    for w in snap["warnings"]:
        sys.stderr.write(f"code_snapshot: warning: {w}\n")


if __name__ == "__main__":
    main()
