#!/usr/bin/env python3
"""Validate every `${}` reference in a stage's four JSON configs.

Called from /infer-init Step 5 and /eval-init Step 5. A `${}` that resolves to
nothing is not caught at run time by anything else: `/{stage}-run` substitutes
what it can and hands the rest to the code as a literal `${input.foo}`, which
becomes a missing-file error hundreds of lines into someone else's stack trace,
or — worse — a path the code silently treats as absent.

Namespaces, per lifecycle/references/layout.md "Variable Reference Syntax `${}`":

    ${artifact.x}   artifacts.json -> items
    ${input.x}      input.json -> items  UNION  input.json -> ground_truth.items
    ${output.x}     output.json -> items
    ${project.x}    project.json      — resolved at run time, not checked here
    ${resources.x}  resources.json    — resolved at run time, not checked here

**The `input` namespace is the union of two blocks.** An evaluation stage
declares its annotations under `ground_truth.items` and references them as
`${input.gt_ann}` like any other input — there is no `${ground_truth.x}` prefix.
A validator that reads only `items` reports every correct eval config as broken,
and /eval-init's "don't save if there are broken references" then blocks a save
that should have gone through. Inference stages have no `ground_truth` block at
all, so the union is a no-op there.

Reference syntax accepted: `${prefix.name}` plus any number of further dotted
segments (`${resources.servers.gpu.host}`), with hyphens allowed in the segments
(`${artifact.my-model}`). Only the first segment after the prefix names an item;
the rest address into it and are not checked.

Output: JSON on stdout — `errors` / `warnings` / `info`.
Exit:   0 = no broken references (warnings may exist)
        1 = at least one broken reference — do not save until it is fixed
        2 = could not run (missing stage_dir, unreadable/invalid JSON) — fall
            back per CLAUDE.md "Script Integration"

Usage:
    python validate_refs.py <stage_dir>
"""
import json
import os
import re
import sys

# `${prefix.name}` with optional further dotted segments; hyphens allowed after
# the prefix. The narrower `\$\{(\w+\.\w+)\}` this replaced matched neither
# `${resources.servers.gpu.host}` nor `${artifact.my-model}` — it did not flag
# them, it could not see them, so they were also counted as never referenced.
REF_RE = re.compile(r"\$\{([A-Za-z_]\w*(?:\.[\w\-]+)+)\}")

# Which file declares each prefix's items, and how to say so in a message.
DECLARING_FILE = {
    "artifact": "artifacts.json",
    "input": "input.json",
    "output": "output.json",
}
DECLARED_IN = {
    "artifact": "artifacts.json -> items",
    "input": "input.json -> items (or input.json -> ground_truth.items)",
    "output": "output.json -> items",
}

# Resolved from outside the stage at run time; nothing here can check them.
EXTERNAL_PREFIXES = ("project", "resources")

FILES = ("config.json", "artifacts.json", "input.json", "output.json")


class ValidationError(Exception):
    """The script cannot run at all."""


def load_json(path, required=False):
    """Parsed JSON, or None when absent. Unreadable/invalid is always fatal —
    an unparseable config is not "no references found"."""
    if not os.path.isfile(path):
        if required:
            raise ValidationError("missing %s" % path)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ValidationError("cannot read %s: %s" % (path, e))


def find_refs(obj):
    """Every `${a.b}` reachable in a JSON value. `_comment*` keys are prose and
    are skipped — a reference named in documentation is not a reference used."""
    refs = []
    if isinstance(obj, str):
        refs.extend(REF_RE.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("_comment"):
                continue
            refs.extend(find_refs(v))
    elif isinstance(obj, list):
        for v in obj:
            refs.extend(find_refs(v))
    return refs


def declared_items(artifacts, inputs, outputs):
    """The three item namespaces. Returns (declared, plain_inputs, gt_inputs)."""
    plain = set(((inputs or {}).get("items") or {}).keys())
    gt = set((((inputs or {}).get("ground_truth") or {}).get("items") or {}).keys())
    declared = {
        "artifact": set(((artifacts or {}).get("items") or {}).keys()),
        "input": plain | gt,
        "output": set(((outputs or {}).get("items") or {}).keys()),
    }
    return declared, plain, gt


def validate(stage_dir):
    stage_dir = os.path.abspath(os.path.expanduser(stage_dir))
    if not os.path.isdir(stage_dir):
        raise ValidationError("stage_dir does not exist: %s" % stage_dir)

    loaded = {name: load_json(os.path.join(stage_dir, name)) for name in FILES}
    if all(v is None for v in loaded.values()):
        raise ValidationError("no config.json / artifacts.json / input.json / "
                              "output.json found in %s" % stage_dir)

    config = loaded["config.json"] or {}
    artifacts = loaded["artifacts.json"] or {}
    inputs = loaded["input.json"] or {}
    outputs = loaded["output.json"] or {}

    errors, warnings, info = [], [], []
    for name in FILES:
        if loaded[name] is None:
            warnings.append("%s not found in %s — references it would have "
                            "declared or used were not checked." % (name, stage_dir))

    declared, plain_inputs, gt_inputs = declared_items(artifacts, inputs, outputs)

    for dup in sorted(plain_inputs & gt_inputs):
        warnings.append("Ambiguous item: %r is declared in both input.json -> items "
                        "and input.json -> ground_truth.items — ${input.%s} could mean "
                        "either. Rename one." % (dup, dup))

    # Where references are read from. runtime_params and entry_command are what
    # actually reaches the code; the other files are scanned because a source
    # `path` may itself be written as a reference.
    scanned = [
        ("config.json -> runtime_params", config.get("runtime_params") or {}),
        ("config.json -> entry_command", config.get("entry_command") or ""),
        ("artifacts.json", artifacts),
        ("input.json", inputs),
        ("output.json", outputs),
    ]

    referenced = {prefix: set() for prefix in declared}
    seen_external = set()
    for where, blob in scanned:
        for ref in find_refs(blob):
            prefix, _, tail = ref.partition(".")
            name = tail.split(".")[0]
            if prefix in declared:
                referenced[prefix].add(name)
                if name in declared[prefix]:
                    pass
                elif loaded[DECLARING_FILE[prefix]] is None:
                    # The declaring file isn't written yet. "Not declared" here
                    # would be an artifact of a half-finished init, not a finding.
                    warnings.append("Unchecked reference in %s: ${%s} — %s does not "
                                    "exist yet, so nothing could confirm %r."
                                    % (where, ref, DECLARING_FILE[prefix], name))
                else:
                    errors.append("Broken reference in %s: ${%s} — %r is not declared "
                                  "in %s" % (where, ref, name, DECLARED_IN[prefix]))
            elif prefix in EXTERNAL_PREFIXES:
                if ref not in seen_external:
                    seen_external.add(ref)
                    info.append("${%s} resolves from project.json / resources.json at "
                                "run time — not checked here." % ref)
            else:
                warnings.append("Unknown prefix in %s: ${%s} uses %r — see CLAUDE.md "
                                "'Variable Reference Syntax'." % (where, ref, prefix))

    for prefix in ("artifact", "input"):
        for name in sorted(declared[prefix] - referenced[prefix]):
            gt = prefix == "input" and name in gt_inputs
            warnings.append(
                "Unused item: %s.%s is declared but never referenced from "
                "runtime_params or entry_command — /{stage}-run has no way to pass it "
                "to the code.%s" % (prefix, name,
                                    " Fine only if the code finds annotations by "
                                    "convention (parallel dirs, fixed filename)." if gt else ""))

    return {
        "stage_dir": stage_dir,
        "checked": [name for name in FILES if loaded[name] is not None],
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "summary": {"errors": len(errors), "warnings": len(warnings), "info": len(info)},
    }


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        sys.stderr.write("Usage: python validate_refs.py <stage_dir>\n"
                         "  stage_dir: path to stages/{stage}/ containing config.json, "
                         "artifacts.json, input.json, output.json\n")
        sys.exit(2)

    try:
        report = validate(sys.argv[1])
    except ValidationError as e:
        json.dump({"error": str(e)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.stderr.write("validate_refs: %s\n" % e)
        sys.exit(2)

    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    for e in report["errors"]:
        sys.stderr.write("validate_refs: error: %s\n" % e)
    sys.exit(1 if report["errors"] else 0)


if __name__ == "__main__":
    main()
