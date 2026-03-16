"""Finalize a completed run: calculate duration, update status in run.json."""
import json
import sys
from datetime import datetime


def main():
    if len(sys.argv) < 3:
        print("Usage: python finalize_run.py <run_json_path> <status>")
        print("  status: completed|failed|cancelled")
        sys.exit(1)

    run_json_path = sys.argv[1]
    status = sys.argv[2]

    with open(run_json_path) as f:
        run = json.load(f)

    now = datetime.now().isoformat()
    run["status"] = status
    run["finished_at"] = now

    # Calculate duration
    if run.get("started_at"):
        try:
            start = datetime.fromisoformat(run["started_at"])
            end = datetime.fromisoformat(now)
            run["duration_s"] = round((end - start).total_seconds(), 1)
        except Exception:
            pass

    # Read error from stderr if failed
    if status == "failed":
        import os
        log_dir = os.path.join(os.path.dirname(run_json_path), "logs")
        stderr_path = os.path.join(log_dir, "stderr.log")
        if os.path.isfile(stderr_path):
            with open(stderr_path) as f:
                lines = f.readlines()
                run["error"] = "".join(lines[-20:]).strip()  # last 20 lines

    with open(run_json_path, "w") as f:
        json.dump(run, f, indent=2)

    print(json.dumps({"status": status, "duration_s": run.get("duration_s")}))


if __name__ == "__main__":
    main()
