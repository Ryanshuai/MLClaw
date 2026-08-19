---
name: data-online-sample
description: >
  Take a dated reading of the live input stream — what production was actually seeing between two
  instants — so it can be compared against the frozen snapshot a model trained on. Uniform draw
  only, records the denominator when one exists and says it is a lower bound when it does not.
  Trigger for: sampling production or online data, taking a reading of what the fleet is seeing now,
  getting the production side of a drift comparison, and checking what readings exist. Also trigger
  for Chinese requests like "线上采样", "采一下线上数据", "线上现在跑的数据什么样", "取一段线上窗口",
  "线上和训练集比一下" (this skill is the online half; the comparison is /data-drift). Not for
  pulling bytes in for retraining (use /data-collect, which cites a reading from here as its
  denominator) and not for scanning what is on disk (use /data-check).
---

# /data-online-sample — the production side of a comparison

`/data-freeze` already pins the reference side: `datasets/<id>@<snap>` is exactly which units trained the model, citable and reproducible. **Every drift tool in existence fakes that half** with a CSV somebody exported once. Here it is real — and this skill is the half that was missing.

A reading is a dated observation like a census, with one difference that decides how it is treated: **a census can always be retaken and a reading never can.** Re-scan a disk next month and you get a different answer, both true of their date. Re-read a window next month and you get nothing — the traffic rolled off, and the model that was answering has been replaced.

## The policy is fixed, and that is the whole design

Sampling from production serves two purposes with **opposite** policies:

| | this skill | `/data-collect` |
|---|---|---|
| for | drift detection | retraining |
| policy | **uniform, always** | **biased on purpose** — low-confidence, rule-flagged, complained-about |
| moves bytes | no | yes |
| needs labels | no | yes, that is the cost |
| wrong policy costs | **a meaningless number** — you measured your filter, not the world | wasted annotation budget on easy frames |

So there is no policy flag here. And the two chains connect in one direction: **`/data-collect` cites a reading from here as its denominator**, because without one your 500 hard frames came out of an unknown number and the bias is not computable.

## It declares rather than assumes

Nothing here knows that production data is date-partitioned, that a unit is a directory, or that ids look like anything. Those are facts about one business, and a script that assumed them would work at one company. So:

- **the window → locations expansion is declared** — `strftime`, `flat`, or `external`
- **the unit identity is reused** from `dataset.json -> identity`, not restated. The online and offline sides must count the same unit or the comparison is between two different questions
- **`--units-from` is a first-class input**, not a fallback. Whatever the layout, your own tooling can enumerate it and this records the result — and records that it did, because with an external enumeration nothing here looked and reachability is somebody else's claim

## The script

```bash
S=<mlclaw_root>/scripts/data-online-sample/online.py

python $S declare --project <p> --dataset <d> --resource <r> --kind server|s3|local \
                  --partition strftime|flat|external [--pattern 'inputs/%Y/%m/%d'] \
                  [--resolves-to unit_id|basename] [--unit-depth 1]
python $S sample  --project <p> --dataset <d> --from <iso±offset> --to <iso±offset> \
                  [--n 500] [--seed S] [--units-from FILE] \
                  [--population N --population-source "..."] [--scored-by ...] \
                  [--retain-until <iso>]
python $S status  --project <p> [--dataset <d>]
```

Exit 2 = broke, do it by hand. **Exit 1 = worked, the answer is no.**

## Step 1 — `declare`, once

Ask one at a time: where live inputs land (a `resources.json` key — named, never spelled out, so the record stays committable), how a window becomes places to look, and how a production id maps onto a dataset unit id.

That last one is the same problem `/eval-triage` has with `resolves_to`, and the same answer: **omitting it is honest.** A mapping nobody has worked out surfaces either now, at declaration, or later when somebody tries to pull the bytes and finds the ids mean nothing on the other side. Now is cheaper.

`--replace` is refused by default: readings already on record were taken under the old contract, and silently swapping it makes them describe something else.

## Step 2 — `sample`, one closed window

Four refusals, and the first two are about the window being a real interval:

| Refusal | Why |
|---|---|
| a timestamp with no UTC offset | Not an instant — an instant in an unnamed zone. Two readings in different zones cannot be ordered, and every trend over these records depends on ordering them |
| `--to` in the future | An open window is a moving target: re-read an hour later it covers more, so the enumeration and the draw are not reproducible |
| `--population` below what was enumerated | The declared total cannot be under the units actually seen; one of the two is measuring something else |
| no online contract | Guessing a production layout produces a reading that describes a directory nobody serves from |

The draw is `sha256(seed:unit_id)` ascending — **not a shuffle**, so it is stable under insertion and re-derivable from the record. A draw nobody can reproduce would make the reading assert a sample no one could check came from this window. `enumeration_digest` records what was enumerated, so a re-run either proves it saw the same window or says the source changed underneath.

### The population rule — the one that matters most

`population` is how many inputs production actually handled, and it is **usually unknowable**: request logging is itself sampled, rotated and lossy, so a listing sees what reached the store, not what happened.

- something that counts can say → `--population N --population-source "..."`, `rates_are: exact`
- nothing can → `population: null`, `population_basis: enumeration_only`, `sample_rate: null`, **`rates_are: lower bound`**

State the basis before quoting any rate. This is the same rule as a partial census: `enumerated` and `population` are different facts, and collapsing them turns "what reached the bucket" into "what happened" — which is how a logging outage reads as a quiet day.

### Reachability, three ways

Per prefix: it did not answer / it answered and the prefix is not there / it answered and this is what it holds. **Only the third means the window was quiet.** Any unreachable prefix makes the reading `complete: false`, and an incomplete reading may be reported but **never compared** — a drift verdict against a window with a missing day is a verdict about the outage.

## What this deliberately does not do

- **It computes no statistics.** Feature histograms over these units are a *run* — the user's code, through the ordinary run machinery, like `/data-curate`. Building a feature extractor in here would be MLClaw doing ML, and zero code invasion is the project's first principle.
- **It does not enforce `retain_until`.** The field travels with the reading because production data is somebody's data and "may we keep this" / "may we send it to a vendor" are `decision`s, not defaults. Enforcement is not built; that gap is stated rather than overlooked.
- **`scored_by` is a path, not a citation.** Which artifact was serving has no citable name yet — that is the model-identity layer. Input drift does not need it; **prediction drift is uninterpretable without it**, since "the output distribution moved" says nothing when nobody recorded which model produced the outputs.

## Requires / suggests

- **Requires**: `datasets/<id>/dataset.json` (the identity contract both sides count with), plus `resources.json` when the source is a server. A frozen snapshot is *not* required to take a reading — only to compare one.
- **Suggests**: a drift comparison against a frozen snapshot; `/data-collect --cite-window` when the interesting units are worth pulling and labeling; `/ask-human` for a `decision` when `retain_until` or vendor access is in question.

Per `<mlclaw_root>/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: null`, `execution: null` — a reading is a dated observation, not an execution to resume, so there is no step chain and nothing to continue from.
