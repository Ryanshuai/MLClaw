"""Create run directory structure and initialize run.json.

`created_at` is written as **UTC with an explicit offset** (`...+00:00`).
CLAUDE.md's canonical run query sorts with `sort_by(.created_at)`; on naive local
strings from two machines in different zones that sort is quietly wrong — the
list comes back ordered, just not chronologically. See finalize_run.py for the
matching rationale on `finished_at` / `duration_s`.

`run_id` keeps local-time formatting. It is a human-facing label ("that
run_20260427_180000 job"), not a sort key — sort by `created_at`.

Because `run_id` has one-second resolution, two runs launched in the same second
(a `/train-tune` session with `max_concurrent > 1` is the realistic case) would
land on the same directory, and the second `run.json` write would overwrite the
first run's record with nothing raised. Colliding ids get a `_2`, `_3`, … suffix
instead; `run_*` globs still match.

Usage:
    python create_run.py <stage_dir> <run_template>
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _records import now_iso  # noqa: E402

MAX_COLLISION_SUFFIX = 100


# --- timestamp helper (mirrored in finalize_run.py; keep the two in step) ---

def allocate_run_dir(stage_dir, base_id):
    """-> (run_id, run_dir). Never returns a directory that already holds a run.json."""
    for n in range(1, MAX_COLLISION_SUFFIX + 1):
        run_id = base_id if n == 1 else f"{base_id}_{n}"
        run_dir = os.path.join(stage_dir, "runs", run_id)
        if not os.path.exists(os.path.join(run_dir, "run.json")):
            return run_id, run_dir
    raise RuntimeError(
        f"{MAX_COLLISION_SUFFIX} runs already exist for {base_id} — refusing to guess further. "
        f"Something is launching runs in a loop."
    )


def main():
    if len(sys.argv) < 3:
        sys.stderr.write(
            "Usage: python create_run.py <stage_dir> <run_template>\n"
            "  stage_dir:    path to stages/{stage}/\n"
            "  run_template: path to lifecycle/run.json template\n"
        )
        sys.exit(2)

    stage_dir = os.path.abspath(os.path.expanduser(sys.argv[1]))
    template_path = os.path.abspath(os.path.expanduser(sys.argv[2]))

    try:
        with open(template_path) as f:
            run_json = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"create_run: cannot read run template {template_path}: {e}\n")
        sys.exit(2)

    base_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        run_id, run_dir = allocate_run_dir(stage_dir, base_id)
    except RuntimeError as e:
        sys.stderr.write(f"create_run: {e}\n")
        sys.exit(3)

    try:
        # `output`, singular — the launch contract overrides the code's
        # `output_dir` to point here, and `${output.xxx}` resolves against it.
        # This line said `outputs` while both of those said `output`.
        os.makedirs(os.path.join(run_dir, "output"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"create_run: cannot create {run_dir}: {e}\n")
        sys.exit(2)

    run_json["run_id"] = run_id
    run_json["stage"] = os.path.basename(stage_dir)
    run_json["created_at"] = now_iso()

    run_json_path = os.path.join(run_dir, "run.json")
    try:
        with open(run_json_path, "w") as f:
            json.dump(run_json, f, indent=2)
    except OSError as e:
        sys.stderr.write(f"create_run: cannot write {run_json_path}: {e}\n")
        sys.exit(2)

    payload = {"run_id": run_id, "run_dir": run_dir, "created_at": run_json["created_at"]}
    if run_id != base_id:
        payload["id_collision"] = (
            f"{base_id} was already taken; allocated {run_id} instead. Another run "
            f"started within the same second."
        )
        sys.stderr.write(f"create_run: warning: {payload['id_collision']}\n")

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
