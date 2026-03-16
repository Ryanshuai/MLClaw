"""Validate ${} references in config.json against artifacts/input/output JSON files."""
import json
import os
import re
import sys


def load_json(path):
    with open(path) as f:
        return json.load(f)


def find_refs(obj, prefix=""):
    """Recursively find all ${...} references in a JSON object."""
    refs = []
    if isinstance(obj, str):
        refs.extend(re.findall(r'\$\{(\w+\.\w+)\}', obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            refs.extend(find_refs(v))
    elif isinstance(obj, list):
        for v in obj:
            refs.extend(find_refs(v))
    return refs


def main():
    if len(sys.argv) < 2:
        print("Usage: python validate_refs.py <stage_dir>")
        print("  stage_dir: path to stages/{stage}/ containing config.json, artifacts.json, input.json, output.json")
        sys.exit(1)

    stage_dir = sys.argv[1]
    config = load_json(os.path.join(stage_dir, "config.json"))
    artifacts = load_json(os.path.join(stage_dir, "artifacts.json"))
    inputs = load_json(os.path.join(stage_dir, "input.json"))
    outputs = load_json(os.path.join(stage_dir, "output.json"))

    refs = find_refs(config.get("runtime_params", {}))
    refs += find_refs(config.get("entry_command", ""))

    errors = []
    warnings = []

    declared = {
        "artifact": set(artifacts.get("items", {}).keys()),
        "input": set(inputs.get("items", {}).keys()),
        "output": set(outputs.get("items", {}).keys()),
    }

    referenced = {"artifact": set(), "input": set(), "output": set()}

    for ref in refs:
        parts = ref.split(".", 1)
        if len(parts) != 2:
            continue
        prefix, name = parts
        if prefix in declared:
            referenced[prefix].add(name)
            if name not in declared[prefix]:
                errors.append(f"Broken reference: ${{{ref}}} — '{name}' not declared in {prefix}s.json → items")
        # project/secrets refs are valid but not checked here

    for prefix in ["artifact", "input"]:
        unused = declared[prefix] - referenced[prefix]
        for name in unused:
            warnings.append(f"Unused item: {prefix}.{name} declared but never referenced in runtime_params")

    result = {"errors": errors, "warnings": warnings}
    json.dump(result, sys.stdout, indent=2)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
