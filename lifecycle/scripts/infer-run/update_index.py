"""Append a completed run's summary to runs_index.json."""
import json
import os
import sys


def main():
    if len(sys.argv) < 3:
        print("Usage: python update_index.py <runs_index_path> <run_json_path>")
        sys.exit(1)

    index_path = sys.argv[1]
    run_json_path = sys.argv[2]

    with open(index_path) as f:
        index = json.load(f)

    with open(run_json_path) as f:
        run = json.load(f)

    entry = {
        "run_id": run.get("run_id"),
        "stage": run.get("stage"),
        "alias": run.get("alias", ""),
        "description": run.get("description", ""),
        "status": run.get("status"),
        "local_tags": run.get("lineage", {}).get("local_tags", []),
        "pipeline_tags": run.get("lineage", {}).get("pipeline_tags", []),
        "created_at": run.get("created_at"),
        "duration_s": run.get("duration_s"),
        "execution": run.get("execution"),
        "server": run.get("server"),
        "metrics": run.get("metrics", {}),
        "path": os.path.join("stages", run.get("stage", ""), "runs", run.get("run_id", "")),
    }

    # Replace existing entry if same run_id, otherwise append
    runs = index.get("runs", [])
    updated = False
    for i, r in enumerate(runs):
        if r.get("run_id") == entry["run_id"]:
            runs[i] = entry
            updated = True
            break
    if not updated:
        runs.append(entry)

    index["runs"] = runs
    with open(index_path, "w") as f:
        json.dump(index, f, indent=2)

    print(json.dumps(entry, indent=2))


if __name__ == "__main__":
    main()
