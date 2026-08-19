"""Compare required packages (from config.json) against installed (from env snapshot).

Usage:
    python check_deps.py <config.json> <run.json | env.json | RUN_DIR>

BOTH ARGUMENTS ARE FILES, and the first one is the STAGE's `config.json` -- not
the code directory. `/train-run` Step 2 documented this as
`check_deps.py <code_dir> <RUN_DIR>` beside `code_snapshot.py <code_dir>
<RUN_DIR>`, which does take exactly that pair. `open(<code_dir>)` then raises
`IsADirectoryError`, and the traceback exits 1 -- the code CLAUDE.md "Script
Integration" reserves for *the script worked and the answer is no*. A usage
error therefore read as "the dependency check ran and the dependencies are
bad", which is the one misreading this script must not produce. Usage is 2.

A run DIRECTORY is accepted for the second argument, because that is what the
caller plainly meant and `run.json` is unambiguous inside it.
"""
import json
import os
import re
import sys

from _records import broke  # same directory; noqa: E402


def parse_constraint(constraint):
    """Parse version constraint like '>=2.0', '==4.8.0', '~=1.0'."""
    match = re.match(r'([><=!~]+)\s*([\d.]+)', constraint)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def version_tuple(v):
    parts = []
    for x in v.split("."):
        try:
            parts.append(int(x))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def check_version(installed, constraint):
    """Check if installed version meets constraint. Returns True/False/None (can't parse)."""
    op, required = parse_constraint(constraint)
    if not op or not required:
        return None

    try:
        inst = version_tuple(installed)
        req = version_tuple(required)
    except (AttributeError, TypeError):
        # A recorded version that is not a string -- the constraint is
        # unanswerable, which the caller renders as "can't verify". `Exception`
        # here also swallowed genuine bugs in version_tuple, and `version_tuple`
        # catches its own ValueError, so nothing else was ever being caught.
        return None

    if op == ">=":
        return inst >= req
    elif op == "<=":
        return inst <= req
    elif op == "==":
        return inst == req
    elif op == "!=":
        return inst != req
    elif op == ">":
        return inst > req
    elif op == "<":
        return inst < req
    elif op == "~=":
        return inst >= req and inst[:len(req)-1] == req[:len(req)-1]
    return None


USAGE = ("python check_deps.py <config.json> <run.json | env.json | RUN_DIR>. "
         "The first argument is the stage's config.json, NOT the code directory.")


def load_json(path, what, dir_means=None):
    """Exit 2 on anything that is not a readable JSON file. Usage is not a refusal.

    `dir_means` names the file to read when `path` is a directory. Only the env
    record has one -- a directory in the config slot is the documented mistake
    and must say so rather than being resolved into a plausible-looking miss.
    """
    if os.path.isdir(path):
        if not dir_means:
            broke(f"{what} is a directory, not a JSON file: {path}", fix=USAGE)
        path = os.path.join(path, dir_means)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except IsADirectoryError:
        broke(f"{what} is a directory, not a JSON file: {path}", fix=USAGE)
    except FileNotFoundError:
        broke(f"{what} not found: {path}", fix=USAGE)
    except (OSError, ValueError) as exc:
        broke(f"{what} is not readable JSON: {path}: {exc}", fix=USAGE)


def main():
    if len(sys.argv) < 3:
        broke("two arguments are required", fix=USAGE)

    config = load_json(sys.argv[1], "the config")
    data = load_json(sys.argv[2], "the env record", dir_means="run.json")

    # env can be standalone env.json or nested in run.json
    env_packages = data.get("packages") or data.get("env", {}).get("packages", {})
    required = config.get("required_packages", {})

    errors = []
    warnings = []

    for pkg, constraint in required.items():
        installed = env_packages.get(pkg) or env_packages.get(pkg.lower())

        if installed is None:
            errors.append(f"{pkg}: required ({constraint}) but NOT installed")
            continue

        if constraint and constraint.strip():
            ok = check_version(installed, constraint)
            if ok is False:
                warnings.append(f"{pkg}: installed {installed}, required {constraint}")
            elif ok is None:
                warnings.append(f"{pkg}: installed {installed}, can't verify constraint {constraint}")

    result = {"errors": errors, "warnings": warnings, "ok": len(errors) == 0}
    json.dump(result, sys.stdout, indent=2)
    sys.exit(0 if len(errors) == 0 else 1)


if __name__ == "__main__":
    main()
