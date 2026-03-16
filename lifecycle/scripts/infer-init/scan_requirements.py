"""Scan code directory for dependency files and extract required packages."""
import json
import os
import re
import sys


def parse_requirements_txt(path):
    """Parse requirements.txt format."""
    pkgs = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Handle: package==1.0, package>=1.0, package~=1.0, package
            match = re.match(r'^([a-zA-Z0-9_.-]+)\s*([><=!~]+.+)?', line)
            if match:
                name = match.group(1).strip()
                constraint = (match.group(2) or "").strip()
                pkgs[name] = constraint
    return pkgs


def parse_pyproject_toml(path):
    """Extract dependencies from pyproject.toml (basic parsing, no toml lib needed)."""
    pkgs = {}
    in_deps = False
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped in ("[project.dependencies]", "dependencies = ["):
                in_deps = True
                continue
            if in_deps:
                if stripped.startswith("[") or stripped == "]":
                    in_deps = False
                    continue
                # Parse: "package>=1.0",
                match = re.match(r'["\']([a-zA-Z0-9_.-]+)\s*([><=!~]+[^"\']*)?["\']', stripped)
                if match:
                    pkgs[match.group(1)] = (match.group(2) or "").strip()
    return pkgs


def parse_setup_py(path):
    """Extract install_requires from setup.py (regex-based, best effort)."""
    pkgs = {}
    with open(path) as f:
        content = f.read()
    match = re.search(r'install_requires\s*=\s*\[(.*?)\]', content, re.DOTALL)
    if match:
        for item in re.findall(r'["\']([^"\']+)["\']', match.group(1)):
            m = re.match(r'([a-zA-Z0-9_.-]+)\s*(.*)', item)
            if m:
                pkgs[m.group(1)] = m.group(2).strip()
    return pkgs


def parse_conda_yaml(path):
    """Extract pip dependencies from conda environment.yaml."""
    pkgs = {}
    in_pip = False
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped == "- pip:":
                in_pip = True
                continue
            if in_pip:
                if stripped.startswith("- "):
                    dep = stripped[2:].strip()
                    match = re.match(r'([a-zA-Z0-9_.-]+)\s*([><=!~]+.+)?', dep)
                    if match:
                        pkgs[match.group(1)] = (match.group(2) or "").strip()
                else:
                    in_pip = False
    return pkgs


def scan_imports(code_dir):
    """Fallback: scan .py files for import statements."""
    imports = set()
    for root, _, files in os.walk(code_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, f)) as fh:
                    for line in fh:
                        match = re.match(r'^(?:import|from)\s+([a-zA-Z0-9_]+)', line)
                        if match:
                            imports.add(match.group(1))
            except Exception:
                continue
    # Map common import names to pip package names
    import_to_pip = {
        "cv2": "opencv-python", "sklearn": "scikit-learn", "PIL": "pillow",
        "yaml": "pyyaml", "torch": "torch", "tf": "tensorflow",
        "np": "numpy", "pd": "pandas",
    }
    pkgs = {}
    for imp in imports:
        pip_name = import_to_pip.get(imp, imp)
        pkgs[pip_name] = ""
    return pkgs


def main():
    if len(sys.argv) < 2:
        print("Usage: python scan_requirements.py <code_dir>")
        sys.exit(1)

    code_dir = sys.argv[1]
    if not os.path.isdir(code_dir):
        print(json.dumps({"error": f"Code directory not found: {code_dir}"}), file=sys.stderr)
        sys.exit(1)
    pkgs = {}

    # Priority order: requirements.txt > pyproject.toml > setup.py > conda yaml > imports
    candidates = [
        ("requirements.txt", parse_requirements_txt),
        ("pyproject.toml", parse_pyproject_toml),
        ("setup.py", parse_setup_py),
        ("environment.yaml", parse_conda_yaml),
        ("environment.yml", parse_conda_yaml),
        ("conda.yaml", parse_conda_yaml),
    ]

    found_file = False
    for fname, parser in candidates:
        path = os.path.join(code_dir, fname)
        if os.path.isfile(path):
            pkgs.update(parser(path))
            found_file = True

    if not found_file:
        pkgs = scan_imports(code_dir)

    result = {"source": "file" if found_file else "imports", "packages": pkgs}
    json.dump(result, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
