"""Shared by lease.py and every provider_<name>.py.

Exists because the skill promises a new provider is one new file: without this,
each adapter re-copies the JSON/error conventions that `lease.py` parses, and the
copies drift — the error-class shape in particular is what lease.py depends on to
tell `no_capacity` from `quota`.

Policy constants live here, one copy: TTL and the tag namespace are layer-2
decisions, so adapters must not carry their own defaults for them.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

DEFAULT_TTL_S = 14400  # 4h. L2 policy — adapters require --ttl-s, they don't default it.
TAG_PREFIX = "mlclaw-"
SSH_UNREACHABLE = 255  # ssh's own "could not connect", distinct from a remote non-zero exit

ERROR_CLASSES = ("no_capacity", "quota", "permission", "credential_expired", "transient")


def die(cls, detail, **extra):
    """Exit with a normalized error class. See the contract's "Error classes" table —
    the caller's next action differs per class, so "it failed" is not a usable result."""
    assert cls in ERROR_CLASSES, f"unknown error class {cls}"
    print(json.dumps({"error": cls, "detail": detail, **extra}))
    sys.exit(1)


def emit(obj, indent=None):
    print(json.dumps(obj, indent=indent))


def fan_out(items, fn):
    """Map `fn` over `items` concurrently. Threads are correct here because the work is
    pure subprocess I/O -- providers and hosts are already isolated in their own
    processes, so wall clock is the slowest one instead of the sum.

    Two reasons that matters, one per caller: an unreachable host costs its own ssh
    timeout rather than everyone else's, and a cloud adapter's describe/capacity APIs
    take seconds, not milliseconds.
    """
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        return list(pool.map(fn, items))


def resources_path(arg):
    """--resources, else $MLCLAW_RESOURCES, else the workspace named in CLAUDE.md.

    The fallback is what makes the skill's promise true — `reap` and `release` have to
    run with zero upstream state, and requiring the caller to already know the
    workspace path is upstream state."""
    for candidate in (arg, os.environ.get("MLCLAW_RESOURCES"), _from_claude_md()):
        if candidate:
            return os.path.expanduser(candidate)
    die("permission", "cannot locate resources.json",
        hint="pass --resources, set $MLCLAW_RESOURCES, or set workspace_root in CLAUDE.md")


def _from_claude_md():
    """CLAUDE.md's `workspace_root:` is the single source of truth for where projects and
    the shared resources.json live (lifecycle/references/layout.md "Workspace and tool-repo location")."""
    # .../<repo>/lifecycle/scripts/lease/_common.py -> <repo>
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    try:
        with open(os.path.join(repo, "CLAUDE.md")) as fh:
            for line in fh:
                if line.startswith("workspace_root:"):
                    root = line.split(":", 1)[1].strip()
                    return os.path.join(root, "resources.json") if root else None
                if line.startswith("#"):
                    break  # the key is in the preamble; stop before the whole doc
    except OSError:
        pass
    return None


def load_resources(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        die("permission", f"resources.json not found: {path}")
    except json.JSONDecodeError as exc:
        die("permission", f"resources.json is not valid JSON: {exc}")


def add_shape_args(parser):
    """The requirement vocabulary, declared once. Adding a dimension (disk_gb, arch_max)
    is one edit here instead of one per subparser per file."""
    parser.add_argument("--gpu-count", type=int, default=1)
    parser.add_argument("--gpu-memory-gb", type=float, default=0)
    parser.add_argument("--arch-min")
    parser.add_argument("--host-ram-gb", type=float)


SHAPE_FLAGS = ("gpu_count", "gpu_memory_gb", "arch_min", "host_ram_gb")


def shape_flags(args):
    """Re-serialize the shape for a subprocess call, driven by the same list."""
    out = []
    for name in SHAPE_FLAGS:
        val = getattr(args, name, None)
        if val not in (None, 0):
            out += [f"--{name.replace('_', '-')}", str(val)]
    return out


def parse_arch(spec):
    """'sm_90' | '90' | '9.0' -> 90. Contract-level vocabulary, so it lives here rather
    than in whichever adapter needed it first."""
    if spec in (None, ""):
        return None
    s = str(spec).strip().lower().removeprefix("sm_")
    if "." in s:
        major, _, minor = s.partition(".")
        return int(major) * 10 + int(minor)
    return int(s)
