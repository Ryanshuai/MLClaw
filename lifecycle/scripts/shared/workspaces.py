#!/usr/bin/env python3
"""MLClaw tool-repo locator.

Single job: tell any skill where the MLClaw tool repo lives on this machine,
without making the user type the path every time. Self-bootstraps from this
script's own __file__ on first run.

State file: ~/.mlclaw/state.json
Schema:     {"mlclaw_root": "~/code/MLClaw"}

CLI:
    workspaces.py tool                   # print expanded mlclaw_root
    workspaces.py register-tool [<path>] # pin mlclaw_root (default: auto-detect)

Workspace location lives in CLAUDE.md `workspace_root:` (line 1). One value,
one home, edited by /project-init when the user chooses a different path.
No registry, no list, no priority chain — when there's actually more than one
workspace, that's the time to add structure, not before.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

STATE = Path.home() / ".mlclaw" / "state.json"


def _self_mlclaw_root():
    # workspaces.py lives at <mlclaw_root>/lifecycle/scripts/shared/workspaces.py
    return Path(__file__).resolve().parents[3]


def _to_portable(path):
    p = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return p.as_posix()


def _expand(path):
    return Path(path).expanduser().resolve()


def _load():
    if not STATE.exists():
        return {"mlclaw_root": None}
    try:
        d = json.loads(STATE.read_text())
    except json.JSONDecodeError:
        STATE.rename(STATE.with_suffix(".json.bak"))
        return {"mlclaw_root": None}
    return {"mlclaw_root": d.get("mlclaw_root")}  # ignore any other legacy keys


def _save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE.parent, prefix=".state.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def get_tool():
    state = _load()
    if not state["mlclaw_root"]:
        # self-bootstrap from this script's location
        state["mlclaw_root"] = _to_portable(_self_mlclaw_root())
        _save(state)
    return state["mlclaw_root"]


def register_tool(path=None):
    target = _to_portable(path) if path else _to_portable(_self_mlclaw_root())
    _save({"mlclaw_root": target})
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tool", help="print expanded mlclaw_root")
    p_rt = sub.add_parser("register-tool", help="pin mlclaw_root (default: auto-detect)")
    p_rt.add_argument("path", nargs="?", default=None)
    args = ap.parse_args()

    if args.cmd == "tool":
        sys.stdout.write(str(_expand(get_tool())) + "\n")
    elif args.cmd == "register-tool":
        sys.stdout.write(str(_expand(register_tool(args.path))) + "\n")


if __name__ == "__main__":
    main()
