#!/usr/bin/env python3
"""Capture a self-contained snapshot of a code directory at run-time.

Records origin SHA + branch + repo URL for the working tree. If dirty, writes
`git diff HEAD` to <run_dir>/code_dirty.patch and counts changed files. The
SHA + patch pair is the reproduction contract:
  git checkout <origin_commit> && git apply <run_dir>/code_dirty.patch

Output is a JSON dict ready to merge into run.json -> code:
{
  "repo": "<git url or local path>",
  "branch": "<branch or null>",
  "origin_commit": "<SHA>",
  "dirty_patch_path": "code_dirty.patch" | null,
  "dirty_files_count": int
}

Non-git directories: refuses with an error. Initialize git on the source
tree if you need a snapshot — there's no value in capturing a tree that
can't be reproduced anyway.

Usage:
    python code_snapshot.py <code_dir> <run_dir>
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def capture(code_dir, run_dir):
    code_dir = os.path.realpath(os.path.expanduser(code_dir))
    if not os.path.isdir(code_dir):
        raise FileNotFoundError(code_dir)
    if _run(["git", "rev-parse", "--is-inside-work-tree"], code_dir).stdout.strip() != "true":
        raise RuntimeError(
            f"{code_dir} is not a git working tree. Initialize with `git init` "
            f"before launching a run — snapshot needs SHA + dirty patch to be reproducible."
        )

    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    sha = _run(["git", "rev-parse", "HEAD"], code_dir).stdout.strip()
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], code_dir).stdout.strip() or None
    if branch == "HEAD":  # detached
        branch = None
    origin = _run(["git", "config", "--get", "remote.origin.url"], code_dir).stdout.strip()
    repo = origin or code_dir

    status = _run(["git", "status", "--porcelain"], code_dir).stdout
    dirty_files = sum(1 for line in status.splitlines() if line[:2].strip() and not line.startswith("??"))
    dirty_patch_path = None
    if dirty_files:
        diff = _run(["git", "diff", "HEAD"], code_dir).stdout
        if diff:
            (run_dir / "code_dirty.patch").write_text(diff)
            dirty_patch_path = "code_dirty.patch"

    return {
        "repo": repo,
        "branch": branch,
        "origin_commit": sha,
        "dirty_patch_path": dirty_patch_path,
        "dirty_files_count": dirty_files,
    }


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("Usage: code_snapshot.py <code_dir> <run_dir>\n")
        sys.exit(2)
    snap = capture(sys.argv[1], sys.argv[2])
    json.dump(snap, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
