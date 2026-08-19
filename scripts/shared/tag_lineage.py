"""Tag a run. Pipeline tags propagate up the lineage chain automatically.

Two tag types:
  - local_tags: free-form, user-defined, no propagation (e.g., "debug", "batch_size_4")
  - pipeline_tags: fixed set, auto-propagate up the DAG to all ancestors

Fixed pipeline tags:
  - production: currently serving in production
  - staging: being tested for production
  - validated: passed full evaluation
  - baseline: reference baseline for comparison
  - deprecated: no longer in use, replaced by newer version

Usage:
    python tag_lineage.py <project_root> <stage/run_id> <tag>

    Pipeline tags auto-propagate. Local tags don't.

Examples:
    python tag_lineage.py /path/to/project inference/run_20260317_091200 production
    python tag_lineage.py /path/to/project inference/run_20260317_091200 my_custom_note
"""
import json
import os
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _records import atomic_write_json, broke, refuse  # noqa: E402

PIPELINE_TAGS = {"production", "staging", "validated", "baseline", "deprecated"}


def load_all_runs(project_root):
    """-> (runs keyed by stage/run_id, [paths that could not be read]).

    A run.json this cannot read is NOT the same as one that is not there, and
    the difference decides whether a propagation was complete. Dropping it
    silently would tag the reachable half of a lineage and report success;
    raising would abandon a tag that is correct for everything else. So it is
    carried out and the caller says so (CLAUDE.md: *never report data you could
    not look at*).
    """
    runs, unreadable = {}, []
    pattern = os.path.join(project_root, "stages", "*", "runs", "*", "run.json")
    for path in sorted(glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                run = json.load(f)
        except (OSError, ValueError):
            unreadable.append(path)
            continue
        if not isinstance(run, dict):
            unreadable.append(path)
            continue
        stage = run.get("stage", "")
        run_id = run.get("run_id", "")
        full_id = f"{stage}/{run_id}"
        runs[full_id] = {"data": run, "path": path}
    return runs, unreadable


def get_ancestors(runs, target_id):
    """Walk up the lineage DAG and collect all ancestor IDs."""
    ancestors = []
    visited = set()
    queue = [target_id]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        if current in runs:
            parents = (runs[current]["data"].get("lineage") or {}).get("parents") or []
            for p in parents:
                if not isinstance(p, dict) or not p.get("stage") or not p.get("run_id"):
                    # A parent entry that names no run cannot be walked. It is
                    # recorded lineage that points nowhere, and crashing on it
                    # would make one malformed edge un-taggable for the whole DAG.
                    continue
                parent_id = f"{p['stage']}/{p['run_id']}"
                ancestors.append(parent_id)
                queue.append(parent_id)

    return ancestors


def add_tag(run_data, tag, tag_type="local_tags"):
    """Add tag to run's lineage.local_tags or pipeline_tags."""
    lineage = run_data.setdefault("lineage", {"parents": [], "local_tags": [], "pipeline_tags": []})
    tags = lineage.setdefault(tag_type, [])
    if tag not in tags:
        tags.append(tag)
        return True
    return False


def save_run(path, run_data):
    """Atomic, and via the one writer.

    This rewrites `run.json` -- the record every conclusion, every baseline
    delta and every reproduction verdict is read back from. A direct `open(w)`
    truncates it before the new bytes land, so a crash here destroys a run
    record to add a string to a list. It also wrote `ensure_ascii=True`, which
    re-escaped every non-ASCII field in a record that was written literally.
    """
    atomic_write_json(path, run_data)


USAGE = ("python tag_lineage.py <project_root> <stage/run_id> <tag>. "
         "Pipeline tags (auto-propagate): %s. Any other tag is local."
         % ", ".join(sorted(PIPELINE_TAGS)))


def main():
    # CLAUDE.md "Script Integration": usage is 2 (the script broke, the caller
    # fixes the call), a run that is not there is 1 (it worked, the answer is
    # no). They shared exit 1, so "you typed it wrong" and "that run does not
    # exist" were the same answer.
    if len(sys.argv) < 4:
        broke("three arguments are required", fix=USAGE)

    project_root = sys.argv[1]
    target_id = sys.argv[2]
    tag = sys.argv[3]

    runs, unreadable = load_all_runs(project_root)

    if target_id not in runs:
        refuse(f"run not found: {target_id}",
               unreadable=unreadable,
               fix=("check `stage/run_id`. If run.json files are listed under "
                    "`unreadable`, the run may exist and simply not be readable — "
                    "which is a different fact." if unreadable else USAGE))

    is_pipeline = tag in PIPELINE_TAGS
    tagged = []

    tag_type = "pipeline_tags" if is_pipeline else "local_tags"
    if add_tag(runs[target_id]["data"], tag, tag_type):
        save_run(runs[target_id]["path"], runs[target_id]["data"])
        tagged.append(target_id)

    # Pipeline tags auto-propagate up the lineage
    if is_pipeline:
        ancestors = get_ancestors(runs, target_id)
        for ancestor_id in ancestors:
            if ancestor_id in runs:
                if add_tag(runs[ancestor_id]["data"], tag, "pipeline_tags"):
                    save_run(runs[ancestor_id]["path"], runs[ancestor_id]["data"])
                    tagged.append(ancestor_id)

    result = {
        "target": target_id,
        "tag": tag,
        "type": "pipeline" if is_pipeline else "local",
        "propagated": is_pipeline,
        "tagged_runs": tagged,
        # A propagation that could not read part of the DAG reached an unknown
        # subset of it. Saying so is the difference between a tag and a claim.
        "unreadable": unreadable,
        "complete": not unreadable,
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 1 if unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
