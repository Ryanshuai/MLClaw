"""Finalize a completed run: calculate duration, update status in run.json.

Timestamps are written as **UTC with an explicit offset** (`...+00:00`), not as
naive local time. Two reasons, both about records nobody reads until later:

  - CLAUDE.md's canonical run query sorts with `sort_by(.created_at)`. On naive
    local strings from two machines in different zones, that string sort is
    simply wrong, and it is wrong quietly — the list comes back ordered, just
    not chronologically.
  - `finished_at - started_at` across a zone change (run launched locally,
    finalized after travel; or launched local and finalized on a remote box)
    is off by the offset. A 4-hour training run recorded as 12 hours is not
    obviously absurd, so nobody catches it.

`run_id` keeps local-time formatting — it is a human-facing label, not a sort
key. Sort by `created_at`.

Usage:
    python finalize_run.py <run_json_path> <status>
    status: completed|failed|cancelled
"""
import json
import os
import sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _records import now_iso, parse_iso  # noqa: E402

VALID_STATUS = ("completed", "failed", "cancelled", "preempted")


# --- timestamp helpers (mirrored in create_run.py; keep the two in step) ---

def main():
    if len(sys.argv) < 3:
        sys.stderr.write(__doc__.rsplit("Usage:", 1)[-1].strip() + "\n")
        sys.exit(2)

    run_json_path, status = sys.argv[1], sys.argv[2]
    if status not in VALID_STATUS:
        sys.stderr.write(f"finalize_run: unknown status {status!r}; expected one of {', '.join(VALID_STATUS)}\n")
        sys.exit(2)

    try:
        with open(run_json_path) as f:
            run = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"finalize_run: cannot read {run_json_path}: {e}\n")
        sys.exit(2)

    warnings = []
    now = now_iso()
    run["status"] = status
    run["finished_at"] = now

    started = run.get("started_at")
    if not started:
        run["duration_s"] = None
        warnings.append("started_at is empty — duration_s left null (the run was never marked started)")
    else:
        try:
            start, start_naive = parse_iso(started)
            end, _ = parse_iso(now)
            run["duration_s"] = round((end - start).total_seconds(), 1)
            if start_naive:
                warnings.append(
                    f"started_at ({started}) carries no timezone; read as this host's local "
                    f"time. If the run was launched on a host in a different zone, duration_s "
                    f"is off by the offset."
                )
            if run["duration_s"] < 0:
                warnings.append(
                    f"duration_s is negative ({run['duration_s']}s) — finished_at precedes "
                    f"started_at. Clock skew between launch and finalize hosts, or a stale "
                    f"started_at. The value is recorded as-is rather than clamped."
                )
        except (ValueError, TypeError) as e:
            # Previously a bare `except: pass`, which left duration_s at null and
            # said nothing — indistinguishable from a run that was never started.
            run["duration_s"] = None
            warnings.append(f"could not parse started_at ({started!r}): {e} — duration_s left null")

    if status == "failed":
        stderr_path = os.path.join(os.path.dirname(run_json_path), "logs", "stderr.log")
        if os.path.isfile(stderr_path):
            try:
                with open(stderr_path, encoding="utf-8", errors="replace") as f:
                    run["error"] = "".join(f.readlines()[-20:]).strip()
            except OSError as e:
                warnings.append(f"status=failed but {stderr_path} unreadable: {e} — error field left as-is")
        else:
            warnings.append(f"status=failed but no {stderr_path} — error field left as-is")

    try:
        with open(run_json_path, "w") as f:
            json.dump(run, f, indent=2)
    except OSError as e:
        sys.stderr.write(f"finalize_run: cannot write {run_json_path}: {e}\n")
        sys.exit(2)

    json.dump({"status": status, "duration_s": run.get("duration_s"), "warnings": warnings},
              sys.stdout, indent=2)
    sys.stdout.write("\n")
    for w in warnings:
        sys.stderr.write(f"finalize_run: warning: {w}\n")


if __name__ == "__main__":
    main()
