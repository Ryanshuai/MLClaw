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

PIPELINE_TAGS = {"production", "staging", "validated", "baseline", "deprecated"}


def load_all_runs(project_root):
    """Load all run.json files, keyed by stage/run_id."""
    runs = {}
    pattern = os.path.join(project_root, "stages", "*", "runs", "*", "run.json")
    for path in glob(pattern):
        with open(path) as f:
            run = json.load(f)
        stage = run.get("stage", "")
        run_id = run.get("run_id", "")
        full_id = f"{stage}/{run_id}"
        runs[full_id] = {"data": run, "path": path}
    return runs


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
            parents = runs[current]["data"].get("lineage", {}).get("parents", [])
            for p in parents:
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
    with open(path, "w") as f:
        json.dump(run_data, f, indent=2)


def main():
    if len(sys.argv) < 4:
        print("Usage: python tag_lineage.py <project_root> <stage/run_id> <tag>")
        print(f"Pipeline tags (auto-propagate): {', '.join(sorted(PIPELINE_TAGS))}")
        print("Any other tag is local (no propagation)")
        sys.exit(1)

    project_root = sys.argv[1]
    target_id = sys.argv[2]
    tag = sys.argv[3]

    runs = load_all_runs(project_root)

    if target_id not in runs:
        print(json.dumps({"error": f"Run not found: {target_id}"}))
        sys.exit(1)

    is_pipeline = tag in PIPELINE_TAGS
    tagged = []

    # Tag the target run
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
    }
    json.dump(result, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
