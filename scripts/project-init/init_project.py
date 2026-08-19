"""Create project directory structure, copy templates, git init.

Usage:
    python init_project.py '<project_json_str>' <mlclaw_root>

Emits a single JSON object on stdout describing what happened. Exit code is 0
on success; a non-zero exit means the project was NOT created (see the JSON
object on stderr for the reason).
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

# Ignore rules are directory-first on purpose. Data lives in stages/*/data/,
# run artifacts in stages/*/runs/ and stages/*/artifacts/ — those directory
# rules already cover every bulky input/output MLClaw itself produces.
# Do NOT re-add global extension ignores for images / video / csv / parquet:
# they also swallow report charts, figures and result tables a user drops at
# the project root or under docs/, and those *should* be committed. Only
# extensions that are never wanted anywhere (model weights) stay global.
GITIGNORE = """\
# Large files — model weights, checkpoints.
# Extension rules are reserved for things that are never wanted anywhere in
# the repo. Everything else (data, images, csv, run outputs) is excluded by
# the directory rules below, so charts/tables at the project root or under
# docs/ stay committable.
*.onnx
*.pt
*.pth
*.engine
*.trt
*.tflite
*.safetensors
*.ckpt
*.bin

# Run outputs, artifacts, and data
stages/*/runs/
stages/*/artifacts/
stages/*/data/

# Secrets — NEVER commit
secrets.json

# OS / IDE
.DS_Store
Thumbs.db
__pycache__/
*.pyc
.vscode/
.idea/
"""

# Written into every project root. A POINTER, never a copy: the ten "Never
# silently" rules duplicated here would drift from the ones that are enforced,
# and a stale copy of a safety rule is worse than none.
#
# It exists because installing MLClaw as a plugin makes the SKILLS reach a
# project directory but not the CONTEXT they assume. `CLAUDE.md` is read from
# the working directory and its parents, so standing in a project the routing
# table and the delete rules are simply absent -- and nothing reports it.
# `references/layout.md` -> "Working directory" is where that is written down.
PROJECT_CLAUDE_MD = """\
# {name} — an MLClaw project

A **record**, not a codebase: JSON configs, run records, and what was measured.
The code that trains lives elsewhere, reached through `stages/<stage>/code/_source`.

## ‼️ Read this before touching anything here

```
{mlclaw_root}/CLAUDE.md
```

Everything that says what must never happen *silently* — deleting a checkpoint
nothing ranked, recording a metric nobody read, letting somebody's word become a
checked fact, releasing a machine you did not evacuate — is in that file and is
**absent from this directory**. Installed as a plugin, MLClaw's skills reach you
anywhere; its rules do not, because `CLAUDE.md` is read from the working
directory and its parents. Nothing reports the difference. That is the whole
reason this file exists.

Its routing table is also what says which reference to open next, and every
skill's requirement check sits one hop past it.

## If that path is wrong

Recorded when this project was created. If the MLClaw checkout moved, or this
project was copied to another machine, re-resolve and correct the line above:

```bash
python <any MLClaw checkout>/scripts/shared/workspaces.py register-tool
python <any MLClaw checkout>/scripts/shared/workspaces.py tool
```
"""


# Project-level templates copied from lifecycle/ into a new project root.
# resources.json is deliberately absent: it is workspace-level, not
# project-level (see bootstrap_workspace_resources).
PROJECT_TEMPLATES = ["history.json"]

# Run-record templates live in a stage's template dir but are instantiated
# per run by the run skill, not per stage — never copy them as stage config.
RUN_RECORD_TEMPLATES = {"refactor_run.json"}

WORKSPACE_RESOURCES = "resources.json"


class SourceLinkConflict(Exception):
    """A real file or directory occupies stages/<stage>/code/_source.

    Not auto-recoverable: removing it could destroy a code tree the user put
    there by hand, and keeping it would silently point the stage at the wrong
    repo (every later run would snapshot the wrong code). The script is
    non-interactive, so this is a hard failure.
    """

    def __init__(self, stage, path, kind):
        self.stage = stage
        self.path = path
        self.kind = kind
        super().__init__(
            "stages/%s/code/_source is an existing %s, not a symlink: %s "
            "— move or delete it, then re-run (refusing to remove it automatically)"
            % (stage, kind, path)
        )


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------

def portable_path(p, home=None):
    """Rewrite a $HOME-relative absolute path to its ~/-prefixed form.

    Paths stored in project.json must survive an rsync to a machine with a
    different $HOME. Non-$HOME paths and non-strings pass through unchanged.
    """
    if not p or not isinstance(p, str):
        return p
    if home is None:
        home = os.path.realpath(os.path.expanduser("~"))
    try:
        absp = os.path.realpath(os.path.expanduser(p))
    except (TypeError, ValueError):
        return p
    if absp == home:
        return "~"
    if absp.startswith(home + os.sep):
        return "~/" + absp[len(home) + 1:].replace(os.sep, "/")
    return p


def portableize_project(project):
    """In-place: ~/-ify `root`, `workspace` and every stage code_source.path."""
    home = os.path.realpath(os.path.expanduser("~"))
    project["root"] = portable_path(project.get("root"), home)
    project["workspace"] = portable_path(project.get("workspace"), home)
    for _stage, cfg in (project.get("stages") or {}).items():
        cs = (cfg or {}).get("code_source") or {}
        if cs.get("path"):
            cs["path"] = portable_path(cs["path"], home)
    return project


def enabled_stages(project):
    """[(stage, cfg)] for enabled stages, in declaration order."""
    return [
        (stage, cfg or {})
        for stage, cfg in (project.get("stages") or {}).items()
        if (cfg or {}).get("enabled")
    ]


def local_source_links(project, root):
    """[(stage, link_path, target)] for every stage with source == 'local'."""
    out = []
    for stage, cfg in enabled_stages(project):
        cs = cfg.get("code_source") or {}
        if cs.get("source") == "local" and cs.get("path"):
            link = os.path.join(root, "stages", stage, "code", "_source")
            out.append((stage, link, os.path.expanduser(cs["path"])))
    return out


# ---------------------------------------------------------------------------
# _source symlinks
# ---------------------------------------------------------------------------

def source_link_conflict(link):
    """'directory' / 'file' if a non-symlink occupies `link`, else None.

    A symlink (valid or dangling) is not a conflict — replacing it is a
    legitimate re-init.
    """
    if os.path.islink(link):
        return None
    if os.path.isdir(link):
        return "directory"
    if os.path.exists(link):
        return "file"
    return None


def check_source_link_conflicts(project, root):
    """Pre-flight: [{stage, path, kind}] for every unusable _source location.

    Run before anything is created so a conflict aborts with zero side
    effects instead of leaving a half-built project behind.
    """
    conflicts = []
    for stage, link, _target in local_source_links(project, root):
        kind = source_link_conflict(link)
        if kind:
            conflicts.append({"stage": stage, "path": link, "kind": kind})
    return conflicts


def link_local_source(stage, link, target):
    """Point `link` at `target`, replacing an existing symlink.

    Raises SourceLinkConflict if a real file/directory sits there (a race
    against the pre-flight check), OSError for anything else.
    """
    kind = source_link_conflict(link)
    if kind:
        raise SourceLinkConflict(stage, link, kind)
    if os.path.islink(link):
        os.unlink(link)
    os.symlink(target, link)


# ---------------------------------------------------------------------------
# template copying
# ---------------------------------------------------------------------------

def copy_if_absent(src, dst):
    """Copy src -> dst unless dst exists. Returns 'copied'/'exists'/'missing'."""
    if os.path.isfile(dst):
        return "exists"
    if not os.path.isfile(src):
        return "missing"
    shutil.copy2(src, dst)
    return "copied"


def copy_project_templates(lifecycle, root, warnings):
    """Copy project-level templates. A missing template is a warning, not a crash."""
    report = {"copied": [], "exists": [], "missing": []}
    for fname in PROJECT_TEMPLATES:
        src = os.path.join(lifecycle, fname)
        dst = os.path.join(root, fname)
        try:
            result = copy_if_absent(src, dst)
        except OSError as e:
            warnings.append("could not copy template %s: %s" % (fname, e))
            continue
        report[result].append(fname)
        if result == "missing":
            warnings.append("project template not found, skipped: %s" % src)
    return report


def stage_template_dir(lifecycle, stage):
    """Stage-specific template dir, else inference's, else None."""
    specific = os.path.join(lifecycle, stage)
    if os.path.isdir(specific):
        return specific
    fallback = os.path.join(lifecycle, "inference")
    if os.path.isdir(fallback):
        return fallback
    return None


def copy_stage_templates(lifecycle, stage, stage_dir, warnings):
    """Copy a stage's JSON config templates. Returns the stage's json filenames.

    Globbed rather than hardcoded so a new template file lands in projects
    without also editing this script — provenance.json was added to training
    and silently never copied.
    """
    template_dir = stage_template_dir(lifecycle, stage)
    if template_dir is None:
        warnings.append("no templates found for stage '%s', skipped template copy" % stage)
        return []
    for jf in sorted(os.listdir(template_dir)):
        if not jf.endswith(".json") or jf in RUN_RECORD_TEMPLATES:
            continue
        try:
            copy_if_absent(os.path.join(template_dir, jf), os.path.join(stage_dir, jf))
        except OSError as e:
            warnings.append("could not copy %s template %s: %s" % (stage, jf, e))
    return sorted(
        jf for jf in os.listdir(stage_dir)
        if jf.endswith(".json") and jf not in RUN_RECORD_TEMPLATES
        and os.path.isfile(os.path.join(stage_dir, jf))
    )


def create_stage(lifecycle, root, stage, cfg, warnings):
    """Create one stage's directories, copy templates, link local code source.

    Returns the stage's config filenames (project-relative) for git add.
    """
    stage_dir = os.path.join(root, "stages", stage)
    for sub in ("code", "runs", "artifacts", "data"):
        os.makedirs(os.path.join(stage_dir, sub), exist_ok=True)

    files = [os.path.join("stages", stage, jf)
             for jf in copy_stage_templates(lifecycle, stage, stage_dir, warnings)]

    # source=local: stages/<stage>/code/_source -> expanded absolute path.
    # Filesystems don't expand ~ at read time, so the target is stored
    # expanded; after rsync to a new machine the symlink dangles and
    # relink_sources.py rebuilds it from the ~/-portable code_source.path.
    cs = cfg.get("code_source") or {}
    if cs.get("source") == "local" and cs.get("path"):
        link = os.path.join(stage_dir, "code", "_source")
        link_local_source(stage, link, os.path.expanduser(cs["path"]))
    return files


# ---------------------------------------------------------------------------
# workspace-level resources.json
# ---------------------------------------------------------------------------

def bootstrap_workspace_resources(project, lifecycle, warnings):
    """Seed {workspace}/resources.json from the template if it is missing.

    resources.json is workspace-level and shared by every project in it; it
    holds real credentials, so an existing file is NEVER overwritten.
    """
    workspace = project.get("workspace")
    if not workspace or not isinstance(workspace, str):
        warnings.append("project has no 'workspace' path; skipped resources.json bootstrap")
        return {"created": False, "reason": "no workspace path in project config", "path": None}

    workspace = os.path.abspath(os.path.expanduser(workspace))
    dst = os.path.join(workspace, WORKSPACE_RESOURCES)
    if os.path.exists(dst):
        return {"created": False, "reason": "already exists", "path": dst}

    src = os.path.join(lifecycle, WORKSPACE_RESOURCES)
    if not os.path.isfile(src):
        warnings.append("resources.json template not found at %s" % src)
        return {"created": False, "reason": "template missing", "path": dst}

    try:
        os.makedirs(workspace, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        warnings.append("could not create %s: %s" % (dst, e))
        return {"created": False, "reason": "error: %s" % e, "path": dst}
    return {"created": True, "reason": "copied from template", "path": dst}


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    """Run one git command. Returns (ok, returncode, last_output_line)."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    except OSError as e:
        return False, None, str(e)
    out = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    last = out.splitlines()[-1] if out else ""
    return proc.returncode == 0, proc.returncode, last


def git_init_and_commit(root, tracked_files):
    """git init + add + commit, reporting every step.

    Never fatal (per CLAUDE.md: scripts are optimizations) but never silent
    either — an untracked project must not report success. Returns a report
    dict destined for the emitted JSON.
    """
    report = {"ok": False, "available": True, "init": None,
              "added": [], "commit": None, "failures": []}

    if shutil.which("git") is None:
        report["available"] = False
        report["failures"].append("git executable not found; project is not version-controlled")
        return report

    ok, rc, msg = _git(root, "init")
    report["init"] = {"ok": ok, "returncode": rc, "message": msg}
    if not ok:
        report["failures"].append("git init failed (rc=%s): %s" % (rc, msg))
        return report

    # One `git add` for everything. The paths are pre-filtered by existence, so
    # the batch cannot hit the "pathspec did not match" abort that would justify
    # a spawn per file. Fall back to per-file only when the batch fails, so a
    # single bad path still reports which one.
    present = [r for r in tracked_files if os.path.exists(os.path.join(root, r))]
    if present:
        ok, rc, msg = _git(root, "add", "--", *present)
        if ok:
            report["added"].extend(present)
        else:
            for rel in present:
                ok, rc, msg = _git(root, "add", "--", rel)
                if ok:
                    report["added"].append(rel)
                else:
                    report["failures"].append("git add %s failed (rc=%s): %s" % (rel, rc, msg))

    ok, rc, msg = _git(root, "commit", "-m", "Initial project setup")
    report["commit"] = {"ok": ok, "returncode": rc, "message": msg}
    if not ok:
        report["failures"].append("git commit failed (rc=%s): %s" % (rc, msg))

    report["ok"] = not report["failures"]
    return report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def fail(message, **extra):
    """Emit a one-line error object on stderr and exit non-zero."""
    payload = {"status": "error", "error": message}
    payload.update(extra)
    print(json.dumps(payload), file=sys.stderr)
    sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("Usage: python init_project.py <project_json_str> <mlclaw_root>")
        print("  project_json_str: JSON string with project config (name, root, stages, etc.)")
        print("  mlclaw_root: path to MLClaw repo (for templates)")
        sys.exit(1)

    try:
        project = json.loads(sys.argv[1])
    except ValueError as e:
        fail("project_json_str is not valid JSON: %s" % e)
    mlclaw_root = os.path.abspath(os.path.expanduser(sys.argv[2]))
    lifecycle = os.path.join(mlclaw_root, "lifecycle")

    if not project.get("root"):
        fail("project config has no 'root' path")
    # Filesystem operations always use the expanded absolute path; project.json
    # stores the ~/-portable form (portableize_project below).
    root = os.path.abspath(os.path.expanduser(project["root"]))

    warnings = []

    # Pre-flight: refuse before creating anything if a stage's _source slot is
    # occupied by a real file/dir. Doing this first is what guarantees "no
    # half-created project" — there is nothing to roll back because nothing has
    # been written yet.
    conflicts = check_source_link_conflicts(project, root)
    if conflicts:
        c = conflicts[0]
        fail(
            "stages/%s/code/_source is an existing %s, not a symlink: %s "
            "— move or delete it, then re-run (nothing was created)"
            % (c["stage"], c["kind"], c["path"]),
            conflicts=conflicts, created=False, root=root,
        )

    os.makedirs(root, exist_ok=True)

    templates = copy_project_templates(lifecycle, root, warnings)
    resources = bootstrap_workspace_resources(project, lifecycle, warnings)

    portableize_project(project)
    project["created"] = datetime.now().isoformat()
    with open(os.path.join(root, "project.json"), "w", encoding="utf-8") as f:
        json.dump(project, f, indent=2)

    tracked = ["project.json", ".gitignore"] + list(PROJECT_TEMPLATES)
    created_stages = []
    for stage, cfg in enabled_stages(project):
        try:
            tracked += create_stage(lifecycle, root, stage, cfg, warnings)
        except SourceLinkConflict as e:
            # Lost a race with something that created a real _source after the
            # pre-flight check. Whatever exists on disk is left in place and
            # reported: this script must not delete a directory tree it did not
            # create, and the project root may well predate this invocation.
            fail(str(e) + " (partial project left at %s for inspection)" % root,
                 created=False, partial=True, root=root,
                 stages_created=created_stages, stage_failed=stage)
        except OSError as e:
            fail("could not create stage '%s': %s (partial project left at %s "
                 "for inspection)" % (stage, e, root),
                 created=False, partial=True, root=root,
                 stages_created=created_stages, stage_failed=stage)
        created_stages.append(stage)

    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(GITIGNORE)

    # Never over an existing one: a project root may predate this invocation and
    # carry a CLAUDE.md the user wrote. Warn instead -- the pointer is worth
    # having, but not at the price of silently replacing somebody's own file.
    claude_md = os.path.join(root, "CLAUDE.md")
    if os.path.exists(claude_md):
        warnings.append("CLAUDE.md already exists at %s — left untouched. It "
                        "should point at %s/CLAUDE.md, or a session standing "
                        "here loses the routing table and the delete rules "
                        "with nothing reporting it." % (root, mlclaw_root))
    else:
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(PROJECT_CLAUDE_MD.format(
                name=project.get("name") or os.path.basename(root),
                mlclaw_root=portable_path(mlclaw_root)))
        tracked.append("CLAUDE.md")

    git_report = git_init_and_commit(root, tracked)
    for failure in git_report["failures"]:
        warnings.append(failure)

    for w in warnings:
        print(json.dumps({"warning": w}), file=sys.stderr)

    print(json.dumps({
        "status": "ok",
        "root": root,
        "stages": created_stages,
        "templates": templates,
        "resources": resources,
        "git": git_report,
        "warnings": warnings,
    }))


if __name__ == "__main__":
    main()
