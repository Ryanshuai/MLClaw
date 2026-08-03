---
name: eval-triage
description: >
  Work out what a model's bad cases actually are, and send each to the party that can fix it. Ranks
  an eval run's worst per-sample scores, has an agent look at each one, has a person confirm or
  overrule that, then splits the findings three ways — wrong label, genuinely hard sample, model
  problem — because those have three different owners and treating them as one makes two of them
  worse. Trigger for: finding out why a model is failing, looking at the worst predictions,
  analyzing errors after an eval, deciding whether to add data or fix labels or change training, and
  sending mislabeled samples back for re-annotation. Also trigger for Chinese requests like
  "看看错在哪", "分析一下 bad case", "为什么这个类别效果差", "哪些是标注错了", "错例分析",
  "把标错的挑出来重标", "该加数据还是改模型". Not for rendering a report of an eval (use
  /eval-report) and not for finding what the eval set never covered (use /data-drift).
---

# /eval-triage — what a bad case is, and whose it is

An aggregate metric says the model got worse. Per-sample records say *where*. Neither says **why**, and the why decides everything — the same 40 images at the bottom of an eval hold three different problems with three different owners:

| Verdict | Owner | Acting on it |
|---|---|---|
| `label_wrong` | `/data-label` | a rework round. Fix the annotation at the source |
| `sample_hard` | the data line | the only pile that legitimately becomes more data |
| `model_wrong` | `param_injection` + the training config | more data does not fix this; **never leaves the model line** |

Route all three as one pile and two get worse. That is the whole reason this is a skill and not a section in `/eval-report`: **rendering is not analysis.** A report can sort by loss and show you 40 images; deciding what each one *is* requires looking at it, and that is the only part that needs an agent.

## Two things that look like success and are not

**1. The ranking is not the answer.** Sorting worst-first surfaces **label errors before hard examples**: a mislabeled box is a target the model cannot satisfy, so its loss stays at the top permanently. Feed that pile back as "hard cases to add" and you have amplified the annotation noise that produced it — which raises those losses further, which selects more of them next round. `route` refuses to put a `label_wrong` unit in the hard-example pile at all, and writes the exclusion down as `not_hard_examples` so the next reader can see which list is not the one they came for.

**2. Looking is a claim — and the agent is not exempt.** CLAUDE.md "Never let somebody's word become a checked fact" does not carve out the agent: one model's judgement is one source. So `provenance` is **derived, never passed** — there is no flag that writes `verified`. It takes two judgements from two *different kinds* of source agreeing. Two agent passes over the same image are one source sampled twice; their agreement measures the model's consistency, not the label, and stays a `claim` saying exactly that in `caveats`.

## What it is blind to, and why that is not a defect

The eval set was cut from the training distribution, so **no sample from a region the training data never covered can appear in this ranking, however deep it goes.** That failure is `/data-drift`'s. The two are complements and neither can be the other's alarm: drift is blind to what the training set covers but the model never learned; this is blind to what drift finds. Every record written here carries `blind_to` saying so.

## The script

```bash
S=<mlclaw_root>/lifecycle/scripts/eval-triage/triage.py

python $S rank    --project <p> --run <eval run_id> [--limit 40] [--name <slug>]
python $S judge   --project <p> --run <r> --session <sid> --unit <u> \
                  --verdict label_wrong|sample_hard|model_wrong|unclear \
                  --by agent|human|gold --basis "..."
python $S confirm --project <p> --run <r> --session <sid> --unit <u> \
                  (--agree | --disagree --verdict <v>) [--by human] --basis "..."
python $S route   --project <p> --run <r> --session <sid> [--partial]
python $S status  --project <p> [--open-only]
```

Exit 2 = broke, do it by hand. **Exit 1 = worked, the answer is no** — every exit-1 here guards either the claim/verified boundary or the amplification bug, so redoing it by hand overrides the check.

## Step 1 — `rank`, and the refusals before it

`rank` reads `stages/evaluation/output.json -> per_sample`. Four refusals, and the first is the common one on a project that has never triaged:

| Refusal | Why |
|---|---|
| `per_sample.path` not declared | Nothing to rank. **A legitimate state, not a broken one** — most eval code writes such a file and MLClaw never used to ask. Route to `/eval-init` Step 1c |
| `score.direction` missing | Which end means bad is never inferred from a field name. Sort the wrong way and the pile is the model's *best* predictions, reviewed as its worst — nothing errors and the review reads normally |
| the declared file is not on disk | Declared-and-absent ≠ never-written. Same rule as "never record a metric you did not read": collapsing them makes an extraction failure read as a model with no bad cases |
| the run is not `completed` | A partial per-sample file ranks a truncated pass, and nothing downstream would say so |

Then it resolves each sample id against the manifest of the snapshot the run cited, and records `resolved` / `unresolved` / **`unverifiable`** (no snapshot cited — nothing could confirm the mapping either way, and this must not read as resolved).

Default `--limit` is 40. This is a person looking at images, not a batch job.

## Step 2 — `judge`: the agent looks

For each case: open the image and whatever `per_sample.fields` carried across (prediction, ground truth, class, any overlay the eval code already rendered), and decide which of the four it is. Read `references/verdicts.md` for what separates them — particularly `sample_hard` from `model_wrong`, which is the judgement call this skill exists to make and the one nothing can check afterwards.

`--basis` is required. A verdict with no basis cannot be re-examined by whoever acts on it, and "the label looks wrong" is not a basis — *"the box covers two cartons, GT has one"* is.

## Step 3 — `confirm`: a person agrees or overrules

This is the step that makes a finding `verified`, and the only one that can.

- **`--agree`** → two kinds of source agree → `verified`.
- **`--disagree --verdict <v>`** → the person's call stands (they outrank the agent about what is in an image), provenance is `claim`, and **the agent's rejected verdict is kept in `caveats`.** That retention is not bookkeeping: whether the agent's calls can be trusted on *this* dataset is answerable only from how often a person overruled it, and that is computable only if the overrules survive.
- Two sources of **equal** authority disagreeing → `disputed`, no standing verdict, and `route` refuses it. There is no tie-break that is not a coin flip, and a coin flip written into the record reads afterwards as a finding.

Confirming every case is not always worth it. Confirm a sample first — if the agent's `label_wrong` calls hold up on ten, the rest are a `claim` you can act on knowingly, and the record says which ones a person actually saw.

## Step 4 — `route`: three piles, and what it refuses

`route` refuses while anything is `unreviewed` (that is the amplification bug itself) or `disputed`. For the two piles that **leave the model line**, it also refuses cases whose unit id resolved to nothing — a finding sent to a labeling party has to name something their side can look up, and `--allow-unaddressable` exists for when free text really is the best available. `model_wrong` needs no manifest: it is acted on by editing a config in this repo.

Then hand each pile to its owner:

- **`label_wrong` → `/data-label`**, a rework round against the units. The handoff freezes a manifest at send time, so completeness comes back checked rather than claimed.
- **`sample_hard` → the data line.** `/data-label` with `kind: data_request` when somebody has to go and capture more, `/data-collect` when it already exists somewhere.
- **`model_wrong` → the training config.** Start at `config.json -> param_injection` and the `evidence` line behind the relevant param. This pile never becomes a data request.
- **`unclear`** is not routed. It is reported, because a pile of unclears is itself a finding about the review — usually that the reviewer could not see enough, which is a `per_sample.fields` problem.

## Requires / suggests

- **Requires**: an evaluation run with `status: "completed"`, and `stages/evaluation/output.json -> per_sample.path` non-null. Missing the second is `/eval-init` Step 1c — offer it rather than ranking something invented.
- **Suggests**: whatever the piles named. `/data-label` on `label_wrong`; the data line on `sample_hard`; `/train-init` or a `/train-run` fork on `model_wrong`. **Never `/eval-run` "to try again"** — the same rule `/repro` follows, and for the same reason: re-measuring is not a fix.

Per `lifecycle/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit — `stage: "evaluation"`, `execution: <eval run_id>`, `step: null` (a triage has no step chain; its state is `status`, because the work is a person looking at images and there is no process to step through).
