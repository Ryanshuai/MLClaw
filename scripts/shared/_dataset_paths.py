#!/usr/bin/env python3
"""Locating a dataset's records — one definition, because these had diverged.

`census.py`, `phase.py`, `retire.py`, `curate.py` and `online.py` each carried
their own copy of "the dataset directory" and "the newest census", and the copies
were not the same:

  * two of the five `dataset_dir` copies skipped `expanduser`. Harmless today
    only because every caller happens to expand at its entry point.
  * `latest_census` came in three variants. Two filtered `census_*.json`; the
    third took **any** `.json` in the directory. Nothing writes a differently
    named file there today, so the divergence is latent — but this is the
    function that decides *which census a decision is made against*, and
    `/data-retire`'s containment rule is ranked against exactly that answer.
    Three implementations of it is three answers waiting to differ.

The stricter filter is the one kept: a census record is named for its
`census_id`, so anything else in that directory is not a census.

Import as `shared/_records.py` documents.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _records import read_json  # noqa: E402

CENSUS_PREFIX = "census_"

# `dataset.json -> replication.min_source_copies`, when the field is absent.
# Was two independent `or 2` fallbacks (census.py and retire.py each wrote the
# literal) -- the same drift this module's docstring already describes for
# dataset_dir/latest_census, just in a config default instead of a path.
DEFAULT_MIN_SOURCE_COPIES = 2


def dataset_dir(project, dataset):
    """`{PROJECT}/datasets/<id>/`. Expands `~` here rather than trusting every
    caller to have done it."""
    return os.path.join(os.path.expanduser(project), "datasets", dataset)


def census_paths(ddir):
    """Every census record for this dataset, oldest first.

    Sorted by filename, which sorts chronologically because a `census_id` is
    `census_<YYYYmmdd>_<HHMMSS>` — see layout.md "Dataset identity and census
    records". A `.json.tmp` left by an interrupted write is excluded by the
    suffix test, which is why the ordering survives a crashed scan.
    """
    cdir = os.path.join(ddir, "census")
    if not os.path.isdir(cdir):
        return []
    return [os.path.join(cdir, n) for n in sorted(os.listdir(cdir))
            if n.startswith(CENSUS_PREFIX) and n.endswith(".json")]


def latest_census_path(ddir):
    """Path of the newest census, or None when nothing has ever scanned."""
    paths = census_paths(ddir)
    return paths[-1] if paths else None


def latest_census(ddir):
    """The newest census record itself, or None.

    None means *nobody has looked*, which is not the same as an empty dataset and
    must never be reported as one — CLAUDE.md "Never report data you could not
    look at". Callers also have to check `complete` before ranking, freezing or
    deleting against what they get back: a partial census is a lower bound.
    """
    path = latest_census_path(ddir)
    return read_json(path, required=False) if path else None
