# Layout, paths, and reference syntax

Loaded on demand. Where things live on disk, how a stage's code directory is
resolved, how paths survive a cross-machine rsync, and how `${}` references
resolve. Needed by init skills and by anything that has to find a file; not
needed to decide what to do next, which is why it is not in CLAUDE.md.

## Workspace and tool-repo location

**Workspace** — directory holding all of this user's MLClaw projects + their shared `resources.json`. One value, lives in `workspace_root:` at the top of this file. `/project-init` rewrites that line if the user picks a different path. No registry, no priority chain — when there's actually more than one workspace on the same machine, that's the time to add structure, not before. CLI override: `/project-init --workspace <path>`.

**MLClaw repo** — auto-detected and cached in `~/.mlclaw/state.json`:
```
mlclaw_root  = $(python <repo>/lifecycle/scripts/shared/workspaces.py tool)
```
Self-bootstraps from `__file__` on first call, so skills don't need the user to pass the MLClaw path each time. Override with `workspaces.py register-tool <path>` if you have multiple clones.

**Working directory** — the MLClaw repo, always. Not the ML project (that is `--project`), and the skills are not copied into `~/.claude/skills/`. The repo is what makes three things resolve, and only the third of them fails loudly:

| | |
|---|---|
| `CLAUDE.md` | loaded from the working directory and its parents. Elsewhere, the *Never silently* rules and the routing table are simply absent — and they were put in the always-loaded file for the case where nothing else is loaded |
| `lifecycle/references/*.md` | read on demand by relative path, including this file |
| `lifecycle/scripts/…` | resolved through `<mlclaw_root>`, so these are portable — enforced by `contract_docs.ScriptPathsAreResolvedNotAssumed` |

‼️ The first two fail SILENTLY, which is why this is written down rather than left to Quick Start. Skills copied to a global directory are still discovered by name and still run; what disappears is everything that decides *which* one and *what must not happen*. Nothing reports the difference.

**Path portability**: `init_project.py` rewrites any `$HOME`-relative path in `project.json` to `~/`-prefixed form (`root`, `workspace`, every `stages.<>.code_source.path`). Always `os.path.expanduser` before using these paths.

## Code Source Resolution

Each stage in `project.json` has a `code_source` block:

```json
"code_source": {
  "source": "local|github|server",
  "path": "",
  "branch": null,
  "commit": null,
  "credentials": ""
}
```

| Source | Behavior |
|--------|----------|
| `local` | Read code directly from `path` (external directory, no copy). Acts as a soft link. |
| `github` | Clone repo from `path` (URL) into `code_path`. Track `branch` and `commit`. |
| `server` | SCP code from remote `path` into `code_path`. Uses `credentials` for SSH. |
| `framework` | **There is no tree.** The code is an installed package and the entry point is its CLI; `path` holds the pinned spec (`ultralytics==8.4.40`). `code_path` stays empty. |
| `null` | Code already lives in `code_path` (manually placed). |

**`framework` is the taking-over-a-deployed-artifact case, and it is not a degenerate `null`.** The other four modes all say *where the tree came from*; this one says there is no tree to come from. It arises whenever what you inherit is a built artifact plus a framework rather than somebody's repo — a `.deb` holding a `.pt`, a released checkpoint, a model somebody handed you — and in that situation `git init` has nothing to initialize, because the code is in site-packages and owned by the package manager.

It changes the reproduction contract rather than losing it:

| | Contract | What pins it |
|---|---|---|
| a tree | `git checkout <sha> && git apply <patch>` | `run.json → code.origin_commit` + the dirty patch |
| a framework | `<env_manager> install <pkg>==<version>` | `run.json → code.framework_version`, and `code.kind` says which contract the record *is* |

Two consequences that must not be papered over:

- **The version pin cannot see a local edit — but something else can.** A monkeypatched site-packages file leaves no trace *in the pin*, where a tree would have produced a dirty patch, and `code_snapshot.py --framework` carries that as a standing warning. It is **not** a permanent limitation: pip writes a sha256 per installed file into `<dist-info>/RECORD`, so `discover.py verify-framework --spec <pkg>==<ver> --python <the run env's interpreter>` answers it offline and exactly — `as_published` / `edited` / `incomplete` / `not_installed`. `/repro`'s code axis calls it, and **without the interpreter that axis is `unverifiable`, never `intact`**: a question nobody asked must not read as a question answered. The pin remains necessary and not sufficient; what changed is that the gap is now checkable instead of merely disclosed. (A `sitecustomize.py` outside the package is still invisible to this — it is not one of the package's files.)
- **Every default becomes load-bearing.** A framework CLI has on the order of a hundred arguments (`yolo val` does), and a version pin says nothing about which ones the invocation relied on. So `config.json → param_injection` and the per-run `config_snapshot.json` carry weight here that they do not carry for a repo, where the code itself records its defaults.

**The framework's own code is still code, and it is still readable — install the pin and read it.** The one thing this mode costs an init skill is that `grep`ing the code directory finds nothing, so every value has to come from somewhere else; the fix is to install `code_source.path` in the run environment and cite `site-packages/<pkg>/…:<line>` like any other source. **The environment belongs where the run will execute** (`resources.json → servers.<key>`, or `local.env_manager`) and **never in MLClaw's own `pixi.toml`** — MLClaw does not run anybody's model, and a workload's framework in this repo would break the stdlib-only default environment `contract_pixi.py` holds, for nothing. Until that read has happened, a framework default is documented and unverified; say so rather than letting it pass as read.

**Unified code-dir rule** — every skill's cwd / read path is exactly one expression:

```
code_dir = stages/<stage>/code/_source if exists else stages/<stage>/code
```

`/project-init` puts the right thing under `stages/<stage>/code/` per source mode, so all downstream skills only see the unified path:

| Source | What `/project-init` does | Effective `code_dir` |
|---|---|---|
| `local` | Symlink `stages/<stage>/code/_source → expanduser(code_source.path)`. The user's external repo stays the source of truth — edits in the IDE are visible immediately. | `code/_source` (the symlink) |
| `github` | `git clone code_source.path` into `stages/<stage>/code/`, then remove `.git` so files are tracked under project git. | `code/` (no `_source`) |
| `server` | `scp` from remote `path` into `stages/<stage>/code/`. | `code/` (no `_source`) |
| `framework` | nothing — there is no tree to place. | `code/`, empty and stays empty |
| `null` | Code was placed manually under `code/`. | `code/` (no `_source`) |

**Why a symlink (not a copy) for `local`**: ML users iterate in their own repo with their own IDE/git. Copying creates two trees and bidirectional sync friction; symlink keeps a single source of truth. The lockdown of "what code did this run actually use" is solved separately at run-time by `code_snapshot.py` (SHA + dirty patch — see Run Skill Internal Dependencies).

**rsync portability**: the symlink stores an *expanded* absolute path (filesystems don't expand `~` at read time), so it dangles after `rsync` to a new machine where `$HOME` is different. Recovery: `python lifecycle/scripts/shared/relink_sources.py [<project_root>]` reads the `~/`-portable `code_source.path` from `project.json` and recreates symlinks for all local-source stages on the current host. Idempotent.

`code_path` (project.json) is always `stages/<stage>/code` — keep it as the join target, don't reinterpret it per source.

## File Layout

**Skill directories are flat, one level, and the hyphen is the hierarchy.** `skills/<name>/` — never nested. `contract_docs.owned_by_a_skill()` is hardcoded to that one level, so a skill at `skills/data/collect/` would be classified as the `data` skill's *internal structure*, need no tree entry, and go invisible to this contract while it stayed green. The naming rule that replaces nesting: **a prefix means the skill belongs to one stage or line (`data-*`, `train-*`, `eval-*`, `infer-*`, `refactor-*`); no prefix means it is cross-cutting (`discover`, `repro`, `resources`, `lease`) or is itself the line's router (`data`).** Two names carry a hyphen that is *not* a family prefix — `ask-human` and `project-init` — which is the one real cost of encoding the tree in names.

### MLClaw repo (`<wherever the MLClaw tool repo is cloned>`, e.g. `~/code/MLClaw`) — the tool

```
pixi.toml                       ← the tool's own env: pins the interpreter, declares no package

pixi.lock                       ← tracked on purpose; it is what makes that env reconstructible

CLAUDE.md                           ← routing + the always-loaded rules
README.md                           ← what MLClaw is, and which stages are built
.claude-plugin/
  plugin.json                       ← the manifest. Plugin root is the REPO root — install copies the plugin dir, so a plugin rooted at `.claude/` ships 34 skills and none of their scripts
  marketplace.json                  ← the catalog that makes it installable: one entry, source `./`
.claude/                            ← project settings only; NOT the plugin. The two were the same directory until that bug was found
agents/                             ← plugin subagents. `triage-verifier` only: read-only by declaration, and its disagreement is a `disputed`, never an overrule
hooks/                              ← plugin hooks. `guard_destructive.py` is the only one: a PreToolUse refusal, because a skill's `allowed-tools` binds only while that skill runs
skills/                             ← one dir per skill; a skill's own `references/` is its business, not listed here
  project-init/SKILL.md             ← /project-init
  infer-init/SKILL.md               ← /infer-init
  infer-run/SKILL.md                ← /infer-run
  eval-init/SKILL.md                ← /eval-init
  eval-run/SKILL.md                 ← /eval-run
  eval-report/SKILL.md              ← /eval-report
  eval-triage/SKILL.md              ← /eval-triage (+ references/verdicts.md)
  train-init/SKILL.md               ← /train-init
  train-run/SKILL.md                ← /train-run
  train-tune/SKILL.md               ← /train-tune
  train-tune-report/SKILL.md        ← /train-tune-report
  refactor-init/SKILL.md            ← /refactor-init
  refactor-run/SKILL.md             ← /refactor-run
  refactor-report/SKILL.md          ← /refactor-report
  resources/SKILL.md                ← /resources
  lease/SKILL.md                    ← /lease
  data-label/SKILL.md               ← /data-label
  ask-human/SKILL.md                ← /ask-human
  data/SKILL.md                     ← /data
  data-collect/SKILL.md             ← /data-collect
  data-online-sample/SKILL.md       ← /data-online-sample
  data-report/SKILL.md              ← /data-report
  data-check/SKILL.md               ← /data-check
  data-audit/SKILL.md               ← /data-audit (the only skill that opens a data file)
  data-audit-report/SKILL.md        ← /data-audit-report
  data-freeze/SKILL.md              ← /data-freeze
  data-curate/SKILL.md              ← /data-curate
  data-retire/SKILL.md              ← /data-retire
  discover/SKILL.md                 ← /discover
  repro/SKILL.md                    ← /repro (+ references/axes.md, verdicts.md)
  explore/SKILL.md                  ← /explore (+ references/: experiment-graph, porting, run-card, cluster-ops, explore-or-stop, debate-roles, human-review — ported from the arch-transplant skill)
  conclude/SKILL.md                 ← /conclude
  evacuate/SKILL.md                 ← /evacuate
  ara/SKILL.md                      ← /ara
  settings.json
contracts/                          ← executable form of this file's contracts; stdlib only
  helpers.py                        ← temp dirs, real git repos, script loading by path
  contract_code_snapshot.py         ← the reproduction contract
  contract_metric_extraction.py     ← extraction failure must not look like absence
  contract_run_record.py            ← timestamps, run_id uniqueness, step-key correspondence
  contract_training_stream.py       ← metric schema, ckpt ranking, retention aborts
  contract_data_label.py            ← the frozen manifest is the only authority
  contract_ask_human.py             ← claim is the default; verified must be earned
  contract_data_census.py           ← unreachable ≠ empty; directory ≠ complete
  contract_data_collect.py          ← an unfinished transfer is a lower bound, not a success
  contract_data_phase.py            ← staleness is measured against the census, not the freeze
  contract_data_board.py            ← a partial census never renders as an inventory
  contract_data_curate.py           ← a derivation is checked against the run, or marked claimed
  contract_data_retire.py           ← only a census-listed path is deletable; the log outlives it
  contract_adaptation.py            ← a handed-back finding is not a resolved one; loading is not correctness
  contract_conclude.py              ← a belief may not outlive its evidence; the tier is the weakest one
  contract_ara.py                   ← the five layers, and a frozen belief that stopped agreeing with the record
  contract_evacuate.py              ← a file that exists is not a file that arrived; leaving one behind is a delete
  contract_docs.py                  ← this file, README, and skills/ must agree
.github/workflows/ci.yml            ← contracts + compileall + JSON parse + no-credentials gate
docs/                               ← README assets only; nothing here is read by a skill
lifecycle/
  references/
    run-mechanics.md                ← run step chain, contracts, record integrity (read on demand)
    layout.md                       ← this file (read on demand)
    skill-graph.md                  ← node hierarchy, requires/suggests, requirement checks, state protocol
    data-line.md                    ← what each data phase owns; what is deliberately not a phase
    roadmap.md                      ← designed, not built; nothing here has a script to call
  project.json                      ← project config template
  resources.json                    ← access credentials and resource definitions template
  history.json                      ← workflow state template
  run.json                          ← run record template
  scripts/
    project-init/
      init_project.py               ← create project dirs, copy templates, git init
    infer-init/
      scan_requirements.py          ← extract dependencies from code
      validate_refs.py              ← validate ${} references across JSONs
    eval-init/
      validate_ground_truth.py      ← validate GT config consistency
    eval-run/
      compare_baseline.py           ← compare metrics against baseline
    eval-triage/
      triage.py                     ← rank/judge/confirm/route; label_wrong never becomes a hard example
    train-run/
      ingest.py                     ← source → records → stream.jsonl + tb/; all adapters
      _stream.py                    ← shared stream reader; infers the record-type key
      reconcile_metrics.py          ← declared metric schema vs what the stream emits
      select_checkpoint.py          ← rank ckpts, show the jsonl values behind the pick
      retention.py                  ← plan / apply; the only irreversible operation
    lease/
      lease.py                      ← acquire / renew / release a rented host
      provider_ssh.py               ← SSH-reachable provider backend
      _common.py                    ← JSON/error conventions every provider adapter reuses
    ask-human/
      ask.py                        ← open / answer / status; an answer carries its evidential status
    data-label/
      handoff.py                    ← send / receive / close / status; the manifest is the only authority
    data-collect/
      collect.py                    ← plan / pull / status; ingest only, records what arrived
      rig.py                        ← optional provenance: rig facts + silent-change tripwires
    data-online-sample/
      online.py                     ← declare / sample / status; uniform only, a reading nobody can retake
    data-check/
      census.py                     ← scan / show / snapshot / resolve / status; three states, never two
    data-audit/
      audit_gate.py                 ← check; the ONLY script on this skill, and it is the consumer's not the auditor's — five states, and `never_audited` is not `clean`
    data-curate/
      curate.py                     ← plan / register / trace; derived_from, checked against the run
    data-retire/
      retire.py                     ← plan / apply / log; the only delete on the data line
    discover/
      discover.py                   ← sources / record / probe / report; `gone` ≠ `unreachable`
    repro/
      repro.py                      ← check/open/trial/band/attribute/close; five axes of rot
    explore/
      graph.py                      ← add/set/ready/fill/close/check/status; nine invariants, reported and never repaired. Executes nothing
    conclude/
      conclude.py                   ← new/add/evidence/set/refute/supersede/check/status/render; `status` and `tier` are computed, never written. Executes nothing
    evacuate/
      evacuate.py                   ← plan/freeze/push/verify/bundle/clearance/status; the manifest is frozen at the source, and `clearance` is what a release reads
    ara/
      ara.py                        ← build/check; the five layers and ARTIFACT.md. `check` reports where the frozen copy stopped agreeing with the live record
    adaptation/
      adapt.py                      ← open/raise/respond/round/distill/relax/close/status; the shared ledger
    data-report/
      board.py                      ← the whole line as one self-contained HTML page
    data/
      phase.py                      ← join census + handoffs + snapshots + citing runs → phase, gates
    resources/
      parse_ssh_config.py           ← parse ~/.ssh/config into server entries
    shared/                         ← the run step chain lives here, not under any one run skill
      _records.py                   ← emit/refuse/broke (the exit-code contract), UTC time, atomic json io
      _dataset_paths.py             ← dataset dir + THE definition of "the newest census"
      create_run.py                 ← create run directory + initialize run.json (UTC-offset timestamps)
      capture_env.py                ← capture ML environment snapshot
      check_deps.py                 ← compare required vs installed packages
      test_connection.py            ← test SSH/S3 connectivity
      extract_metrics.py            ← extract metrics; "could not read" never becomes null
      finalize_run.py               ← update run.json with duration/status
      code_snapshot.py              ← SHA + dirty patch incl. untracked; reproduction contract
      compare.py                    ← THE definition of "are these two values equivalent"
      list_runs.py                  ← THE run query; mode filter cannot be omitted
      relink_sources.py             ← repair local-source symlinks after a cross-host rsync
      build_dag.py                  ← build lineage DAG from all runs
      tag_lineage.py                ← tag a run + propagate up the DAG
      workspaces.py                 ← locate the MLClaw tool repo
  inference/                        ← inference stage JSON templates
    artifacts.json
    config.json
    input.json
    output.json
  evaluation/                       ← evaluation stage JSON templates
    artifacts.json
    config.json                     ← includes dataset block
    input.json                      ← includes ground_truth block
    output.json                     ← includes per_class, baseline
  exploration/                      ← architecture-search stage JSON templates
    config.json                     ← corpus, metric script, comparability fingerprint
    findings.json                   ← input A: phenomena as counts, re-measurable with one ruler
    baseline.json                   ← the noise floor and the runs it came from
    audit.json                      ← four-state audit of this repo + cost profile
    graph.json                      ← the experiment graph; `graph.py` owns it
    state.json                      ← constants / tiers / kill list — the half that expires
  training/                         ← training stage JSON templates
    config.json                     ← resources, param_injection, hazards, env snapshot
    artifacts.json                  ← data/weight candidates + preprocessing contract
    input.json                      ← dataset + preprocessing (train/serve skew guard)
    output.json                     ← streaming-metric schema, ckpt selection, done signal
    provenance.json                 ← which values were read vs guessed; sidecar, not consumed by a run
  ask-human/                        ← one question to a person, and what came back
    ask.json                        ← the answer's `kind` is the load-bearing field, not its text
  data-label/                       ← exchange record template (project-level, not a stage)
    handoff.json                    ← one exchange: frozen manifest ref, spec, rounds, coverage
  knowledge/                        ← conclusion template (project-level; a belief outlives the round that made it)
    conclusions.json                ← statement / evidence+quote / interpretation / falsifier / depends_on
  evacuate/                         ← evacuation template (project-level; one per machine being emptied)
    evacuation.json                 ← source / destination / ARA layers / frozen manifest / per-file arrival / clearance
  repro/                            ← reproduction-session template (project-level, not a stage)
    session.json                    ← target run, five-axis audit, band, trials, attribution
  eval-triage/                      ← bad-case triage template (hangs off one eval run)
    session.json                    ← ranked units, per-unit verdict + provenance, routed piles
  adaptation/                       ← data↔consumer adaptation template (project-level, spans two stages)
    session.json                    ← contract at open, two-way findings + responses, rounds, refuted
  data/                             ← data-stage record templates (project-level, not a stage)
    dataset.json                    ← identity glob, layers + kind, locations + role, completeness
    rig.json                        ← the rig's facts, each with evidence and a breaks/shifts class
    online_window.json              ← one live-stream reading: window, uniform draw, denominator
  refactor/                         ← refactor stage JSON templates
    plan.json                       ← repo analysis, module classification, paper benchmarks
    config.json                     ← benchmark entry command + params
    artifacts.json                  ← model weights for benchmark
    input.json                      ← benchmark data + ground truth
    output.json                     ← expected metrics from paper
    refactor_run.json               ← run record template (refactor-specific steps)
```

### Workspace root (`{workspace_root}`, e.g. `~/agent_space/mlclaw/projects`)

```
resources.json                      ← access credentials and resources, shared across all projects (NEVER commit)
detection/                          ← one project
another_project/                    ← another project
```

### User project (`{workspace_root}/{project_name}`, e.g. `~/agent_space/mlclaw/projects/detection`)

```
project.json                        ← project config (git tracked)
history.json                        ← workflow state + history
.gitignore
discovery/                          ← what data was found to exist, and where the claim came from.
  leads.json                          ONE living file, not a dated scan: a lead outlives an init,
                                      because access arrives weeks after a handover starts. This
                                      is the artifact you hand the next person instead of a wiki
                                      page — every path, its evidence, what was actually found,
                                      and when it was last checked
evacuations/                        ← one directory per machine emptied before it was released.
  evac_{YYYYMMDD}_{HHmmss}/           Project-level and NEVER on the box: a record kept on the disk
    evacuation.json                   it describes disappears with it, same reason `retire/` is one
    plan.jsonl                      ← every file and which ARA layer it is
    manifest.jsonl                  ← frozen AT THE SOURCE before the transfer. The only authority
                                      for what was supposed to arrive; computing completeness from
                                      what showed up is a tautology that passes every partial pull
    bundle/                         ← the readable artifact: ARTIFACT.md (ARA's PAPER.md) plus
                                      logic/ and trace/ copied in physically, so the conclusions
                                      and the ablation graph stay readable without pulling 40GB
                                      of weights back down
ara/                                ← one directory per artifact, DATED and never overwritten:
  ara_{YYYYMMDD}_{HHmmss}/            round two building over round one destroys the record of
    ARTIFACT.md                       what was believed during round one, which is what makes
    ara.json                        ← round one's runs legible. Same rule as a settled conclusion
    logic/ trace/                     not being edited. `ara.json` records what the artifact was
                                      built out of, so `check` reports where the FROZEN copy
                                      stopped agreeing with the live record — the copy people
                                      read does not update when its evidence rots
knowledge/                          ← what is now BELIEVED, as opposed to what happened. Project-level
  conclusions.json                    because a conclusion outlives the exploration that produced it and
                                      may come from an eval, a tune session or an audit just as easily.
                                      `status` and `tier` are COMPUTED from what the evidence still
                                      resolves to — a belief whose support rotted is indistinguishable,
                                      in JSON, from one whose support is intact, and that is the whole
                                      reason the file exists
  conclusions.md                    ← the rendered artifact: what somebody opens in six weeks instead
                                      of the JSON. Every conclusion prints its status and its tier
handoffs/                           ← exchanges with parties MLClaw doesn't control — cross-stage,
  handoff_{YYYYMMDD}_{HHmmss}/        so project-level rather than under any one stage
    handoff.json                    ← the record (status, spec, party, rounds, coverage)
    manifest.jsonl                  ← frozen at send: {item, hash, bytes} per line. The only
                                      authority for what a return is supposed to cover
    spec/                           ← snapshot of the guideline the other party worked from
    rounds/round_{N}/
      reconciliation.json           ← matched / missing / unexpected / ambiguous / source_drift
datasets/                           ← where the data is and what state it is in — cross-stage, so
  {dataset_id}/                       project-level. See "Dataset identity and census records"
    dataset.json                    ← the layout contract, plus `derived_from` (null = captured),
                                      plus `online` (where the LIVE counterpart arrives)
    census/census_{ts}.json         ← one scan; `complete: false` = a location did not answer
    audits/{audit_id}/              ← audit.json + audit_report.html. The census's counterpart:
                                      that one reads where the files are, this one reads what is
                                      inside them, and neither answers the other's question
    online/window_{ts}.json         ← one reading of the live stream this dataset gets compared
                                      against. Describes data that never enters the dataset, and
                                      unlike a census it can never be retaken
    snapshots/{snapshot_id}/        ← frozen unit set, cited as datasets/{id}@{snapshot_id}
    curate/plan_{ts}.json           ← a declared derivation, written before the transform runs
    retire/retire_{ts}.json         ← what was deleted, where, and what survived it. Written
                                      BEFORE the first rm — one level above what it deletes,
                                      and on a different machine from the bytes
stages/
  {stage}/                          ← same structure for each stage (inference, evaluation, ...)
    code/                           ← user's code (git tracked)
    artifacts.json                  ← filled by /{stage}-init
    config.json                     ← filled by /{stage}-init
    input.json                      ← filled by /{stage}-init
    output.json                     ← filled by /{stage}-init
    provenance.json                 ← sidecar: source_mode, sources_checked, evidence, unresolved (training only so far)
    artifacts/                      ← actual artifact files (gitignored)
    data/                           ← actual input data (gitignored)
    runs/
      run_{YYYYMMDD}_{HHmmss}/      ← one execution
        run.json                    ← run record (code, env, metrics, steps, lineage)
        sources.json                ← snapshot of sources used
        config_snapshot.json        ← frozen config
        stream.jsonl                ← normalized metric stream, MLClaw's (training only).
                                      Fixed name, not a config value. Re-derived whole,
                                      never appended to
        stream_meta.json            ← what the normalizer did: sources, group_by, what it
                                      inferred, what it discarded
        output/                     ← the code's own artifacts — ckpts, its tfevents, its logs.
                                      `output_dir` is overridden to point here
        tb/                         ← TensorBoard render target, on by default. Written from
                                      the stream when the source isn't already tfevents.
                                      Append-only, `.mlclaw` suffix, `.watermark`. Nothing
                                      reads it back and ingest refuses to
        logs/                       ← stdout/stderr logs
  refactor/                         ← refactor stage (special structure)
    original/                       ← GitHub clone, read-only reference
    code/                           ← refactored version (working copy, git tracked)
    plan.json                       ← refactoring plan + module classification
    config.json                     ← benchmark config
    artifacts.json                  ← benchmark artifacts
    input.json                      ← benchmark inputs + ground truth
    output.json                     ← benchmark expected metrics
    snapshots/                      ← module I/O snapshots from original code (for Tier 2 verification)
    runs/                           ← refactoring round executions
      run_{YYYYMMDD}_{HHmmss}/      ← one round
  exploration/                      ← architecture search (special structure — NO runs/)
    config.json                     ← entry, corpus, metric script, comparability fingerprint,
                                      where the prose design doc lives
    findings.json                   ← input A: the phenomena, as counts with denominators.
                                      Re-measured with the SAME ruler each round and diffed
    baseline.json                   ← the noise floor + the two /eval-run ids it came from
    audit.json                      ← the four-state audit of this repo, + the cost profile
    graph.json                      ← the experiment graph: cards, seven states, dependency
                                      edges. The only order-of-work truth. `graph.py` owns it
    state.json                      ← what expires: constants, tier assignments, the kill list
                                      with revival conditions. Voided by a change of weights,
                                      frame selection, metric or corpus snapshot
```

‼️ **`exploration/` has no `runs/`, and the absence is the design.** An arm is an ordinary run
under `stages/training/runs/` (or `evaluation/` for a measurement-only card), cited from a card
by `run_id`. Same boundary `/data-curate` draws for a conversion and `adaptation` for a
converter: a search that runs its own trials is a second run machinery, and it drifts from the
first. `/train-tune` is the same shape one stage over, and it stores its sessions under
`training/` for exactly this reason.

## Dataset identity and census records

Runs are identified by a timestamp MLClaw assigns. Data is not — it exists before MLClaw sees it, on machines MLClaw did not set up, so its identity has to be something already true of it.

**Path is identity.** A unit's id is its path relative to a location's root (`260725/s003`, `260714/seq2/s000`), which is what makes "the same unit on two machines" a fact by construction rather than a match to compute: a union across locations is a set union, with no key to choose and nothing to reconcile. `dataset.json -> identity.unit_glob` declares the shape of that path.

**The glob's depth is load-bearing, and getting it wrong is silent.** A rig writing `<date>/<scene>` scanned with a `*/*/*` glob yields zero units — not an error, not a warning, zero. Every count that follows is then correct about a set that excludes an entire machine. When a new rig or a new capture tool appears, its depth is the first thing to check, and a depth change is a different dataset rather than a variation of the old one.

**Layers are marker paths, relative to the unit.** A layer exists at a location iff its marker exists there. `kind` (`source` / `derived` / `human_locked`) is what makes a missing copy readable: `derived` can be recomputed, `source` cannot and its copy count is the data's survival odds, `human_locked` is recomputable in form but not in fact and never auto-overwrites.

Records live at project level, not under `stages/` — training, evaluation and export all consume the same data, so filing it under one of them would make the other two reach across:

```
datasets/
  {dataset_id}/
    dataset.json                  ← the layout contract: identity, layers, locations, completeness
    census/
      census_{YYYYMMDD}_{HHmmss}.json   ← one scan: per-location listing + the five verdicts.
                                          `complete: false` means a location did not answer and
                                          every count in it is a lower bound
    audits/
      {audit_id}/
        audit.json                ← what it judged against (layout contract, consuming code, prior
                                    version), per-section verdict, every finding WITH its file and
                                    record locator, the sample sizes, and every section that was
                                    SKIPPED. A skipped section is a recorded field, never an absent
                                    one — absent reads as passed
        audit_report.html         ← /data-audit-report's render. Beside the audit, not under a
                                    run's output/: its subject is the dataset, and it has to stay
                                    readable after every run that touched that data is gone
    snapshots/
      {snapshot_id}/
        snapshot.json             ← cite_as, source census, layer coverage, unverified units,
                                    and `data_retired` when a retirement took units it names
        manifest.jsonl            ← the frozen unit set. Pins MEMBERSHIP, not bytes
    curate/
      plan_{YYYYMMDD}_{HHmmss}.json     ← a declared derivation: frozen parents, op, output
                                          root, and the citations the run must carry
    retire/
      retire_{YYYYMMDD}_{HHmmss}.json   ← one deletion: what was ranked, what was excluded and
                                          why, what was deleted / failed / never reported back
```

**A snapshot outlives the bytes it names, so it has to be able to say when they are gone.** Deleting units a frozen snapshot lists does not break the citation — `datasets/{id}@{sid}` goes on resolving and the manifest goes on listing them. `retire.py apply` therefore appends `data_retired` to `snapshot.json` for every unit it removed, which is the only place a reader a year later can find out. `--waive cited_by_snapshot` is what permits the deletion; the stamp is what stops it from being silent.

A consuming run cites a snapshot in `run.json -> lineage.parents` as `datasets/{dataset_id}@{snapshot_id}` — never the dataset id alone, because a dataset grows and a citation that cannot say which day is not lineage. Same slot and same form as `handoffs/{handoff_id}`.

**Nothing machine-specific goes in that directory, which is why the resolved view does not.** A manifest line names location *keys*; opening a file needs `locations[].root` and `layers[].marker` joined onto the unit path, and `census.py resolve --at <loc>` performs that join into JSONL with real paths. Its output lands **beside the consuming run** (`stages/{stage}/runs/{run_id}/data_resolved.jsonl`), never under `snapshots/` — the verb refuses that path outright. A resolved path embeds one machine's root, and this is the one record that has to stay readable after the disk moves. Regeneration is snapshot + `dataset.json` + `--at`, so the run cites the snapshot and keeps the view as scratch.

## Variable Reference Syntax `${}`

Used across all config files. Resolved at runtime by `/{stage}-run`.

| Reference | Source |
|-----------|--------|
| `${project.name}` | project.json → name |
| `${project.root}` | project.json → root |
| `${resources.aws.region}` | {WORKSPACE}/resources.json → aws → region |
| `${resources.servers.xxx.host}` | {WORKSPACE}/resources.json → servers → xxx → host |
| `${artifact.xxx}` | stages/{stage}/artifacts.json → sources → xxx → path |
| `${input.xxx}` | stages/{stage}/input.json → sources → xxx → path |
| `${output.xxx}` | stages/{stage}/runs/run_NNN/output/ (resolved at runtime) |
