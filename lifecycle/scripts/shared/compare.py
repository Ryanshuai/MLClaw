"""The one definition of "are these two values equivalent" in MLClaw.

Three copies of this logic landed at once — in `list_runs.py`, in
`compare_baseline.py`, and in `validate_ground_truth.py` — and they already
disagreed at the moment they were written:

    scope A                  scope B                  list_runs   compare_baseline
    {"samples":[1,2,3]}      {"samples":[3,2,1]}      same        DIFFERENT
    {"dataset":"COCO"}       {"dataset":"coco"}       DIFFERENT   same
    {"samples":20,"_c":"…"}  {"samples":20}           same        DIFFERENT
    {"samples":20}           {"samples":20.0}         DIFFERENT   same

So the same two runs were comparable to a leaderboard and un-comparable to a
baseline diff, or the reverse — which is the exact failure the comparability
rule exists to prevent, reproduced inside the fix for it. Everything that asks
"equivalent?" now asks here.

The settled semantics, and why:

- **null values and `_comment*` keys are dropped.** They are annotation, not
  scope. A template comment must not make two identical runs incomparable.
- **all-scalar lists are order-insensitive.** `samples: [3,1,2]` and
  `[1,2,3]` describe the same 3 samples.
- **numbers compare across int/float.** `20` and `20.0` survive a JSON
  round-trip as either; treating them as different scopes is an artifact.
- **strings compare exactly, no case folding.** Scope values are frequently
  paths and dataset directory names, where `COCO` and `coco` can genuinely be
  two different things on disk. Being wrong in this direction is loud (a
  refused comparison) rather than silent (a fake one).

Stdlib only. Imported from hyphenated script directories with the usual
`sys.path.insert(0, <…>/lifecycle/scripts/shared)` line.
"""
import json

# `direction` as written by humans across the stage templates. The training
# schema says max/min; eval configs in the wild say things like
# `higher_is_better`. Accepting both in one place beats one script hard-failing
# on a value another script reads happily — the user's config is either legal
# or it is not, and that answer must not depend on which script asks.
DIRECTION_ALIASES = {
    "max": "max", "maximize": "max", "higher": "max", "higher_is_better": "max",
    "up": "max", "increase": "max", "greater": "max",
    "min": "min", "minimize": "min", "lower": "min", "lower_is_better": "min",
    "down": "min", "decrease": "min", "less": "min",
}

UNSPECIFIED_SCOPE = "unspecified"


def normalize_direction(raw):
    """-> 'max' | 'min' | None. None means "not recorded or not recognized"."""
    if not isinstance(raw, str):
        return None
    return DIRECTION_ALIASES.get(raw.strip().lower())


def norm_scalar(v):
    """Canonical form of a leaf value for equivalence testing."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def _is_scalar(v):
    return v is None or isinstance(v, (bool, int, float, str))


def normalize(value):
    """Recursively canonicalize a value: drop nulls and `_comment*` keys from
    dicts, order-normalize all-scalar lists, coerce numbers."""
    if isinstance(value, dict):
        normalized_dict = {}
        for k, v in value.items():
            if v is None or (isinstance(k, str) and k.startswith("_comment")):
                continue
            normalized_dict[k] = normalize(v)
        return normalized_dict
    if isinstance(value, list):
        normalized_items = [normalize(v) for v in value]
        if all(_is_scalar(v) for v in normalized_items):
            # sort by (type name, repr) so mixed scalars stay orderable
            return sorted(normalized_items, key=lambda x: (type(x).__name__, repr(x)))
        return normalized_items
    return norm_scalar(value)


def values_equal(a, b, tol=0.0):
    """Equivalence under `normalize`, with an optional float tolerance."""
    a, b = normalize(a), normalize(b)
    return _eq(a, b, tol)


def _eq(a, b, tol):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_eq(a[k], b[k], tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float) and tol:
        return abs(a - b) <= tol
    return a == b


def scope_key(scope):
    """A hashable identity for a run's `scope`. Equal keys == comparable scopes.

    Empty scope collapses to `UNSPECIFIED_SCOPE` — its own bucket, never equal
    to a real one. An unrecorded scope is not evidence of an equal workload.
    """
    if not scope:
        return UNSPECIFIED_SCOPE
    norm = normalize(scope)
    if not norm:
        return UNSPECIFIED_SCOPE
    return json.dumps(norm, sort_keys=True, default=str)


def scopes_equivalent(a, b):
    return scope_key(a) == scope_key(b) != UNSPECIFIED_SCOPE
