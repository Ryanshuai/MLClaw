"""Create run directory structure and initialize run.json."""
import json
import os
import sys
from datetime import datetime


def main():
    if len(sys.argv) < 3:
        print("Usage: python create_run.py <stage_dir> <run_template>")
        print("  stage_dir: path to stages/{stage}/")
        print("  run_template: path to lifecycle/run.json template")
        sys.exit(1)

    stage_dir = os.path.abspath(sys.argv[1])
    template_path = os.path.abspath(sys.argv[2])

    now = datetime.now()
    run_id = f"run_{now.strftime('%Y%m%d_%H%M%S')}"
    run_dir = os.path.join(stage_dir, "runs", run_id)

    os.makedirs(os.path.join(run_dir, "outputs"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)

    with open(template_path) as f:
        run_json = json.load(f)

    stage_name = os.path.basename(stage_dir)
    run_json["run_id"] = run_id
    run_json["stage"] = stage_name
    run_json["created_at"] = now.isoformat()

    run_json_path = os.path.join(run_dir, "run.json")
    with open(run_json_path, "w") as f:
        json.dump(run_json, f, indent=2)

    print(json.dumps({"run_id": run_id, "run_dir": run_dir}))


if __name__ == "__main__":
    main()
