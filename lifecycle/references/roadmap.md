# Roadmap — designed, not built

Design reasoning for skills that do not exist yet. It lives here rather than in CLAUDE.md because
CLAUDE.md is loaded every session and none of this is actionable: there is no script to call and no
record to read. It is kept because each entry records a decision that would otherwise be re-argued
from scratch — most usefully, the traps that make the obvious implementation wrong.

Read this before starting one of them. Read `CLAUDE.md -> "Status"` for what is actually built.

## `/data-drift` — online vs training

Cross-cutting on the data line like `/data-check`, not a phase on it. It earns a skill for one
reason: **it is the only quality signal that needs no labels**, so it is the only thing that can
speak on the day a regression starts rather than after a labeling round.

**Only the comparison is outstanding — both sides it compares now exist.** `/data-freeze` pins the
reference side and **`/data-online-sample` is built**: it takes the dated, uniform reading of the
live stream, records the denominator when one exists and says it is a lower bound when it does not,
and refuses a window that is open or offset-less. What remains is `compare`, and the split is
deliberate for the reason `census.py scan` is split from `phase.py phase`: the thing that goes out
and asks machines fails for boring reasons (a prefix did not answer, a credential expired) and the
thing that renders a verdict must never fail for those. `sample` runs on a schedule; `compare` runs
when somebody asks.

**`compare` computes nothing itself.** Feature statistics over a window are a *run* — the user's
code through the ordinary run machinery, like `/data-curate`. A feature extractor built into MLClaw
would be MLClaw doing ML, against the project's first principle.

Two refusals it inherits and must enforce: a reading whose `policy` is not `uniform` is never a
window (it measures the filter, not the world — and the biased pull is `/data-collect
--cite-window`), and a reading whose `complete` is false is never compared, because a verdict
against a window with a missing day is a verdict about the outage.

Four things it has to get right.

**Its reference side is a frozen snapshot, never "the training data."** An unpinned reference makes
the number uninterpretable and moves on its own as the dataset grows.

**Its record hangs off that snapshot**, which gives `/data` a second way for a freeze to expire.
Staleness today means "inflow arrived" and cannot see "the bytes did not change but the world did".

**Input drift, prediction drift and performance drift are three facts and must not collapse into
one red light.** The first two are measured; "it therefore regressed" is a `claim` until an eval on
returned labels, and "therefore retrain" is a `decision`.

**It does not subsume bad-case reflow.** Drift is blind to samples the training set covers but the
model never learned; bad-case mining is blind to the region drift finds, because the eval set was
cut from the old distribution. Neither can serve as the other's red light.

Until a model has a citable identity, it can only name the snapshot that drifted — not the model
whose training set that was. The operator still holds that link.

Its complement, `/eval-triage`, is **built** — see its SKILL.md. Neither can be the other's alarm.

## `/train-triage` — what went wrong with the run, and whose it is

Sibling of `/eval-triage` by shape and **not** by reason. That one asks what is wrong with the
*model*; this one asks what is wrong with *this execution of a training run*. Depends on nothing —
the cheapest thing on this list to build.

**The design decision the whole skill turns on is the entry point, not the verdicts.** Failure comes
in three kinds and only one has a skill today:

| Failure | Today | |
|---|---|---|
| finished, model is not good enough | `/eval-triage` | three verdicts, three owners |
| crashed | detection only — `status: crashed` | what this entry adds |
| **finished, but the run itself was void** | nothing | the expensive one |

The third is expensive because **it is disguised as success, and the record layer does the
disguising**: it has a clean code snapshot, metrics, full lineage, `status: done`. It gets cited as a
baseline, passes `/repro` as `intact`, and enters any comparison as a peer. Gradients that never
synced, a dataloader that silently dropped a third of the set, a resume that loaded the wrong
optimizer state — all finish cleanly. A crashed run is at least honest.

And a crash *has* a trigger, so the user shows up on their own; the void run has **no trigger at
all**. So a skill the user must think to invoke covers only the case that did not need it. Hence two
verbs:

- **`inspect`** — called by `finalize` unconditionally, on every run, crashed or not. Read-only,
  no network, reads only what the run directory already holds. Emits `findings[]`.
- **`triage`** — opens a session only when `inspect` found something. Attribute and route.

**Not `screen`**: `run.json -> mode` already takes `screen` as a value (a short trial in a
`/train-tune` screen_then_refine sweep). Two meanings for one word in the same record is how a
filter silently selects the wrong runs.

**Most of this already exists as `config.json -> hazards`, and that is the right anchor to build
on.** Its `impact: degrades` is defined as *"runs and produces plausible-but-wrong results"* — the
void run, named, with train/val overlap and a silently-changed dependency default already given as
the examples. Its `kind` enum (`world_size`, `data_leakage`, `dependency_version`,
`nondeterminism`, …) is the vocabulary this skill should reuse rather than reinvent.

So `/train-triage` is not a new idea — **it is the second half of a mechanism that today only has a
first half.** Hazards are echoed by `/train-run` right before launch, on the stated grounds that it
is the only moment anyone reads them. But before launch, "this code may leak val into train" is
something the user can do nothing with; it is *after* the run that it becomes checkable. Every
`degrades` and `risks` hazard should therefore be re-asked at `finalize` as "did this one actually
happen", against evidence. That turns a warning nobody can act on into a finding with an owner.

What `inspect` checks — every one readable, none of it a question for a person: **injected params
declared vs. actually printed** (the post-hoc half of CLAUDE.md's "Never pass a param the code
ignores", which today is only checked before launch) · samples seen vs. `num_samples × epochs` ·
last_step vs. declared epochs · loss trajectory shape (never descended; NaN mid-run yet completed;
val exactly parallel to train, which means the val set is a subset of train) · declared world_size
vs. ranks appearing in the log · model-state step vs. optimizer-state step in the checkpoint ·
framework integrity, reusing `framework_integrity.py` · and on non-zero exit, a crash class.

Five owners, one more than `/eval-triage` and one of them deliberately outside: `resource` →
`/resources` + `/lease` · `data` → the data line · `env` → `/discover verify-framework` ·
`config` → `param_injection` and the training config · `code` → **the user's own training code,
which MLClaw does not take**, the same boundary zero-code-invasion holds everywhere else.

Three traps:

- **False positives kill it.** A thing that prints three yellow warnings after every finalize is
  ignored inside two weeks — the same failure mode as CLAUDE.md's unnecessary question, which
  teaches its reader to skim. Every check must produce a *definite* answer or stay silent. Prefer a
  miss over noise.
- **A clean inspection is not a correct run.** It checked the handful of things it can read. So the
  record must never say `verdict: ok`; it says `inspected: [...]` naming the checks performed —
  the same discipline as `/eval-triage`'s `lower_bound` and `blind_to`.
- **Never re-run anything to diagnose.** The pull toward "run it again and see if it still crashes"
  is strong and wrong: that is `/repro`'s, and a training re-run costs hours and real money.
  `inspect` reads existing evidence only. Intermittent faults are found by **frequency across runs**,
  not by reproduction — which is the other half of this skill and the only thing it offers over
  reading the traceback yourself. A fifth OOM in one project and a first OOM are different facts:
  the first says this batch size was too big, the fifth says the resource config is wrong. Nothing
  today can see the difference, because each one was solved in the moment and forgotten.

**A hang is out of scope, and must be said so.** A hung run never reaches `finalize`, so `inspect`
is never called on the one failure people most want caught. That belongs to heartbeat timeout in
`_stream.py`. A skill that reads as covering it would be worse than one that does not exist.

**The record side is done — `run.json -> workload` landed ahead of this skill.** It was the blocker:
the top level held `last_step`, `metrics`, `scope`, `env`, `code`, `error` and no `world_size`, no
batch size, no epoch count, so half the checks above had nothing to compare against. Filled at
launch from the same `param_injection` entries the command is built from (Launch contract rule 4),
with nulls that stay null, and `contracts/contract_run_record.py -> Workload` holding that line.

So the two sides `inspect` needs both exist now: **`workload` is what the run was asked to do,
`scope` is what it actually reached** — /train-run already reads a debug run's real epochs and batch
size back out of the log. `inspect` is the comparison between them, and it was not writable before
because only one side was ever recorded.

Still to build: the comparison itself, the crash classifier, the hazard re-ask, and the cross-run
frequency verb. Nothing in that list is blocked on a record any more.

## Identity — what `<id>@<pin>` is for, and what it is not

Read before `models/` below: that section is an *instance* of this one, and building it without
this produces a fifth hand-written citation parser.

**The system already exists, in two syntaxes.** `candidates.location` (`dataset:<id>@<snapshot>`,
`handoff:<handoff_id>`, `run:<stage>/<run_id>`) and `lineage.parents` (`datasets/<id>@<sid>`,
`handoffs/<hid>`, `<stage>/<run_id>`). Adding a citable kind means adding a row to **both**, never
inventing a third form. The `:` / `/` divergence between them is not worth unifying: the fix would
have to rewrite run records that cannot be rewritten.

**Three directions, and the middle one is why the forms are formal at all:**

| | Who asks | When | Cost of failure |
|---|---|---|---|
| back — where did this come from | a person | afterwards | you do not know |
| **forward — who still cites this** | a script | **before an `rm`** | **you already deleted it** |
| same — are these still the bytes | a hash | either | the other two are confidently wrong |

Only the first is served by prose, which is why "trained on July's boxes" is survivable and a
missing reverse edge is not. `retention.py`, `retire.py plan` and deploy all ask the second before
acting; nobody can ask it by hand, because it means walking every record in the project.

**The reverse direction is computed, never stored.** `data-label/handoff.json` already settles this:
`consumed_by` is advisory *and may lag*, and the authoritative direction is the consuming record's
own `lineage.parents`. A stored reverse edge goes stale the moment a third record cites the same
parent and nothing walks back to update it.

**Two kinds, and the `@` is what marks which:**

- **event** — happened at an instant; id assigned by MLClaw, timestamp-shaped, never named by a
  human; cited as `<plural>/<id>`. It splits again by failure mode: those born finished (census,
  online window) go **stale**, those with a duration (run, lease, handoff, ask) **dangle** — which
  is what three of the four "On Conversation Start" checks are looking for. An event's id is minted
  at birth and does **not** change as its content churns; a run has to stay citable while it runs.
- **entity** — persists and is revised; id declared by a human because it has to mean something to
  one; cited **only** as `<plural>/<name>@<pin>`, never bare. The pin is cut when somebody needs to
  cite it, not on a schedule — so an entity has no history *between* pins, and two pins must never
  be interpolated into one.

**What must never get an id at all**: `unreachable`, `absent`, `unverifiable`. Minting one for a
non-observation is exactly how "could not look" becomes "looked, nothing there" — the failure
`census.py -> complete: false` and the `match` enum's `unreachable` exist to prevent. `/discover`'s
lead is the legal form: **the id belongs to the claim, not to the data it claims.**

**Do not build a general identity layer before `models/`.** The generalization is real and so is the
debt — one shared `parse` for the five readers that hand-roll it today (`build_dag.py` accepts a
`{stage, run_id}` dict form nothing else does), a `_comment_parents` in `run.json` so the one
load-bearing field stops being the only one in `lineage` with no declared form, and `build_dag`
walking every record kind rather than only runs, so a `datasets/…@…` parent is a node instead of an
edge into empty space. All worth doing; none of it blocks. Build `models/` under the one borrowed
constraint — **do not invent a third citation syntax** — and fold `cited_by_release` in as the
shared reverse-edge helper it is the fourth hand-written copy of.

## `models/<id>@<release>` — the model identity layer

**Build this first.** It is not a skill; it is the missing primitive three separate records need, and
without it each can only name a file path.

The data side has this and the model side does not. Data gets `identity` → census → a citable
`datasets/<id>@<snapshot>` → `retire.py plan`, which excludes units a live snapshot still cites.
A model gets one path in `run_dir/outputs.best_checkpoint` plus `retention.py`, which ranks by metric
and — in its own words at `.claude/skills/repro/references/axes.md` — **"has no idea who cited
them."** So `/eval-init` cites `run:training/<run_id>`: an edge to a *run*, not to an artifact.
Retention then deletes correctly by its own lights and the eval's parent dangles, which `/repro`
reports afterwards as `not_reproducible`. The ten "Never silently" rules include *never delete data a
frozen snapshot still names*; there is no model counterpart, not because it does not apply but
because there is no frozen thing to name.

What a release pins: **the checkpoint's content hash**, the eval numbers **with their `scope`**, the
env, and the snapshot it trained on. Then `retention.py plan` grows a `cited_by_release` exclusion
exactly like `retire.py plan`'s `cited_by_snapshot`.

**The hash is the load-bearing one, and pinning a path instead reproduces one level up the exact bug
this layer exists to fix** — a release that names a location rather than bytes gets the deployment
failure named below, where `/srv/models/best.pt` stops being true the first time somebody scp's over
it. Note that this *inverts* the data side rather than copying it: a snapshot deliberately pins
membership and **not** bytes, because hashing a multi-terabyte tree trades a real answer for one
nobody would wait for. A checkpoint is one file, so here the trade runs the other way and the hash is
affordable. It is also the only thing that makes an inherited model's `verified` distinguishable from
its `claimed`.

**Three records, one prerequisite, and conflating any two is a known expensive failure:**

| Record | Answers | Shape |
|---|---|---|
| better | is the new one an improvement | a *judgment*: new vs old on **one fixed measurement** |
| approved | who cleared it, superseding what | a *`decision`* — `/ask-human` already owns this |
| serving | what is answering requests, and what was on the day it broke | a *binding*: artifact ↔ environment ↔ time interval, with history |

Reading "better" as "serving" gives the classic bug: the leaderboard says C is best, everyone
believes C is live, B was deployed and C never shipped. Reading "serving" as "better" leaves a
rollback decision nothing to consult.

**But only one of the three is this layer's to build.** `better` is computed and never stored — the
classic bug above is a *stored* leaderboard, and storing a judgment is precisely what lets it go on
asserting after the measurement it summarized has moved. It is a verb: refuse across a different exam
or non-equivalent `scope` (through `shared/compare.py`, already the one definition of equivalence)
and recompute. `approved` needs no new machinery either — an `/ask-human` `decision` that the release
cites by id. So this layer builds `serving`'s **subject**, and points the other two at things that
already exist.

And a trap the loop springs the moment it turns twice: the new model is evaluated on `boxes@v2`, the
old one on `boxes@v1`, and the two numbers **look comparable and are not** — CLAUDE.md's own *never
compare across non-equivalent `scope`*. Cross-generation comparison must be re-measured on one fixed
set, which needs a record of *which* set is this model line's standing exam. That record is part of
this layer.

So `model.json` — the entity-side record, sibling of `dataset.json` — carries two fields, and neither
is a fact about any one artifact: the **standing exam** (what makes two releases' numbers
subtractable) and the **interface contract** (what an artifact must accept and produce to fill this
slot). Every fact lives in `release.json`, which is close to already written: `artifacts.json ->
items.<name>.origin` carries `metrics` / `scope` / `confidence: verified | claimed | asked` /
`source` today, and needs the hash and an id to become citable instead of buried under one item name.
The exam is a dated *list*, not a value — an exam set gets retired and the world shifts — and a
comparison spanning an exam change is refused, not silently taken.

**The interface contract also settles where an export goes, and not the obvious way.** An `.onnx` or
`.engine` has a different runtime interface, so it cannot fill the slot its parent fills: it is a
**separate line** with a `derived_from` edge, not a `kind: derived` release under the same id. That
is what keeps deploy answerable — the edge fleet and the cloud bind different slots, and asking one
"what is serving" must never return the other's answer.

## Deployment stage — `/deploy-init` + `/deploy-run`

**The gap that keeps the long loop open.** Blocked on the identity layer above: a deployment whose
subject is `/srv/models/best.pt` stops being true the first time somebody scp's over it.

What it owns: the *binding* — this release, on this environment, from this instant — and its
**history**, because the question asked in an incident is "what was serving at 03:00 on the 12th",
which a current-value pointer cannot answer.

**It is not a second identity layer. It is the forward direction of a release's, plus a duration.** A
deployment is a record that *cites* `models/<id>@<release>`, and "what is serving" is the reverse-edge
query on that release — so three of the four things it needs are borrowed rather than designed:

| what deploy needs | borrowed from |
|---|---|
| the subject | `models/<id>@<release>` and its hash |
| the state of one binding | `/discover`'s `claim` / `verified` / `gone` / `unreachable` |
| what a fleet is running | `census.py`'s scan: partial results, `complete: false`, unreachable ≠ absent |

Only the interval is its own, and the reason it needs the other three is that **its citation is the
only live one in MLClaw**. Every other edge is past tense: a run consumed a snapshot and finished,
and that edge can never afterwards become false. A binding goes on being true — or quietly stops —
while nobody is watching. So a binding is a `claim` until something re-reads the bytes on the target
and matches them against the release's hash, which is CLAUDE.md's *never let somebody's word become a
checked fact* applied to a machine's word about itself. And a binding that is never closed is a
dangling event in the sense of "Identity" above: `to: null` forever means nobody knows whether it was
ever taken out of service.

One thing it owns that is easy to miss: **where the served inputs land.** That value is
`dataset.json -> online.resource`, so `/data-online-sample` can read the stream this deployment
produces. Deploy and the drift loop join there, and nowhere else.

Two things it must not own:

- **The serving stack.** MLClaw does not run anybody's inference server — zero code invasion, the
  same boundary `/data-curate` holds by not converting bytes.
- **The exported model's metrics.** An ONNX or int8 artifact is a *derivation*, and its numbers are
  not the source checkpoint's — inheriting them is where fp16/int8 accuracy loss disappears. That is
  the model-curate gap (below), and deploy must refuse to carry a metric across a conversion it did
  not measure.

Edge and cloud differ in one way that matters to the record and not to the verb: an edge fleet has
*many* simultaneous bindings at different versions, so "what is serving" is a distribution, not a
value. A schema that assumes one binding per environment will not survive the first staged rollout.

## Also outstanding

- **Model curate** — export and quantization (ONNX / TensorRT / int8). Structurally identical to
  `/data-curate`: derive a new artifact from a frozen one, record `derived_from` checked against the
  run that made it. Today `.onnx` and `.engine` appear only as *inputs* to `/infer-init` — MLClaw can
  consume one and has no record of where it came from. Its one hard refusal: **an exported model
  never inherits the source model's metrics.**
- **Exploration stage** — architecture search.
- **`/train-compare`** — side-by-side metrics / params / env diff across runs.
- **Data quality checks + format conversion.** Curate *records* a conversion and the census reads no
  file content; neither performs one.
