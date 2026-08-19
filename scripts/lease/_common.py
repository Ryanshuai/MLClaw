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
    # `ensure_ascii=False` for the same reason `emit` gives, and it matters more here:
    # the error path is where the em-dashes and arrows live, and a hint that comes out
    # as `\u2014` escapes is read by whoever is already having a bad time.
    print(json.dumps({"error": cls, "detail": detail, **extra}, ensure_ascii=False))
    sys.exit(1)


def emit(obj, indent=None):
    """Success payload on stdout, compact by default.

    `ensure_ascii=False` is not cosmetic and not optional: every other emitter in
    this repo uses it, and without it a non-ASCII host label or note comes out as
    backslash-u escapes -- readable only to whoever knows to decode them. A record
    nobody can read is not a record.

    Differs from `shared/_records.py -> emit(obj)` on purpose, which is why the
    signature differs too: 15 of 16 call sites here are small provider payloads
    read by a script, so compact is the right default; the one place a human
    reads it passes `indent=2`. Same-name-different-contract is only safe while
    the signatures also differ -- see `load_layout_contract` for the case where
    they did not.
    """
    print(json.dumps(obj, indent=indent, ensure_ascii=False))


def sweep_result(units, checked=(), unreached=(), storage=None):
    """The `sweep` / `history` envelope. A helper rather than a convention because the
    convention is what fails: an adapter that returns a bare list is indistinguishable
    from one that swept everything and found nothing, and the difference is a bill.

    `unreached` is non-empty exactly when some corner of the provider's scope did not
    answer -- a host that timed out, a project whose list errored, a region the
    credential could not reach. `complete` is derived from it, never passed in, so an
    adapter cannot report `complete: true` while naming what it missed.

    `storage` is the second billing category and it is separate from `units` because it
    outlives them: a volume survives the instance that declared it, so it is not a field
    on an instance row -- there may be no instance row left.

    The default is `None` rather than `()`, and that is the load-bearing part. An adapter
    that never learned about storage passes nothing and the key is ABSENT; one that looked
    and found none passes `[]` and the key is present and empty. With `()` as the default
    the first would silently render as the second, which is the exact reading that lets
    residual billing go unmeasured while `reap` prints a total. See `sweep_storage_known`.

    Contract: `skills/lease/references/contract.md` "Scope completeness" and
    "Storage is the second meter".
    """
    unreached = list(unreached)
    out = {"units": list(units),
           "scope": {"complete": not unreached,
                     "checked": list(checked), "unreached": unreached}}
    if storage is not None:
        out["storage"] = list(storage)
    return out


def sweep_storage_known(payload):
    """Did this sweep look at storage at all?

    Three states, not two, exactly as with `scope`: a list of volumes, an empty list
    (looked, none there), and **no key** (this adapter never looked). Reading the third
    as the second is how a provider whose adapter predates the storage rule reports
    `$0.00/hr` in residual billing forever -- the number is not wrong, it is unmeasured,
    and only the missing key says so.
    """
    return isinstance(payload, dict) and "storage" in payload


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


def resources_from_workspace_root(arg):
    """--resources, else $MLCLAW_RESOURCES, else the workspace named in CLAUDE.md.

    Not the same third fallback as `data-collect/collect.py ->
    resources_beside_project`, which looks beside the project directory. Named
    apart so the divergence is visible: one name for two resolvers that can land
    on different registry files is a disagreement nothing downstream can see.

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
    the shared resources.json live (references/layout.md "Workspace and tool-repo location")."""
    # .../<repo>/scripts/lease/_common.py -> <repo>
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


# The requirement vocabulary, declared **once**. Adding a dimension (disk_gb,
# arch_max) is one line here instead of one per subparser per file.
#
# It used to be two lists that had to agree: four `add_argument` calls plus a
# `SHAPE_FLAGS` tuple, under a docstring claiming "driven by the same list". Add
# `--disk-gb` to only one of them and the failure is silent in whichever
# direction you got wrong -- a flag the user passed never reaches the provider,
# or a name nobody registers is skipped. One table cannot disagree with itself.
SHAPE_ARGS = (
    ("gpu_count", {"type": int, "default": 1}),
    ("gpu_memory_gb", {"type": float, "default": 0}),
    ("arch_min", {}),
    ("host_ram_gb", {"type": float}),
)
SHAPE_FLAGS = tuple(name for name, _ in SHAPE_ARGS)


def add_shape_args(parser):
    for name, kw in SHAPE_ARGS:
        parser.add_argument(f"--{name.replace('_', '-')}", **kw)


def shape_flags(args):
    """Re-serialize the shape for a subprocess call, driven by the same table.

    Direct attribute access, not `getattr(args, name, None)`. Every call site is
    a subparser that ran `add_shape_args`, so the default was unreachable -- and
    what it hid is the one error worth seeing: a name in the table that no parser
    registers would have been silently skipped instead of raising.
    """
    out = []
    for name in SHAPE_FLAGS:
        val = getattr(args, name)
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
