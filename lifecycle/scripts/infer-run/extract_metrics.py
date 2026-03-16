"""Extract metrics from stdout log and result files based on output.json definitions."""
import json
import os
import re
import sys


def extract_from_stdout(log_path, pattern):
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        matches = re.findall(pattern, content)
        if matches:
            return float(matches[-1])
    except Exception:
        pass
    return None


def extract_from_file(file_path, key):
    try:
        with open(file_path) as f:
            data = json.load(f)
        keys = key.split(".")
        val = data
        for k in keys:
            val = val[k]
        return float(val)
    except Exception:
        return None


def main():
    if len(sys.argv) < 3:
        print("Usage: python extract_metrics.py <output_json> <run_dir>")
        sys.exit(1)

    output_json = sys.argv[1]
    run_dir = sys.argv[2]

    with open(output_json) as f:
        output = json.load(f)

    definitions = output.get("metrics", {}).get("definitions", {})
    watch_list = output.get("metrics", {}).get("watch", [])

    results = {}
    for name in watch_list:
        defn = definitions.get(name)
        if not defn:
            continue

        source = defn.get("source")
        if source == "stdout":
            log_path = os.path.join(run_dir, "logs", "stdout.log")
            val = extract_from_stdout(log_path, defn["pattern"])
        elif source == "file":
            file_path = os.path.join(run_dir, defn["path"])
            val = extract_from_file(file_path, defn["key"])
        else:
            val = None

        results[name] = val

    json.dump(results, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
