#!/usr/bin/env python3
"""Recreate the `stages/<stage>/code/_source` symlinks declared in project.json.

Why this exists
---------------
For a stage whose `code_source.source` is `local`, /project-init links
`stages/<stage>/code/_source` at the user's own repo instead of copying it, so
the IDE and the project see a single tree (CLAUDE.md, "Code Source
Resolution"). A symlink stores an *expanded* absolute path — filesystems do
not expand `~` at read time — so every one of those links dangles after the
project is rsynced to a machine with a different `$HOME`. project.json keeps
the same path in `~/`-portable form, so the links can be rebuilt from it on
the new host. That is the whole job.

Idempotent: a link that already points where project.json says is left alone.

What it refuses to do
---------------------
* **A real file or directory at `_source` is never touched.** Replacing it
  with a symlink would delete a code tree that may be the only copy, and
  nothing downstream could tell that it happened. Reported as `refused`.
* **A target that does not exist on this host is not linked.** A symlink to
  nothing is worse than no symlink: the `code_dir` rule
  (`code/_source if exists else code/`) silently falls back to `code/`, and
  the run then executes a different tree than the one recorded. Reported as
  `unresolved`.

Either way the remaining stages are still processed — one broken stage must
not leave the others dangling.

Output: JSON on stdout — one entry per local-source stage, plus a summary.
Exit:   0 = every local-source stage resolved
        1 = at least one stage unresolved or refused
        2 = could not run at all (no/unreadable project.json)

Usage:
    python relink_sources.py [<project_root>]      # default: cwd
    python relink_sources.py <project_root> --dry-run
"""
import argparse
import json
import os
import sys

# Per-stage outcomes. `unresolved` and `refused` are the two that make the
# process exit non-zero; everything else is a success state.
OK = "ok"                  # link already correct — no-op
CREATED = "created"        # no link existed, one was made
RELINKED = "relinked"      # link was dangling or pointed elsewhere, replaced
UNRESOLVED = "unresolved"  # cannot link honestly (no target / no path / no stage dir)
REFUSED = "refused"        # a real file or directory occupies _source
SKIPPED = "skipped"        # stage not present on this host and not enabled

FAILED = (UNRESOLVED, REFUSED)


class RelinkError(Exception):
    """The script cannot run at all (as opposed to a stage it cannot fix)."""


def load_project(project_root):
    """Return `(root, project_dict)`.

    Accepts either the project directory or the path to its project.json.
    """
    p = os.path.abspath(os.path.expanduser(project_root or "."))
    if os.path.isdir(p):
        root, pj = p, os.path.join(p, "project.json")
    else:
        pj, root = p, os.path.dirname(p) or "."
    if not os.path.isfile(pj):
        raise RelinkError(
            "no project.json at %s — pass the project root as the first argument" % pj
        )
    try:
        with open(pj, encoding="utf-8") as f:
            project = json.load(f)
    except (OSError, ValueError) as e:
        raise RelinkError("cannot read %s: %s" % (pj, e))
    if not isinstance(project, dict):
        raise RelinkError("%s is not a JSON object" % pj)
    return root, project


def local_source_stages(project):
    """Stage name -> stage config, for stages whose code_source is `local`.

    Every other source mode (`github`, `server`, null) puts real files under
    `stages/<stage>/code/` and has no `_source` link to repair.
    """
    out = {}
    for stage, cfg in sorted((project.get("stages") or {}).items()):
        if not isinstance(cfg, dict):
            continue
        cs = cfg.get("code_source") or {}
        if isinstance(cs, dict) and cs.get("source") == "local":
            out[stage] = cfg
    return out


def _entry(stage, link, declared, target, action, message):
    return {
        "stage": stage,
        "link": link,
        "declared_path": declared,
        "target": target,
        "action": action,
        "message": message,
    }


def _describe_occupant(path):
    if os.path.isdir(path):
        try:
            n = len(os.listdir(path))
        except OSError:
            n = "?"
        return "a real directory (%s entries)" % n
    return "a real file"


def relink_stage(root, stage, cfg, dry_run=False):
    """Repair one stage's `_source` link. Returns a report entry; never raises."""
    cs = cfg.get("code_source") or {}
    declared = cs.get("path")
    stage_dir = os.path.join(root, "stages", stage)
    code_dir = os.path.join(stage_dir, "code")
    link = os.path.join(code_dir, "_source")

    if not declared or not str(declared).strip():
        return _entry(
            stage, link, declared, None, UNRESOLVED,
            "code_source.source is 'local' but code_source.path is empty — "
            "there is nothing to link to. Fill it in project.json.",
        )

    # Store the target expanded but NOT realpath'd: init_project.py writes
    # expanduser(path), and matching that keeps repeat runs a no-op.
    target = os.path.normpath(os.path.expanduser(str(declared)))

    if not os.path.isdir(stage_dir):
        if cfg.get("enabled"):
            return _entry(
                stage, link, declared, target, UNRESOLVED,
                "stage is enabled but %s does not exist — the project tree looks "
                "incomplete; re-run /project-init or restore the directory." % stage_dir,
            )
        return _entry(
            stage, link, declared, target, SKIPPED,
            "stage directory %s does not exist and the stage is not enabled." % stage_dir,
        )

    target_exists = os.path.exists(target)

    if os.path.islink(link):
        current = os.readlink(link)
        same = current == target or os.path.normpath(current) == target
        if not same and target_exists and os.path.exists(current):
            # Different spelling of the same place (bind mount, /tmp -> /private/tmp).
            same = os.path.realpath(link) == os.path.realpath(target)
        if same:
            if not target_exists:
                return _entry(
                    stage, link, declared, target, UNRESOLVED,
                    "symlink already points at %s but that path does not exist on this "
                    "host. The link is dangling; leaving it in place rather than "
                    "deleting evidence. Restore the code there, or update "
                    "code_source.path in project.json." % target,
                )
            return _entry(
                stage, link, declared, target, OK,
                "already points at %s" % target,
            )
        if not target_exists:
            return _entry(
                stage, link, declared, target, UNRESOLVED,
                "target does not exist on this host: %s (existing symlink -> %s left "
                "in place). Not replacing a link with one that points at nothing." % (target, current),
            )
        if not dry_run:
            try:
                os.unlink(link)
                os.symlink(target, link)
            except OSError as e:
                return _entry(
                    stage, link, declared, target, UNRESOLVED,
                    "could not replace symlink (was -> %s): %s" % (current, e),
                )
        return _entry(
            stage, link, declared, target, RELINKED,
            "was -> %s, now -> %s" % (current, target),
        )

    if os.path.exists(link):
        return _entry(
            stage, link, declared, target, REFUSED,
            "REFUSED: %s is %s, not a symlink. Replacing it with a link to %s would "
            "destroy whatever is there. Move it aside first, or set "
            "code_source.source to null if the code genuinely lives in the project."
            % (link, _describe_occupant(link), target),
        )

    if not target_exists:
        return _entry(
            stage, link, declared, target, UNRESOLVED,
            "target does not exist on this host: %s — no link created. A symlink to "
            "nothing makes code_dir fall back to %s and the run would use the wrong "
            "tree without erroring." % (target, code_dir),
        )

    if not dry_run:
        try:
            os.makedirs(code_dir, exist_ok=True)
            os.symlink(target, link)
        except OSError as e:
            return _entry(
                stage, link, declared, target, UNRESOLVED,
                "could not create symlink: %s" % e,
            )
    return _entry(stage, link, declared, target, CREATED, "created -> %s" % target)


def relink_all(project_root, dry_run=False):
    """Repair every local-source stage. Returns the full report dict."""
    root, project = load_project(project_root)
    stages = [
        relink_stage(root, stage, cfg, dry_run=dry_run)
        for stage, cfg in local_source_stages(project).items()
    ]
    summary = {a: 0 for a in (OK, CREATED, RELINKED, UNRESOLVED, REFUSED, SKIPPED)}
    for e in stages:
        summary[e["action"]] = summary.get(e["action"], 0) + 1
    return {
        "project_root": root,
        "dry_run": dry_run,
        "local_source_stages": len(stages),
        "stages": stages,
        "summary": summary,
        "unresolved": [e["stage"] for e in stages if e["action"] in FAILED],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("project_root", nargs="?", default=".",
                    help="project directory (or its project.json). Default: cwd")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without touching the filesystem")
    args = ap.parse_args()

    try:
        report = relink_all(args.project_root, dry_run=args.dry_run)
    except RelinkError as e:
        json.dump({"error": str(e)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.stderr.write("relink_sources: %s\n" % e)
        sys.exit(2)

    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    for e in report["stages"]:
        if e["action"] in FAILED:
            sys.stderr.write("relink_sources: %s: %s: %s\n" % (e["action"], e["stage"], e["message"]))
    sys.exit(1 if report["unresolved"] else 0)


if __name__ == "__main__":
    main()
