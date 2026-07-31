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

Output is a JSON dict ready to merge into run.json -> code:
{
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
  1  refused — not a git work tree, or a repo with no commits. The script
     worked and the answer is no. Do not hand-write a `code` block instead;
     offer `git init` plus an initial commit.
  2  broke — bad arguments, or a code_dir that is missing or unreadable.
     Fall back to doing the work by hand.

Usage:
    python code_snapshot.py <code_dir> <run_dir> [--max-untracked-mb N]
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
        "reproducible": not skipped and (dirty_patch_path is not None or (not tracked and not included)),
        "warnings": warnings,
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
    args = ap.parse_args()

    try:
        snap = capture(args.code_dir, args.run_dir, args.max_untracked_mb)
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
