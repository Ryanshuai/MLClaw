"""Compare required packages (from config.json) against installed (from env snapshot)."""
import json
import re
import sys


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
    except Exception:
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


def main():
    if len(sys.argv) < 3:
        print("Usage: python check_deps.py <config_json_path> <env_json_or_run_json_path>")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        config = json.load(f)

    with open(sys.argv[2]) as f:
        data = json.load(f)

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
