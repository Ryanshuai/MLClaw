# Run mechanics — shared by every run skill

Loaded on demand, not at session start. `/train-run`, `/eval-run`, `/infer-run`,
and `/refactor-run` must read this before Step 1; CLAUDE.md carries only the
headline rules and a pointer here.

Contract statements in this file are cited by checks in `contracts/` as
`run-mechanics.md ->` followed by the section heading in double quotes. (Spelled out
rather than shown, because the format example would otherwise be picked up as a
citation to a section named after the placeholder — `contract_docs.py` scans the
whole repo for exactly this pattern.)

## Run Skill Internal Dependencies

Run skills (infer-run, eval-run, train-run) share the same internal dependency chain. Each step depends on the previous step completing, and some steps have external dependencies that trigger other skills. Train-run adds a `monitor` step between `execute` and `collect_results` to handle long-running streaming state (heartbeat, last_step, latest_metrics).

```
Locate Project
     │
     │  [external: init done? ── no ──→ offer /{stage}-init]
     ↓
Fork check: "Base on a previous run?"
     │  - no  → proceed normally (fresh run)
     │  - yes → load base run's config_snapshot.json + sources.json + lineage.parents
     │          set fork_of = base run ID
     │          user modifies only what they want to change
     │          skip Step 1 if sources unchanged
     ↓
Step 1: Resolve Assets                        depends on: init (items defined),
     │  - CHOOSE from `candidates`; see                  resources (credentials)
     │    "Asset resolution (Step 1 detail)"
     │  - only ask for a path when no
     │    candidates block exists at all
     │  [external: credentials fail? ── invoke /resources ── resume]
     │  - validate paths exist, test connectivity
     ↓
Step 2: Create Run                            depends on: assets resolved
     │  - create run dir + run.json
     │  - code snapshot — see "Code snapshot (Step 2 detail)" below
     │  - env snapshot (packages from lifecycle/run.json template)
     │  - dependency check (required vs installed)
     │  - snapshot resolved assets → sources.json
     │  - if fork_of is set: compute lineage.variation_summary
     │    (diff runtime_params vs base run; null otherwise)
     │  - if user provided hypothesis: store in run.json -> hypothesis
     ↓
Step 3: Build & Execute                       depends on: run created
     │  - resolve ${} references (from assets)
     │  - build command (argparse/yaml/hydra/hybrid)
     │  - cwd + output_dir — see "Launch contract (Step 3 detail)" below
     │  - debug mode (sync, limited data) or production mode (background/remote)
     │  [if fail: diagnose → fix → retry loop]
     ↓
Step 4: Collect Results                       depends on: execution finished
     │  - finalize run.json (status, duration, metrics) — this is the
     │    only writeback; there is no separate index file to maintain
     │  - extract metrics (from stdout/files)
     │  eval-run extras: per-class metrics, baseline comparison, offer baseline update
     │
     │  [external: eval-run only ── offer /eval-report]
     ↓
  Done (pop from stack)
```

**Internal step recording**: each run skill writes step completion to `run.json → steps`. Each step has `status` (`null` / `completed` / `skipped` / `failed`) and `at` (timestamp). Used for debugging, reproducibility, and resume — on resume, the skill reads `run.json → steps` to skip completed steps.

Steps correspond to headings in the skill's SKILL.md: `##` headings are major steps, `###` headings are sub-steps. All are recorded. Different stages may have different steps (e.g., training-run may add a `monitor` step).

`steps.ad_hoc` is an array for unplanned actions that don't match any predefined step — e.g., fixing a file permission, patching a config typo, installing a missing package. Each entry: `{ "name": "...", "description": "...", "after_step": "...", "status": "...", "at": "..." }`. If the same ad_hoc action shows up across multiple runs, it's a signal to promote it into a formal step in the SKILL.md.

### Asset resolution (Step 1 detail)

Defined here once because all three run skills do it and each had started describing it
differently. `/train-init` and `/eval-init` fill `input.json` / `artifacts.json -> candidates`;
`/infer-init` deliberately does not, since inference inputs change every run.

**When a `candidates` block exists, it is the source — do not interrogate the user path by path.**
Asking for a path that init already located throws away the work init did, and invites a different
answer than the one that was validated.

**Every `match` value routes somewhere. Filtering to `ok` and ignoring the rest is a bug**, and it is
the bug this section exists to name: a run skill that shows only the `ok` rows and then reports "no
usable options" has erased the difference between *not here* and *could not look* — the same
collapse that `match: "unreachable"` was added to prevent, committed one layer further down.

| `match` | What the run skill does |
|---|---|
| `ok` | offer it; default to it when there is exactly one, and confirm rather than ask |
| `mismatch` | show it **with its `notes`**, never as the default. The notes say what is wrong — wrong format, wrong sample count — and that is usually a conversion away, not a dead end |
| `absent` | never a choice. It is the layout reference (the original author's path) and the yardstick for the others |
| `pending` | **stop.** Name the party, the spec version and the due date from the handoff, offer `handoff.py status --project {PROJECT} --open-only`, and do not fall through to asking for a path. Falling through is how a half-labeled directory gets picked over work that is still arriving |
| `unreachable` | **stop.** Name what is missing (a credential, a host) and route to `/resources`. Never present it as absent and never drop it — the asset is claimed to exist and only the check is missing |

Two more that are not `match` values but change how a chosen candidate resolves:

- **`location: dataset:<id>@<snapshot>`** — a citation, not a path. Re-gate it at launch
  (`phase.py gate --to consume`), then `census.py resolve --at <loc>` into the run directory. Never
  into the config: a resolved path embeds one machine's root. Cite `datasets/<id>@<sid>` in
  `run.json -> lineage.parents`.
- **`location: run:<stage>/<run_id>`** — a checkpoint from a run in this project (evaluation and
  inference only). Resolve through that run's directory and add it to `lineage.parents`, which is
  what makes the consuming run an edge rather than a number about an anonymous file.

**Candidates go stale.** They are machine-specific, so an rsync to another host invalidates every
path in the list. Re-verify the chosen one resolves before launching. If they all dangle, re-run the
init skill's locate step — do not fall back to interrogating the user, which produces a path nobody
judged against `items`.

**If nothing is `ok`, say which kind of no it is.** They route to different places, and a flat
"no data available" sends people to the wrong one: `absent`/`mismatch` → `/data-collect` or a
conversion; `pending` → the party and the date; `unreachable` → `/resources`.

### Code snapshot (Step 2 detail)

Every run skill captures the exact code state at run-time so a completed run is self-contained for reproduction.

- **`code_dir`** is resolved by the unified rule from "Code Source Resolution":
  ```
  code_dir = stages/<stage>/code/_source if exists else stages/<stage>/code
  ```
- **Helper**: `python <mlclaw_root>/lifecycle/scripts/shared/code_snapshot.py <code_dir> <RUN_DIR>` — outputs a JSON dict; merge it into `run.json -> code`.
- **Git working tree** (typical): records `repo` (origin URL), `branch`, `origin_commit` (SHA). If the tree differs from that SHA, writes `<RUN_DIR>/code_dirty.patch` and fills `dirty_patch_path` + `dirty_files_count`. Reproduction contract: `git checkout <origin_commit> && git apply <run_dir>/code_dirty.patch`, run **from the code dir**.
- **`code_dir` is often not the repo root.** `/project-init` puts `github` / `server` / `null` sources at `stages/<stage>/code/` — inside the *project's* git repo. The patch is then scoped to that subtree and `repo_subdir` records the offset, so the record describes the stage's code and not the whole project. Two consequences worth knowing: the patch still uses repo-relative paths (a `--relative` patch applies **nothing** from a subdirectory and exits 0 while doing it), and `origin_commit` is still repository-wide — editing `project.json` moves it, so it is not a stable identity for this stage's code alone. `list_runs.py --commit` inherits that limitation.
- **Untracked files are part of the diff.** A new `model_v2.py` that was never `git add`ed is invisible to `git diff HEAD`; counting only tracked changes records a tree as clean that is not, and the run reproduces different code with nothing raised.
- **Read `reproducible` before trusting the record.** False means a file that differed was not embedded, so `checkout + apply` will not rebuild the tree that ran. Surface it *before* launching, not at reproduction time.
- **Non-git directory**: refused, cleanly (exit 1 — the script worked and the answer is no; one line, no traceback). A tree with no SHA cannot be reproduced. On this refusal `/train-run` should offer `git init` + an initial commit rather than stopping — a one-off script folder is a normal starting point, and refusing to train over it is friction, not safety. Same for a repo with no commits.
- **No tree at all** (`code_source.source: framework`): `--framework <pkg>==<version>`, which records the *other* reproduction contract — `install <pkg>==<version>` in place of `checkout <sha> && apply <patch>`. `code.kind` says which contract the record is, and that field is the point: a framework record's `origin_commit` is null **by construction**, and without `kind` a reader cannot tell that from a capture that failed. `dirty_files_count` is **null, not 0** — "there is no tree" and "the tree was clean" are different facts. An unpinned `--framework` is refused for the same reason a missing SHA is: a package name is not a contract. **Do not offer `git init` here** — site-packages is not the user's to initialize. The contract's one blind spot rides along as a standing warning: a local edit to the installed package is invisible, where a tree would have produced a patch. Full rationale, and the reason `param_injection` carries extra weight on this branch: `layout.md` → "Code Source Resolution".

Mechanism (index handling, size threshold, field breakdown) lives in the script's own docstring; the assertions live in `contracts/contract_code_snapshot.py`. Don't restate either here — a copy in this file is a copy that drifts.

The same call applies to all run skills (`/train-run`, `/eval-run`, `/infer-run`, `/refactor-run`) — no per-skill variant.

### Launch contract (Step 3 detail)

Three rules, uniform across all run skills:

1. **`cwd = <code_dir>`** (the same path used in Step 2). For module-style entry commands like `python -m pkg.train` this is mandatory — package imports won't resolve from anywhere else.
2. **`output_dir` (or framework equivalent) must be overridden to an absolute path under `<RUN_DIR>/output/`**. The default config's relative `output_dir` would land artifacts back in the user's code repo, where MLClaw can't manage them and the next run would overwrite them. Override syntax depends on the framework:
   - omegaconf / hydra: `--set output_dir=<abs>` or `+output_dir=<abs>`
   - argparse: `--output-dir <abs>` / `--output_dir <abs>`
   - HF Trainer: `--output_dir <abs>`
   - accelerate: `--output_dir <abs>`
   - env var: `OUTPUT_DIR=<abs>`
   Which form a given codebase uses is recorded as a `param_injection` entry (rule 3), not re-derived per framework at launch time.

3. **Every managed param needs a `config.json -> param_injection.items` entry, and `runtime_params` must hold the effective-at-runtime value.** A config file's declared value is not necessarily the one the code uses: a `--lr` flag is dead if the optimizer is built with a literal, a `warmup` value is discarded if it's recomputed from `epochs`, and `lr * world_size` means one config trains differently on 1 vs 8 GPUs. Passing a param the code ignores produces a **fake metric** — the run completes, records the value you asked for, and reports a number produced by a different value. Nothing errors.

   ```json
   "lr":   { "via": "cli", "flag": "--lr", "overridable": true, "evidence": "train.py:33" },
   "seed": { "via": "hardcoded", "overridable": false, "evidence": "train.py:12",
             "note": "torch.manual_seed(42) after arg parse — --seed is dead" }
   ```

   `via`: `cli` (+`flag`) | `yaml` (+`key`) | `env` (+`env`) | `hardcoded` | `derived` (+`derived_from`). The last two imply `overridable: false`.

   - **`/{stage}-init`** fills this while selecting managed params, and must not put an `overridable: false` param into `runtime_params` — it isn't a knob. Keep it in `param_injection` as documentation of why, and surface it as a risk ("changing this needs a code edit at `<path:line>`").
   - **`/{stage}-run`** builds the launch command per-param from these entries instead of guessing from `config_format`. A `runtime_params` key with no entry is an error — stop and ask; never fall back to trying `--key value`, because a silently-ignored flag yields a run whose recorded config lies about what ran.
   - This applies to params **MLClaw sets on its own** too, not just user-supplied ones: debug-mode `epochs=1` / sample limits, OOM auto-retry `batch_size ÷ 2`, resume-from-checkpoint flags. Those are the dangerous ones — the user never typed them, so nobody is watching whether they took effect.

4. **Write `run.json -> workload` from the same entries you just built the command from.** `world_size`, `batch_size`, `grad_accum`, `epochs`, `samples_per_epoch` — plus `source`, mirroring each one's `param_injection.via`. This costs nothing at launch: every value is already resolved by rule 3, one line above.

   It is a separate rule because the failure it prevents is not rule 3's. Rule 3 stops a param the code *ignores*. This one stops a run that finished having done **a different amount of work than it was asked to do** — a dataloader that dropped a shard, an early exit that still wrote a checkpoint, a resume that counted the same epochs twice. Those all produce a complete run with plausible metrics and no error, and the only way to see them is to compare what was asked against what happened. Nothing can make that comparison if the ask was never recorded.

   **Null means not recorded — never fill a default.** A `grad_accum` defaulted to 1 because the code did not say turns an unknown into a stated fact, and the sample count computed from it is wrong by whatever the real value was. `samples_per_epoch` should come from the cited dataset snapshot when there is one; leave it null rather than counting files.

   Applies to every run skill, not just `/train-run`: eval and inference have no `epochs` or `grad_accum` and leave them null, but `world_size`, `batch_size` and `samples_per_epoch` are exactly as load-bearing there — an eval that silently scored two thirds of the set is the same defect as a train that saw two thirds of it, and `samples` ≠ `dataset.num_samples` is already a `mismatch` gate in `/eval-init`.

   Do not confuse this with `scope`, which holds what the run **actually reached** and is read back out of the log afterwards. Both sides are needed and neither is derivable from the other; that gap is the only visible symptom of a run that completed having done different work than it was given.

Stage-specific extras (production mode launching, monitoring, ETA computation, finalize hooks) go in each run skill's SKILL.md — these four rules do not.

### Baseline measurement (fine-tune only)

**A fine-tune exists because the base was not good enough on this data, so the only number it produces that can be read is the delta against that base.** The child's absolute score has no scale on its own: `box mAP 0.914` is a fact about nothing until `0.451` sits next to it. And the base's *published* number cannot supply that scale — it is a `claim` measured on somebody else's `scope`, which the fine-tune section of `/train-run` SKILL.md already says. That leaves exactly one baseline worth the name: **the base, put through the same measurement, on this run's data, with the same settings.**

It costs one eval, and both inputs are already in hand — you cannot fine-tune without the base weights and you cannot train without the data. **Take it before launch, not at finalize**, for four reasons that are all the same reason:

- after the run, measuring the base is pure expenditure against a model nobody is shipping, so it does not happen;
- by the time anyone wants it, the data, the weights or the env has moved, and then it cannot happen;
- it exercises the exact path that will measure the child, on known-good weights, before hours are spent — a smoke test that costs nothing extra;
- if the run crashes, the half you already paid for survives.

**Both measurements are `/eval-run` runs, not something the training skill performs.** Measuring is already a skill with a configured entry command, ground truth, and metric extraction; a second evaluator living inside `/train-run` is the mistake `/eval-init` refuses to make in the other direction ("two implementations of *where is the data* is how they start disagreeing"). Being runs is also what makes them worth having: each carries a `run_id`, a `scope`, a code snapshot and an extracted metric, where a pair of loose JSON files would carry a schema invented on the spot. The training run cites them:

```json
"baseline_delta": {"before": "evaluation/run_...", "after": "evaluation/run_...",
                   "waived": null}
```

This gives `/train-run` a real dependency on an **evaluation stage existing** for the fine-tune case. That is a cost, and the honest handling is to say it and route to `/eval-init` — not to grow a private evaluator so the dependency disappears. A fine-tune whose worth cannot be measured is a fact about the project, and the block is the finding.

The **diff** between the two is `eval-run/compare_baseline.py` — it already reads `direction` from config instead of guessing from a metric's name, and already grades comparability into hard versus unverifiable blockers. `baseline_delta.py` holds only what a fine-tune knows and neither of those can:

```bash
# a fine-tune whose base was never measured (and not waived) -> exit 1
python .../baseline_delta.py check <TRAIN_RUN_DIR>

# were the two measurements taken the same way, each under its own weights' protocol?
python .../baseline_delta.py protocol <before eval run> <after eval run>
```

**`protocol` guards the defect that matters more than the missing measurement**: two measurements that both exist, both look fine, and were not taken the same way. It refuses on any differing settings key, and — the free half, since the value sits inside the weights — on any key where a measurement departs from the checkpoint's *own recorded training args*.

Found live, on a yolo26 segmentation fine-tune: validating at the library's `overlap_mask=True` instead of the `False` this model family trains under moved box mAP50-95 from `0.9142` to `0.9027` and wall from `0.9672` to `0.9445`. **`compare_baseline.py` passes this, correctly** — same mode, same scope, nothing about the *scale* is wrong, which is the only thing that check is about. And because both sides carried the same default, the delta stayed honest while every absolute number quietly stopped being comparable to any figure published for those weights.

Both refusals take a per-key `--waive-setting KEY='<why>'`; `check`'s waiver is `run.json -> baseline_delta.waived`. Two absences are reported rather than passed: a measurement that recorded no settings **fails** (an unrecorded protocol is not a matching one, the same rule the repro axes draw between `intact` and `unverifiable`), and a checkpoint that recorded no training args **warns** — absent evidence, not agreement.

### Metric stream

Three words with fixed meanings. They were interchangeable while the stream was
just whatever the code happened to write; they no longer are, and a skill that
mixes them up reads the wrong file.

| Word | What it is | Where |
|---|---|---|
| **source** | what the training code itself wrote — tfevents, its own jsonl, stdout | `output.json -> metrics.log_path`, relative to `<RUN_DIR>` |
| **stream** | the normalized record layer MLClaw owns | `<RUN_DIR>/stream.jsonl` — a convention, not a config value |
| **record** | one line of the stream | — |

One script owns this: `python <mlclaw_root>/lifecycle/scripts/train-run/ingest.py
<stage>/output.json --run-dir <RUN_DIR>`. It holds a thin adapter per source
format, one shared records layer, and the sinks. The three scripts that read metric
records — `reconcile_metrics.py`, `select_checkpoint.py`, `retention.py` — all
resolve through `resolve_stream()` and so read the stream; pass them `--run-dir`
rather than a path, or they rank whatever file you named. (`list_runs.py` and
`/train-tune` read `run.json`, not the stream.) Adding a source format is one
adapter and no change to any of them.

**A read that fell back to the source says so.** `resolve_stream` returns a `kind`
alongside the path, and `unnormalized_finding` turns `kind == "source"` into a
`warn`. Reading the source is the right fallback for a run created before there was
a normalizer — doing it silently is not, because then a missing `stream.jsonl`, a
stale one, and one that `ingest.py` deliberately refused to write all look like a
healthy normalized read.
`output.json -> metrics.log_format` names the **source** format and says nothing
about the stream's shape.

Not a per-format converter. A `tb_to_stream.py` beside a `wandb_to_stream.py`
would each re-implement grouping, provenance stamping and the write discipline,
and the third one written would disagree with the first two about something
nobody notices.

**Run ingest in the environment that produced the source.** The tfevents adapter
needs `tensorboard` importable, and the environment that wrote the events has it,
at the version that wrote them. On a remote run that means over ssh on the
training host, not locally.

**The stream is re-derived whole, never appended to** — temp file, then
`os.replace`, because monitor and the user may be reading it. A restart writes a
second tfevents whose step range overlaps the first with *different values for
the same steps*, so an incremental writer bakes in a resolution that later events
invalidate. It is a derived file; rebuilding costs nothing. Same reasoning as "no
`runs_index.json`" below: no incremental cache, no drift to handle. The cost is a
whole-source parse per monitor tick — seconds at worst, but do not call it
per-run inside a `/train-tune` loop.

**A normalizer groups and tags. It never renames a field.** `train/loss` stays
`train/loss`; no sanitizing to `train_loss`. The declared-schema-vs-actual-stream
comparison in `reconcile_metrics.py` is the only place the
`val_loss`-declared-but-`train_loss`-emitted bug gets caught, and a renaming
layer between the two launders exactly that mismatch — silently, because
renaming looks like tidying.

**A normalizer never invents a record type.** tfevents carries no equivalent of
`train_step` / `val_epoch`: `add_scalar` takes one tag at a time, so the grouping
was destroyed at write time. Stamping `type: "scalars"` on every record would
make `find_type_key` report *full coverage* for a classification that never
happened — worse than no key at all, which `classify()` already handles by
falling back to field-set matching. Omit the key and record why in
`stream_meta.json -> inferred`.

Record shape — metric fields stay at the **top level** (`select_checkpoint.py`
reads `r.get(best_by)`, `observed_fields` scans keys), and provenance keys are
`_`-prefixed so they cannot be mistaken for metrics:

```json
{"step": 5000, "wall_time": 1753900000.123,
 "train/loss": 0.312, "lr": 0.0003,
 "_src": "tfevents", "_group": "step"}
```

**Grouping is a recorded decision, not a constant.** Which `(tag, step, value)`
triples become one record has no universally correct answer: `step` merges train
and val into a co-occurrence that never happened, `step+namespace` breaks on
inverted hierarchies (`Loss/train` vs `train/loss`), `step+wall_time±ε` needs an
ε nobody can set. So `/train-init` shows the user the observed tag list, picks
one, and writes it to `output.json -> metrics.normalize.group_by`; the normalizer
stamps it on every record as `_group`. Default `step`.

Worth knowing before spending care here: **grouping affects `record_types`
reconciliation, not ranking.** Ranking needs `(metric value, index)`, and that
pair survives every grouping — checkpoint selection does not ride on this choice.

`<RUN_DIR>/stream_meta.json` is the sidecar answering "why is a derived file
trustworthy": source file list with mtime/size, `group_by`, `inferred` (what was
guessed and on what basis), any reader setting that could silently drop data, and
the count of overlapping steps discarded. It is a sidecar rather than a header
line in the stream because `observed_fields` would scan a header's keys as metric
names.

**What a source holds beyond metrics is recorded, not silently skipped.** A
tfevents directory routinely carries images, histograms, hparams, and a graph.
None of it belongs in the stream — nothing ranks a checkpoint by a segmentation
mask — but an unmentioned omission is indistinguishable from a run that logged
none, and the user who logged them will go looking. The kinds present land in
`stream_meta.json -> not_ingested` with a warning naming the `--logdir` that shows
them. Never load their payloads: in `EventAccumulator`'s `size_guidance`, `0`
means *unbounded*, so `{'images': 0}` pulls every sample image a run ever wrote
into memory. Override `scalars` only.

**No synthetic `ckpt_saved` records.** Reconstructing them from a `path_pattern`
glob is tempting, but `inventory()` in `select_checkpoint.py` already pairs files
against records using that same pattern. Two sources for one fact disagree
eventually.

#### Viewing (`<RUN_DIR>/tb/`)

**On by default** — `ingest.py` renders it on every call, which means *whenever it
is possible and useful*. `--no-tb` opts out, for a batch re-derive over many runs
where nobody is looking. Two conditions make it a no-op, and neither is an error:

- **The source already is tfevents.** Nothing is written. `--logdir <RUN_DIR>`
  overlays every subdirectory as a separate run, so rendering the same scalars
  again would draw every curve twice under two names — and the code's own file
  already carries the images and histograms a render never could.
- **No writer package is importable** (`torch.utils.tensorboard`, then
  `tensorboardX`). Reported as a `warn`; the stream still lands. A viewer that
  cannot be built must never break a run's monitoring.

Status: the selection layer (`tb_points` — which fields become which tags at which
step) is contract-covered, and so are both no-op paths. The `add_scalar` write path
itself has **never executed** — no environment on hand has a writer package.
Verify on first real use: open the render and check the tag list against the
stream's field names.

TensorBoard is a leaf consumer and never reads the stream. When the source
already *is* tfevents, point the user at the code's own file and write nothing.
Rendering at all is permitted for the same reason `chain.md` is: no decision path
reads it back.

**The render target is append-only** — the opposite discipline from the stream,
because TB tails its files and a rewritten one reads as steps going backwards. It
keeps a watermark, and losing that watermark duplicates a curve in a picture,
which is acceptable for something no decision reads. That asymmetry is why these
are two files and not one.

**Nothing may ingest the render target.** Write it with
`filename_suffix=".mlclaw"` so the filename self-identifies, resolve sources only
from `metrics.log_path` (never a recursive glob for `events.out.tfevents.*`), and
refuse any source path under `<RUN_DIR>/tb/`.

### Preprocessing contract (cross-stage)

`input.json -> preprocessing` records what happens to an input before the model sees it: normalization constants, resize mode, channel order, label index base, augmentation. Filled from code by the stage's init skill (`/train-init` Step 1b), never guessed — a framework default written into this field is worse than a blank, because a blank gets asked about.

**`normalization`, `input_layout`, and `label_transform` must be identical between training and any eval/infer stage of the same model.** A mismatch raises nothing anywhere: training converges, evaluation reports plausible metrics, inference returns plausible outputs, and the deployed model is quietly degraded. An eval stage whose preprocessing differs from training doesn't measure the model you trained — its metrics are fake in exactly the sense of "Metric comparability" below. Recurring instances: BGR/RGB swap (cv2 vs PIL), ImageNet constants reused on X-ray / satellite / spectrogram data, COCO 91-vs-80 category id remapping, an off-by-one background class.

**`augmentation` is training-only.** Non-empty in an eval or infer stage is a bug, not a variation.

When initializing a stage while another stage of the same project already has `preprocessing` filled, diff the three shared blocks and surface any difference before proceeding. Deliberate differences exist — test-time augmentation, a different inference resolution — but they must be stated at that moment, not discovered from a confusing metric three weeks later.

### Record integrity

Rules for anything a run writes down now that somebody reads later. They share one property: **breaking them raises nothing.** The run completes, a number is recorded, and it is the wrong number — discovered months later, if ever. Each is enforced by a check in `contracts/`; the check cites the rule, this file states it.

| Rule | Why it isn't obvious |
|---|---|
| An extracted metric is recorded only when it was actually read. Extraction failure and a metric the run never produced are **different**, and must not both become `null`. | A broken regex and an absent number read identically downstream, and both get silently skipped by comparisons. |
| Run timestamps are UTC with an explicit offset. | The canonical query sorts with `sort_by(.created_at)`. On naive local strings from machines in different zones that sort is wrong and looks fine. |
| A duration that could not be computed is reported as such, never left null in silence. | Null reads as "never started" — a real state — so a failed subtraction disguises itself as data. |
| A `run_id` identifies exactly one run. | One-second resolution plus `makedirs(exist_ok=True)` let two same-second launches share a directory; the second `run.json` write destroys the first record. `/train-tune` with `max_concurrent > 1` does this routinely. |
| The metric schema in `output.json` must describe the stream the code actually emits — right field, right split, right direction. | A schema naming `val_loss` against code that emits `train_loss` produces a complete run whose checkpoint was selected to fit the training set. Both numbers are real and both look plausible. |
| The chosen checkpoint and the recorded metric describe the **same artifact**. | When the peak epoch was never saved, falling through to the next-best is correct — recording the stream's peak beside the surviving file is a fake metric. |
| Retention plans and applies as two steps, never deletes what it cannot rank, and aborts wholly on drift. | It is the only irreversible operation here. "Confirm with the user" is not a safeguard: a list of filenames carries no evidence that the sort behind it was right. |
| `workload` records what the run was **asked** to do, and an unknown stays null rather than taking a default. | It is the only record of the ask, so nothing downstream can tell "8 GPUs were requested and 1 responded" from "nobody wrote down how many were requested" once a default is filled in. A `grad_accum` silently defaulted to 1 makes every derived sample count wrong by the real factor while the arithmetic stays sound. |
| A skill writes only step keys the `run.json` template defines. | Resume skips completed steps by reading them back; an undefined key is never recognized as completed and re-runs forever. This is how `check_sources` vs `resolve_assets` survived unnoticed. |
| An axis probe that could not run is `unverifiable`, never `intact`. | Same shape as the metric rule above, applied to reproduction: "the commit resolves" and "no commit was recorded" are different facts, and only the first is evidence. A probe that fails open turns every unchecked axis into a pass. |
| The overall reproduction verdict ranks `gone` above every other axis state. | Ranking by the verdict enum's own order put `unverifiable` above `gone` and reported a run whose cited data had been **deleted** as merely unverifiable — the worst state in the table, announced as the second mildest, with the `you can still` guidance never firing. |
| `reproduced` requires a measured band, every axis `intact`, and — if a probe was declared — that the probe actually ran. | Each missing piece degrades it to a different weaker claim, and all three read identically once the word "reproduced" is written down. A matching average over changed predictions is the fake-metric shape one layer above the model. |
| A repro trial is comparable to its target: same `mode`, equivalent `scope`. | A band assembled from mismatched trials is "Metric comparability" below with a measurement's authority on top — nothing errors, nothing is missing, and the noise estimate every later verdict rests on is of the wrong quantity. |
| When two records disagree, neither is overwritten: both are marked, the pair is recorded, and adjudication waits for a person. | The two available moves without it both destroy the record — rewrite the older one (losing exactly what the next round reads) or stay silent — and the third state is the true one. A record that quietly resolves its own contradictions reads, afterwards, exactly like a record that never had one. Its companion rule: **a cheaper measurement may not refute a dearer one.** Most apparent contradictions between a short run and a controlled one are not disagreements but incomparabilities, and adjudicating one as a disagreement is how a good result is discarded by a cheap probe. Enforced in `/explore` (`graph.py dispute`). |
| A number transcribed into a record carries the line it was read from, verbatim — `{value, sources:[{ref, quote, kind}]}` — or says `pending`. | "Never record a metric you did not read" is a prohibition, and a prohibition leaves **no trace when broken**: a value typed from memory and a value read off a log are the same JSON, and the memory one is more likely to be round and plausible. The quote is what makes the difference visible, and mechanically so — a quote that does not contain the number's significant digits proves the source was not open. `kind` separates a value you **set** from one a run **produced**, the same line `workload` and `scope` draw one level up; citing a measured outcome to the config meant to produce it is the same error in a smaller place. Enforced today only in `/explore`'s records (`graph.py check`), which is where numbers currently get transcribed by hand. |
| A **production** run launches only once `provenance.json` has no `blocking` / `guessed` / `unverified` entry left — or the launch stamps the waiver into `run.json`. Debug always clears. | `provenance.json` is the only record of which values were *read* and which were *inferred*, and it was built with no reader: its own template says *"Nothing here is required for a run to launch"* and `/train-init` says *"Not read by `/train-run`"*. So a production run on a guessed `primary_metric` writes a `run.json` stating that metric as fact — then `/conclude` cites it and `baseline_delta` subtracts it, each of them correct about a value nobody ever read, with nothing raising anywhere. Debug is exactly where a guess belongs, which is why the gate is the boundary between finding out whether it runs and putting a number into the record. ‼️ `absent` is a **conclusion**, not a gap, and must never block — treating it as one is how a correct record gets edited to make a gate pass. Enforced by `provenance_gate.py`. |
| A fine-tune records the base **measured here**, and the two measurements were taken the same way. | See "Baseline measurement" below. A fine-tune's absolute score has no scale without the base's, the base's published number was measured on another scope, and the measurement is only cheap while the run is being set up. Two measurements taken under different settings are the worse half: both exist, both look fine, and nothing downstream can tell they are not a delta. |

### Listing runs (no separate index)

There is no `runs_index.json` cache. The source of truth is the `run.json` files themselves; "list all runs" / "find comparable runs" is a scan of the run tree, run on demand. This avoids cache drift after rsync, manual run deletion, schema evolution, and concurrent updates — none of which need any code to handle when there's no cache.

**Go through `lifecycle/scripts/shared/list_runs.py`. Do not hand-write the jq.** The rule below about `mode` is a correctness rule, and a correctness rule implemented as a snippet everyone retypes gets forgotten exactly once, silently, in the query that mattered. The script's `mode` argument is keyword-only with no default, so forgetting it is a `TypeError` at the call site rather than a leaderboard with debug runs in it.

```bash
# All completed production runs in a stage
python <mlclaw_root>/lifecycle/scripts/shared/list_runs.py <project_root> \
    --stage training --mode production

# Runs comparable for /train-tune (same code SHA, not part of a session, full-scale)
python .../list_runs.py <project_root> --stage training --mode production \
    --commit "$SHA" --no-session

# Most recent N for a menu — no comparison, so mode mixing is allowed but must be named
python .../list_runs.py <project_root> --all-modes-not-comparable --sort created_at --limit 10
```

From Python: `query_comparable_runs(root, *, mode, stage=None, status="completed", session="*", code_commit=None, sort_by="created_at", descending=True, limit=None)`. Sentinels: `session="*"` doesn't filter, `session=None` means ad-hoc runs only. The `/train-tune` filter above is four clauses long (mode, status, commit, no-session) and dropping the fourth is invisible — there is no Python wrapper for it, only the CLI flags; type the four flags, don't reconstruct them from memory.

Results carry `matched`, `excluded` (runs whose `mode` is null — reported, never silently included *or* silently dropped), `filtered_out_count`, `distinct_scopes`, `comparable`, and `errors`; a malformed `run.json` becomes one error entry instead of killing the scan. **Read `comparable` before ranking anything** — false means the matched runs span more than one scope, so their metrics are not a series. At 10k runs the scan is ~200 ms.

The escape hatch is named `list_all_modes_not_comparable()` on purpose: it marks its result `comparable: false` and tags every entry, so a mixed population cannot be mistaken downstream for a ranked one. Use it for menus, never for a delta.

### Metric comparability

`mode` is not cosmetic — filter on it in **every** query that compares, ranks, or aggregates metrics: leaderboards, baseline diffs, best-so-far curves, `/train-tune` observation, DAG renderings. A debug run's numbers are real but describe a different workload (20 images instead of 5000, 1 epoch instead of 100), so mixing them in produces a **fake comparison**: nothing errors, no data is missing, and a wrong conclusion gets drawn from correctly-recorded numbers. Two rules:

- Compare only across runs with the same `mode` **and** equivalent `scope`. Two production runs over different sample counts aren't comparable either.
- When you deliberately want debug runs ("did my last smoke test pass?"), select `mode == "debug"` explicitly rather than dropping the filter — an absent filter reads as a bug to the next person.

A run that produced metrics with `mode: null` is unusable for comparison and should be reported as such, not silently included.

Each run tracks two types of relationships in `run.json → lineage`:

```
lineage:
  parents:             ["training/run_20260315_120000"]   ← I consume their output artifact
  fork_of:             "evaluation/run_20260317_091500"   ← I copied their config to start
  variation_summary:   "lr: 1e-4 → 2e-4; warmup: 0 → 0.03"  ← auto-derived diff vs fork_of
  session:             "20260428_120000_lr_search"        ← optional, set by /train-tune
```

- **`parents`** (cross-stage, hard dependency): this run consumes artifacts produced by those runs (e.g., eval consumes train ckpt). Base's artifact must exist for this run to be reproducible. Drawn as solid arrow across stage columns.
- **`fork_of`** (same-stage, metadata only): this run started from that run's config, with modifications. **No I/O dependency** — fork is reproducible even if base is deleted. Drawn as dashed arrow within the same stage column.
- **`variation_summary`** (auto-derived, optional): short human-readable diff of `runtime_params` vs the `fork_of` base, e.g., `"lr: 1e-4 → 2e-4; warmup_ratio: 0 → 0.03"`. Filled by the run skill at create time. Null when `fork_of` is null. Saves `/train-compare` and DAG renderers from re-diffing snapshots.
- **`session`** (optional): when this run is part of a `/train-tune` HPO session, this field holds the session ID (matches `tune_sessions/<id>/` directory). Null for ad-hoc runs. `/train-tune-report` filters runs by this field to render a single session's chain.md without scanning all project runs.
- **`repro_of`** (optional, cross-stage): `<stage>/<run_id>` of the run this one is trying to reproduce. Set by `/repro` on each trial. **Deliberately none of the three above.** Not `fork_of`, because a fork intends to differ and carries a `variation_summary` of what it changed, whereas a trial intends to be *identical* and its empty diff is the entire point — conflating them makes every reproduction read as an experiment. Not `parents`, because a trial consumes no artifact of its target; it re-measures the same quantity. Not `session`, which is `/train-tune`'s. A run skill launched as a trial fills this and otherwise leaves it null.

**A repro trial is only evidence if it is comparable to its target** — same `mode`, equivalent `scope`, per the rules above. `repro.py trial` refuses a mismatch, and that refusal is load-bearing rather than fussy: the band every later verdict rests on is a noise estimate, and one assembled from a debug trial and a production target is an estimate of the wrong quantity carrying a measurement's authority.

```
     training           evaluation
     train_run_1  ──→   eval_run_1
                  ──→   eval_run_2  (fork_of: eval_run_1, changed threshold)
                  ──→   eval_run_3  (fork_of: eval_run_1, changed dataset)

     train_run_2  ──→   eval_run_4  (fresh, not a fork)
```

**Fork behavior**: when `fork_of` is set, the run skill loads the base run's `config_snapshot.json`, `sources.json`, and `lineage.parents` as starting point. User only changes what they want. Assets that haven't changed are reused (Step 1 can be skipped). Fork inherits the base run's `lineage.parents` — if the user changes the model artifact (not just params), parents should be updated accordingly.

**Continuing training / preempt recovery / fine-tuning**: there is no separate lineage field for "I extend prior training". Express it as a `fork_of` (config copy) plus loading the base's checkpoint as initial weights via `runtime_params` — and add the base to `parents` since the new run consumes its ckpt. The reasoning ("why continue") lives in the run's `description` / `hypothesis` field, or in `decisions.jsonl` when running `/train-tune`.

## Optional narrative fields

Two top-level run.json fields exist purely to enrich human-readable reports. **Both are optional, default null, and tools must not require them.**

- **`hypothesis`** (set at run creation): a one-sentence expectation, e.g., `"Higher lr with warmup should reach lower val_loss faster."` Skills may prompt for it but should never block on it.
- **`outcome`** (set at run completion): a free-text retrospective, e.g., `"Refuted. val_loss 0.234 → 0.241 (+3%), convergence epoch 87 → 92."` Agents fill this when finalizing; users may also write it manually.

- **`verifies`** (optional, additive, default null): the structured sibling that says which
  claim the sentence is about and what would refute it — `{card, criterion, falsified_if}`.
  `card` points into whichever record holds the claim (today an `/explore` graph,
  `stages/exploration/graph.json#N05`) or is the literal `[pending]`.

  ‼️ **`falsified_if` is the field.** A hypothesis nothing can refute is a wish, and a wish
  passes every run. It must name a number or the criterion's own metric — *"if it does not
  help"* is a tautology. Both halves are checked by `graph.py check`: the criterion is
  executable, and the card names this run back, because **a one-way pointer reads exactly like
  a binding** until somebody follows it.

  The three fields above stay optional and stay strings, so no existing run record is
  invalidated. That is the whole reason `verifies` is a sibling rather than a richer
  `hypothesis`: this file records what a run WAS in 26 structured fields and what it MEANT in
  three nullable ones, and making the three required would retire the archive to fix the
  present.

When both are present, `/train-compare` weaves them into the narrative ("hypothesis was X; outcome confirmed/refuted"). When absent, comparisons fall back to pure metric deltas — both should remain valid.

## Environment Resolution

All run skills (infer-run, eval-run, refactor-run, future train-run) need a Python environment to execute code.

**One project, one env** — prefer a single shared environment per project. Different stages in the same project usually share the same codebase (or refactored version of it), so their dependencies overlap. Maintaining separate envs per stage is unnecessary overhead.

Environment resolution:

1. **Check `project.json → env_name`** (project-level). If set, use it for all stages.

2. **If empty, look for an existing env**:
   - Refactor stage has a verified env (`plan.json → env.env_name`)? → promote it to project-level: set `project.json → env_name`, reuse for all stages.
   - No refactor env? → create a new one (see below).

3. **Create project env**: use the env manager from `{WORKSPACE}/resources.json → local.env_manager`:
   - Env name: `mlclaw_{project_name}` (e.g., `mlclaw_detection`)
   - Install from: `requirements.txt` or `setup.py` in the stage's code directory
   - Record in `project.json → env_name`
   - If a later stage needs extra packages, install them into the same env — don't create a new one.

4. **Stage-level override** (rare): if a stage truly has conflicting dependencies (e.g., inference needs TensorRT but training doesn't), set `project.json → stages.{stage}.env_name` to override. This is the exception, not the norm.

5. **Remote execution**: server's `python_path` in `resources.json → servers.{key}` takes precedence. Local env resolution doesn't apply.

**Env manager** is read from `{WORKSPACE}/resources.json → local.env_manager.tool` (pixi/mamba/conda/uv). If empty, invoke `/resources` to detect it. **Prefer `pixi` when it is present**: it is the one that pins a lockfile per project directory, so the env a run resolves is reconstructible from the repo rather than from a named env somebody has to still have — which is the difference between a run whose env can be rebuilt and one whose env is a machine's local state. This matters directly to `/repro`'s env axis.

Run skills use `{run_in_env}` as shorthand for the activation command:
- pixi: `pixi run --manifest-path {project_dir}/pixi.toml` — the env is keyed to the directory and its lockfile, not to a global name
- mamba/conda: `mamba run -n {env_name}` or `conda run -n {env_name}`
- uv/venv: `source {venv_path}/bin/activate &&` (Linux) or `{venv_path}/Scripts/activate &&` (Windows)

## Path Mapping (Cross-Machine Execution)

When executing code on a remote server, local MLClaw paths must be mapped to remote paths. Each compute resource (server or local) in `{WORKSPACE}/resources.json` has:

- `mlclaw_root`: the MLClaw workspace root on that machine (e.g., `/home/ubuntu/agent_space/mlclaw`)
- `python_path`: the Python executable path on that machine (e.g., `/home/ubuntu/miniconda3/envs/ml/bin/python`)

Path mapping rule:
```
local:  {local mlclaw_root}/projects/detection/stages/evaluation/...
remote: {server mlclaw_root}/projects/detection/stages/evaluation/...
```

The project-relative path stays the same; only the root prefix changes. Run skills sync only necessary files to the remote `mlclaw_root` before execution.
