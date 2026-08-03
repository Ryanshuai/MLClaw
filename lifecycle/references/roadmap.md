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

What a release pins: the checkpoint, the eval numbers **with their `scope`**, the env, and the
snapshot it trained on. Then `retention.py plan` grows a `cited_by_release` exclusion exactly like
`retire.py plan`'s `cited_by_snapshot`.

**Three records, one prerequisite, and conflating any two is a known expensive failure:**

| Record | Answers | Shape |
|---|---|---|
| better | is the new one an improvement | a *judgment*: new vs old on **one fixed measurement** |
| approved | who cleared it, superseding what | a *`decision`* — `/ask-human` already owns this |
| serving | what is answering requests, and what was on the day it broke | a *binding*: artifact ↔ environment ↔ time interval, with history |

Reading "better" as "serving" gives the classic bug: the leaderboard says C is best, everyone
believes C is live, B was deployed and C never shipped. Reading "serving" as "better" leaves a
rollback decision nothing to consult.

And a trap the loop springs the moment it turns twice: the new model is evaluated on `boxes@v2`, the
old one on `boxes@v1`, and the two numbers **look comparable and are not** — CLAUDE.md's own *never
compare across non-equivalent `scope`*. Cross-generation comparison must be re-measured on one fixed
set, which needs a record of *which* set is this model line's standing exam. That record is part of
this layer.

## Deployment stage — `/deploy-init` + `/deploy-run`

**The gap that keeps the long loop open.** Blocked on the identity layer above: a deployment whose
subject is `/srv/models/best.pt` stops being true the first time somebody scp's over it.

What it owns: the *binding* — this release, on this environment, from this instant — and its
**history**, because the question asked in an incident is "what was serving at 03:00 on the 12th",
which a current-value pointer cannot answer.

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
