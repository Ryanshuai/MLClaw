# The five axes — what each probe reads, and how to pin it

`repro.py check` reads only records and the local filesystem: no network, no ssh. That restraint is what makes it safe to run on a whim, and it also sets the honest ceiling on what it can say. A path on a machine that nobody asked is `unverifiable`, never `intact`.

Every axis has the same four verdicts, and the fourth is the load-bearing one. **A probe that could not run never collapses into `intact`.** This is the same rule as "never record a metric you did not read", one domain over: "the commit resolves" and "no commit was recorded" are different facts, and only the first is evidence.

Severity for the overall verdict is `gone > drifted > unverifiable > intact` — deliberately not the order the enum happens to be written in. `drifted` outranks `unverifiable` because a known difference is actionable now while an unknown one is not; the unverifiable set is reported *next to* the overall verdict rather than folded into it, and `close` refuses `reproduced` on an unverifiable axis independently.

---

## data

**Reads** `run.json → lineage.parents`, splitting it three ways:

| Parent form | Probe |
|---|---|
| `datasets/<id>@<snapshot>` | `datasets/<id>/snapshots/<snapshot>/snapshot.json` — does it exist, and does it carry `data_retired`? If it does, the census join below |
| `handoffs/<handoff_id>` | `handoffs/<id>/handoff.json` — is `status` still `accepted`, and what was the coverage? |
| neither (a plain path in `sources.json`) | nothing can be checked |

| Verdict | When |
|---|---|
| `intact` | every citation resolves, no retirement stamp, handoffs still accepted |
| `drifted` | a handoff is no longer `accepted`, or a retirement's units all survive elsewhere |
| `gone` | the snapshot record is missing, or a complete post-retirement census finds retired units at no location |
| `unverifiable` | **the run cited nothing** — it read a path, and a directory that still exists says nothing about whether its contents are the ones that trained. Also: a retirement no census can be shown to postdate |

**This is the axis where MLClaw can give a real answer and other tools cannot.** A path string recorded in MLflow silently resolves to different data a month later with nothing raising. A citation is a frozen membership set, so "retired" is a fact somebody wrote down — and `/data-retire`'s `data_retired` stamp exists precisely so this probe can find it.

### A retirement stamp is not by itself a verdict

**`/data-retire` frees space at one location** (`plan --at <loc>`), and `below_min_copies` is excluded by default — so the ordinary way to produce a stamp at all is waiving `cited_by_snapshot` alone, on a deletion that provably left copies behind. Reading every stamp as `gone` reports that run `not_reproducible`, which means *no relaunch gets past it*: the user stops while the data sits on the authority untouched.

So the stamp opens a question and a census answers it. Records-only, no network — the census files are local JSON:

| What the records show | Verdict |
|---|---|
| a **complete** census taken after the retirement finds every retired unit at some location | `drifted` — the frozen membership is intact, but `resolve --at <the deleted location>` no longer rebuilds it. Say which location to use instead |
| that census finds some of them nowhere | `gone` |
| the census is **incomplete** | `unverifiable` — a machine that did not answer may be holding them. "Could not look" is not "not there" |
| no census postdates the retirement, or the stamp carries no dated `retired_at` | `unverifiable` — an earlier census describes a disk that has since been written to. Route the user to `census.py scan`, which settles it |

**To pin it:** re-resolve the cited snapshot into openable paths and point the trial at that view.

```bash
python <mlclaw_root>/scripts/data-check/census.py resolve --project {PROJECT} \
    --dataset <id> --snapshot <snap> --at <location> --layer <l> \
    --out {RUN_DIR}/data_resolved.jsonl
```

Pass `resolve`'s own refusals through — a layer that is not present on every unit, or an `--at` naming a `backup`. When the snapshot is `gone`, there is nothing to pin and the axis is not a suspect: it is the answer.

**One refusal you must expect and override here**: `/eval-run` re-gates a `dataset:` candidate with `phase.py gate --to consume`, which refuses `snapshot_stale` — the snapshot's census predates inflow accepted since. That is correct for a new experiment and wrong for a trial, whose whole job is to consume the *old* set. Acknowledge it (`gate --acknowledge <n>`) and say in the same breath that a stale snapshot is being consumed deliberately. Re-freezing here would change what is being reproduced.

**The `unverifiable` case cannot be pinned retroactively.** Nobody can reconstruct which files a bare path held. Say so, and note that future runs fix it by citing a snapshot (`/data-freeze`).

---

## code

**Reads** `run.json → code`, against the stage's code dir (`code/_source` when it exists, else `code/`).

This is the only thing anywhere that checks the contract `code_snapshot.py` writes:

```bash
git checkout <origin_commit> && git apply <run_dir>/<dirty_patch_path>
```

| Verdict | When |
|---|---|
| `intact` | `origin_commit` resolves via `git cat-file -e`, and the patch file is present (or the tree was clean) |
| `drifted` | the commit resolves but `dirty_patch_path` names a file that is gone — checkout alone rebuilds a different tree, and the patch *was* the difference |
| `gone` | the commit does not resolve in this repo. The tree that ran cannot be rebuilt |
| `unverifiable` | no `origin_commit` recorded, **or `code.reproducible` is false** |

**`reproducible: false` can never become `intact`, at any later date.** It means a differing file was too large to embed, so checkout + apply rebuilds a tree that is *not* the one that ran. A number that matches under those conditions is evidence, not proof — which is exactly why `close` refuses `reproduced` while any axis is unverifiable.

**To pin it:** `git checkout <origin_commit>` in a scratch worktree, `git apply` the patch, and run the trial against that tree. Never check out over the user's working tree. A `gone` commit is sometimes recoverable — `git fetch --all`, or find the branch that still contains it — and that is worth one attempt before concluding.

---

## env

**Reads** `run.json → env` versus the current env, by invoking `scripts/shared/capture_env.py` rather than reimplementing the query.

Only packages whose version silently changes what the model computes count toward the verdict (`torch`, `numpy`, `transformers`, `timm`, `mmcv`, `mmdet`, `detectron2`, `opencv-python`, `deepspeed`, …), plus `cuda` / `cudnn` / `nvidia_driver` / `gpu` / `gpu_count` / `python`. Everything else is recorded under `also_changed` and leaves the verdict alone.

**That restriction is deliberate and it is a correctness choice, not laziness.** A verdict that fires on a pandas patch bump gets ignored, and the torch bump gets ignored along with it — the same reasoning that keeps `phase.py`'s gates from firing on everything.

| Verdict | When |
|---|---|
| `intact` | every key package and device field matches |
| `drifted` | ≥1 key package or device field differs |
| `unverifiable` | no env recorded for the run, or the current env could not be read |

**To pin it:** build an env at the recorded versions (`{WORKSPACE}/resources.json → local.env_manager`) and run the trial in it. A `gpu` or `nvidia_driver` difference usually cannot be pinned at all without the original machine — say that rather than pretending the pin is available, and consider whether `/lease` can get a matching one.

---

## params

**Reads** the run's `config_snapshot.json` against the stage's current `config.json → param_injection.items`.

| Verdict | When |
|---|---|
| `intact` | every snapshotted param is still classified and still `overridable: true` |
| `drifted` | a param that was externally settable is now `overridable: false` — **the code moved under the recorded config** |
| `unverifiable` | no `config_snapshot.json`, or params absent from `param_injection` (run `/train-init` Step 2b) |

The `drifted` case is subtle and worth stating to the user in full: the original run's numbers are still real. What broke is that relaunching with the recorded config no longer produces them, because the value now comes from a literal in the code instead of the flag that was passed. Nothing raises; the flag is simply ignored.

**To pin it:** edit the shadowing site named in `param_injection.items.<key>.evidence` back to the snapshotted value for the duration of the trial — which makes this pin a code change, so it rides on the code axis's scratch worktree and never touches the user's tree.

---

## artifacts

**Reads** the non-dataset, non-handoff entries of `lineage.parents` — upstream runs whose outputs this run consumed — and checks that each declared path in the parent's `outputs` is still on disk.

run-mechanics states the rule this enforces: *"Base's artifact must exist for this run to be reproducible."*

| Verdict | When |
|---|---|
| `intact` | no upstream artifacts consumed, or every declared output is present |
| `gone` | a parent run record is missing, or a declared output is absent from disk |
| `unverifiable` | a parent ref is not in `<stage>/<run_id>` form |

`retention.py` is what deletes these, and **it has no idea who cited them.** A retention policy that kept the best and last checkpoint is correct on its own terms and can still remove the exact one an eval run consumed. This probe is the only thing that notices.

**To pin it:** there is nothing to pin — a deleted checkpoint is `gone`, not drifted. If the parent training run is itself reproducible, regenerating the checkpoint is a new run and a different question; say so rather than quietly substituting a re-trained artifact for the one that was consumed.
