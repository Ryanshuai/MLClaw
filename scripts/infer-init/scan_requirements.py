"""Scan code directory for dependency files and extract required packages.

WHAT THE RESULT CLAIMS. `source` says whether this was READ or GUESSED --
`file` means a declared dependency file was parsed, `imports` means nothing
declared anything and the packages were inferred from import statements. The
second is exactly the `guessed` status `provenance.json` exists to carry, and
a caller that treats the two alike records an inference as a fact.

`files` names which ones were read, because "from a file" with no file named
is not provenance, and `unreadable` names the ones that could not be parsed --
a file nothing could read is not a file with no dependencies.
"""
import ast
import json
import os
import re
import sys

# 3.10+ knows its own stdlib; below that a short list covers what a training
# script actually reaches for. Getting this wrong in the safe direction (a name
# missing from the set) only restores the old behaviour for that one name.
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "abc", "argparse", "ast", "base64", "collections", "contextlib", "copy",
    "csv", "dataclasses", "datetime", "enum", "functools", "glob", "hashlib",
    "html", "io", "itertools", "json", "logging", "math", "os", "pathlib",
    "pickle", "platform", "random", "re", "shutil", "signal", "socket",
    "string", "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
    "traceback", "typing", "unittest", "urllib", "uuid", "warnings", "zipfile",
}


def parse_requirements_txt(path):
    pkgs = {}
    with open(path, encoding="utf-8") as f:
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
    with open(path, encoding="utf-8") as f:
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
    with open(path, encoding="utf-8") as f:
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
    with open(path, encoding="utf-8") as f:
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


def _imports_of(text):
    """Top-level module names imported by one source file.

    Parsed, not pattern-matched. The regex this replaced was
    `^(?:import|from)\\s+(\\w+)`, which fires on ordinary prose inside a
    docstring -- "from the caller" contributed a package called `the` -- and on
    a line inside a `try: import optional_thing`. `ast` sees imports and only
    imports; the regex stays as the fallback for a file that will not parse,
    since a partial answer beats none for a best-effort scan that says so.
    """
    names = set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for line in text.splitlines():
            m = re.match(r"^(?:import|from)\s+([a-zA-Z0-9_]+)", line)
            if m:
                names.add(m.group(1))
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has no module name and is local by construction.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def local_module_names(code_dir):
    """Every name that resolves inside the code itself, anywhere in the tree.

    `from _common import x` is not a PyPI package, and `import model` beside
    `model.py` is not either. Walked rather than listed at the top level,
    because a repo's own modules sit in subpackages and a top-level listing
    reported every one of them as a missing dependency.
    """
    names = set()
    for root, dirs, files in os.walk(code_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for d in dirs:
            names.add(d)
        for f in files:
            if f.endswith(".py"):
                names.add(f[:-3])
    return names


def scan_imports(code_dir):
    """Fallback: scan .py files for import statements.

    A GUESS, and the caller is told so through `source: imports`. It cannot see
    an optional import, a dynamic one, or a version -- which is why a declared
    dependency file always wins.
    """
    local_modules = local_module_names(code_dir)
    imports = set()
    for root, dirs, files in os.walk(code_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                text = open(os.path.join(root, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                # A file this cannot read contributes no imports, and that is the
                # honest outcome -- but `Exception` also swallowed a bug in the
                # loop below, which would have left the whole scan silently short.
                continue
            imports |= _imports_of(text)
    # Map common import names to pip package names
    import_to_pip = {
        "cv2": "opencv-python", "sklearn": "scikit-learn", "PIL": "pillow",
        "yaml": "pyyaml", "torch": "torch", "tf": "tensorflow",
        "np": "numpy", "pd": "pandas",
    }
    pkgs = {}
    for imp in imports:
        # The stdlib is not a dependency, and neither is the code's own module
        # next door. Left in, they became `required_packages` entries that
        # `check_deps.py` then reported as "required but NOT installed" -- one
        # per stdlib module, on every project with no requirements.txt, which is
        # exactly the project this fallback exists for.
        if imp in _STDLIB or imp in local_modules:
            continue
        pip_name = import_to_pip.get(imp, imp)
        pkgs[pip_name] = ""
    return pkgs


def main():
    # CLAUDE.md "Script Integration": usage and a missing directory are both
    # "the script could not run" -- exit 2, so the skill falls back and reads
    # requirements.txt by hand, which is what its SKILL.md already says to do.
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: scan_requirements.py <code_dir>"}))
        sys.exit(2)

    code_dir = sys.argv[1]
    if not os.path.isdir(code_dir):
        print(json.dumps({"error": f"Code directory not found: {code_dir}"}), file=sys.stderr)
        sys.exit(2)
    pkgs = {}
    unreadable = []

    # Priority order: requirements.txt > pyproject.toml > setup.py > conda yaml > imports
    candidates = [
        ("requirements.txt", parse_requirements_txt),
        ("pyproject.toml", parse_pyproject_toml),
        ("setup.py", parse_setup_py),
        ("environment.yaml", parse_conda_yaml),
        ("environment.yml", parse_conda_yaml),
        ("conda.yaml", parse_conda_yaml),
    ]

    # `candidates` is in priority order and the merge has to honour it. It used
    # `pkgs.update(parser(path))`, which lets the LAST file parsed win -- so a
    # stale `conda.yaml` silently overrode the `requirements.txt` beside it, the
    # exact inversion of what the comment above claims. `setdefault` keeps the
    # first, highest-priority constraint for each package.
    read_files = []
    for fname, parser in candidates:
        path = os.path.join(code_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            found = parser(path)
        except (OSError, UnicodeDecodeError, ValueError):
            # A file nothing could parse is not a file with no dependencies.
            unreadable.append(fname)
            continue
        read_files.append(fname)
        for name, constraint in found.items():
            pkgs.setdefault(name, constraint)

    if not read_files:
        pkgs = scan_imports(code_dir)

    result = {
        "source": "file" if read_files else "imports",
        "files": read_files,
        "unreadable": unreadable,
        "packages": pkgs,
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
