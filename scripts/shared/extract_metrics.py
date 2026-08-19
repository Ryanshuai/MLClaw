"""Extract metrics from stdout log and result files based on output.json definitions.

**A metric that could not be extracted is not the same as a metric the run never
produced.** Both used to come out as `null`, which then flowed into `run.json ->
metrics` and got silently skipped by every downstream comparison — a broken regex
and a genuinely absent number were indistinguishable, and nobody found out at the
time of writing.

So nothing here returns a bare `null`. Every watched metric lands in exactly one
of three buckets:

  metrics    name -> float     extracted, trustworthy
  errors     name -> {reason, source, detail}
                               the script looked and did not come back with a
                               number; `reason` says which of the ways it failed
  undefined  [name]            listed in `metrics.watch` with no entry in
                               `metrics.definitions` — a config bug, not a run bug

`metrics` contains only values you can compare. A caller that reads `.metrics`
and finds a name missing must consult `.errors` to learn why; it must not treat
the gap as zero, as absent-by-design, or as anything else.

The script does NOT editorialize about whether an absence was intended — it
cannot know. It reports what it observed ("result file not found", "key absent",
"pattern matched nothing") and leaves the judgment to the caller.

Exit code is 0 whenever the extraction ran, even with errors present: findings
are the output, not a failure. Non-zero means the script itself could not run
(bad arguments, unreadable output.json).

Usage:
    python extract_metrics.py <output_json> <run_dir>
"""
import json
import os
import re
import sys

# Number of capture groups a stdout pattern may have. 0 = the whole match is the
# number; 1 = the group is. More than that is ambiguous and reported, not guessed.
_MAX_GROUPS = 1


def _err(reason, source, detail=None):
    e = {"reason": reason, "source": source}
    if detail:
        e["detail"] = detail
    return e


def extract_from_stdout(log_path, pattern):
    """-> (value, error). Exactly one is None."""
    if not os.path.isfile(log_path):
        return None, _err("log_not_found", log_path)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return None, _err("log_unreadable", log_path, str(e))

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return None, _err("pattern_invalid", log_path, f"{pattern!r}: {e}")

    if rx.groups > _MAX_GROUPS:
        return None, _err(
            "pattern_ambiguous", log_path,
            f"{pattern!r} has {rx.groups} capture groups; expected 0 or 1 so the "
            f"number to take is unambiguous",
        )

    matches = rx.findall(content)
    if not matches:
        return None, _err(
            "pattern_no_match", log_path,
            f"{pattern!r} matched nothing in {os.path.getsize(log_path)} bytes",
        )

    # Last match wins: for a metric printed once per epoch, the final print is
    # the terminal value. Recorded here because it is a real assumption.
    raw = matches[-1]
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, _err(
            "match_not_numeric", log_path,
            f"{pattern!r} matched {raw!r} ({len(matches)} match(es)), which is not a number",
        )


def extract_from_file(file_path, key):
    """-> (value, error). Exactly one is None."""
    if not os.path.isfile(file_path):
        return None, _err("file_not_found", file_path)
    try:
        with open(file_path, encoding="utf-8") as f:
            parsed = json.load(f)
    except json.JSONDecodeError as e:
        return None, _err("file_not_json", file_path, str(e))
    except OSError as e:
        return None, _err("file_unreadable", file_path, str(e))

    node = parsed
    walked = []
    for k in key.split("."):
        walked.append(k)
        if not isinstance(node, dict) or k not in node:
            available = ", ".join(sorted(node.keys())[:10]) if isinstance(node, dict) else f"<{type(node).__name__}>"
            return None, _err(
                "key_absent", file_path,
                f"{'.'.join(walked)} not present (at that level: {available})",
            )
        node = node[k]

    try:
        return float(node), None
    except (TypeError, ValueError):
        return None, _err("value_not_numeric", file_path, f"{key} = {node!r}")


def extract(output_spec, run_dir):
    """Pure function over a parsed output.json. Returns the three-bucket dict."""
    definitions = output_spec.get("metrics", {}).get("definitions", {}) or {}
    watch_list = output_spec.get("metrics", {}).get("watch", []) or []

    metrics, errors, undefined = {}, {}, []

    for name in watch_list:
        defn = definitions.get(name)
        if not defn:
            # Previously `continue` — the metric vanished from the output with no
            # trace, so a typo in `watch` looked exactly like a clean extraction.
            undefined.append(name)
            continue

        source = defn.get("source")
        if source == "stdout":
            pattern = defn.get("pattern")
            if not pattern:
                errors[name] = _err("definition_incomplete", "output.json",
                                    f"source=stdout but no `pattern` for {name}")
                continue
            metric_value, err = extract_from_stdout(os.path.join(run_dir, "logs", "stdout.log"), pattern)
        elif source == "file":
            path, key = defn.get("path"), defn.get("key")
            if not path or not key:
                errors[name] = _err("definition_incomplete", "output.json",
                                    f"source=file but missing `path` or `key` for {name}")
                continue
            metric_value, err = extract_from_file(os.path.join(run_dir, path), key)
        else:
            errors[name] = _err("source_unknown", "output.json",
                                f"{name}.source = {source!r}; expected 'stdout' or 'file'")
            continue

        if err:
            errors[name] = err
        else:
            metrics[name] = metric_value

    return {"metrics": metrics, "errors": errors, "undefined": undefined}


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: python extract_metrics.py <output_json> <run_dir>\n")
        sys.exit(2)

    output_json, run_dir = sys.argv[1], sys.argv[2]
    try:
        with open(output_json, encoding="utf-8") as f:
            output_spec = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"extract_metrics: cannot read {output_json}: {e}\n")
        sys.exit(2)

    extracted = extract(output_spec, run_dir)
    json.dump(extracted, sys.stdout, indent=2)
    sys.stdout.write("\n")

    for name, err in extracted["errors"].items():
        sys.stderr.write(
            f"extract_metrics: {name}: {err['reason']} ({err['source']})"
            + (f" — {err['detail']}" if err.get("detail") else "") + "\n"
        )
    for name in extracted["undefined"]:
        sys.stderr.write(
            f"extract_metrics: {name}: in metrics.watch but has no metrics.definitions "
            f"entry — config bug, this metric was never going to be extracted\n"
        )


if __name__ == "__main__":
    main()
