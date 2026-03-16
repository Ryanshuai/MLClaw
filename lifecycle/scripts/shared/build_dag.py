"""Build DAG from all runs' lineage. Outputs nodes, edges, roots, orphans."""
import json
import os
import sys
from glob import glob


def main():
    if len(sys.argv) < 2:
        print("Usage: python build_dag.py <project_root>")
        sys.exit(1)

    project_root = sys.argv[1]
    pattern = os.path.join(project_root, "stages", "*", "runs", "*", "run.json")
    run_files = glob(pattern)

    nodes = {}
    edges = []

    for path in run_files:
        with open(path) as f:
            run = json.load(f)

        run_id = run.get("run_id", "")
        stage = run.get("stage", "")
        full_id = f"{stage}/{run_id}"

        nodes[full_id] = {
            "run_id": run_id,
            "stage": stage,
            "alias": run.get("alias", ""),
            "status": run.get("status", ""),
            "local_tags": run.get("lineage", {}).get("local_tags", []),
            "pipeline_tags": run.get("lineage", {}).get("pipeline_tags", []),
            "metrics": run.get("metrics", {}),
            "created_at": run.get("created_at", ""),
        }

        parents = run.get("lineage", {}).get("parents", [])
        for p in parents:
            parent_id = f"{p['stage']}/{p['run_id']}"
            edges.append({"from": parent_id, "to": full_id, "artifact": p.get("artifact", "")})

    # Find roots (no incoming edges as child)
    children = {e["to"] for e in edges}
    parents_set = {e["from"] for e in edges}
    roots = [n for n in nodes if n not in children]

    # Find orphans (no parents AND no children)
    orphans = [n for n in nodes if n not in children and n not in parents_set]

    # Find leaves (no outgoing edges as parent)
    leaves = [n for n in nodes if n not in parents_set]

    # Group by pipeline tags
    tagged = {}
    for nid, node in nodes.items():
        for tag in node.get("pipeline_tags", []):
            tagged.setdefault(tag, []).append(nid)

    result = {
        "nodes": nodes,
        "edges": edges,
        "roots": roots,
        "leaves": leaves,
        "orphans": orphans,
        "tagged": tagged,
        "total_runs": len(nodes),
    }

    json.dump(result, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
