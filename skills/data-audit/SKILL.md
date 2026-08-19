---
name: data-audit
description: >
  Open the files and judge the data against its own declared contract and the code about to consume
  it — the one thing on the data line that reads content. Layered fatal → advisory: does every
  referenced file exist and parse, does the annotation format match the dataloader, is the
  annotation internally consistent, what is statistically out of family. Trigger for: checking data
  before a training or eval run, finding corrupted or unreadable files, finding bboxes outside the
  image or empty or duplicate annotations, checking a class list against the code's num_classes,
  finding outliers and long-tail classes, and diffing a new dataset's schema against the one the
  code was written for. Also trigger for Chinese requests like "检查数据质量", "数据有没有问题",
  "标注对不对", "开训前查一下数据", "这批新数据格式变了吗", "有没有坏图", "类别分布看一下".
  Not for where the data physically is or whether it is replicated (use /data-check, which never
  reads content) and not for finding bad cases through a model's errors (use /eval-triage).
---

# /data-audit — the one thing that opens the file

`/data-check` censuses: existence, location, completeness markers, **never content**. That line is
what keeps it one small script instead of a second pipeline, and it is right. But it leaves a real
gap, named in `lifecycle/references/roadmap.md` → "Also outstanding": *curate records a conversion
and the census reads no file content; neither performs one.* A unit can be present at every
location, replicated three times, marked complete — and be 40 000 annotations whose category ids
stopped matching the class list two collections ago.

This skill is that reading. It is the **only** place on the data line permitted to open a file and
judge what is inside.

## What it is not, stated first, because the objection is in the design

`/data-check`'s own frontmatter routes quality away: *"not for judging the data's quality (that is
an evaluation stage)."* That routing is about a **model's** quality — a score, on data, against
labels. Nothing here scores a model, and nothing here needs one.

| | judges | needs a model | when |
|---|---|---|---|
| evaluation stage | a model, against labels | yes | after training |
| `/eval-triage` | individual bad cases, **found through a model's errors** | yes | after an eval run |
| **`/data-audit`** | the data, **against its own contract and the consuming code** | **no** | **before any run** |

The distinction that matters is not strictness, it is *evidence*. `/eval-triage` finds a wrong label
because a good model got it wrong — which means it can only ever see labels a model happened to
disagree with, after paying for a training run. This skill finds the same wrong label from internal
consistency alone, for free, before launch. **Neither subsumes the other**, and both route their
label findings to the same owner: a `/data-label` rework.

## The judging rule

**Every finding must name the file and the record it came from. Never a count alone.**

A count is not evidence and cannot be acted on — "1 240 inconsistent annotations" tells nobody which
collection broke or whether it is one bad export. This is CLAUDE.md "Never record a metric you did
not read" turned toward the output: the audit's whole value is that it looked, so it has to say
where.

The other half of the same rule: **an audit fixes nothing.** It does not rewrite an annotation, drop
a sample, or move a byte. Same split as `/data-check` and for the stronger reason — a repair is a
derivation, and a derivation that nobody recorded is exactly the untraceable dataset the data line
exists to prevent. A fix is a `/data-curate` run consuming this audit's findings, separately
recorded and separately citable.

## What it audits against

Three references, and a check that has none of them is not a check, it is an opinion:

| Reference | Where it comes from | What it makes checkable |
|---|---|---|
| the **layout contract** | `datasets/<id>/dataset.json` | which layers a unit must carry; what proves it finished |
| the **consuming code** | the stage's `config.json` + its dataloader | annotation format, `num_classes`, field names, channel order |
| a **prior dataset or snapshot** | `datasets/<id>@<sid>`, or the last audit | what the schema used to be |

Ask which stage's code this data is headed for — that is the second reference, and without it Step 2
cannot run at all. It is the one thing only the user knows here; everything else is read (CLAUDE.md
"Decide what evidence can decide").

## Step 1 — Integrity (fatal)

Does the data physically parse.

- every file the annotation references exists on disk
- every file on disk is referenced by something (orphans are a warning, not an error — a staging
  directory legitimately has them)
- files open without throwing; sample rather than decode a terabyte, and **say the sample size**
- zero-byte and implausibly-small files

**Stop here on a fatal.** Steps 2-4 on unparseable data produce numbers that describe the breakage
rather than the data, and a statistical summary computed over a half-read file is worse than no
summary, because it looks like one.

## Step 2 — Code compatibility (fatal)

Does the code about to consume this data actually accept it. Read both sides — data samples and the
dataloader — and check:

- annotation format vs what the dataloader parses
- class count vs `num_classes` in the code or config
- field names and types the code indexes by
- image format, channel count, dtype, value range

**This is the check that earns the whole skill.** Every other finding here degrades a result;
this one is the class of defect that trains to convergence and produces a model that is confidently
wrong, with nothing anywhere raising. A category id the dataloader silently clamps is not a crash.

Per CLAUDE.md "Never pass a param the code ignores", the same suspicion applies to data: a field the
dataloader never reads is not a field this dataset has.

## Step 3 — Internal consistency (silent errors)

Things that will not crash and will make the result wrong:

- bounding boxes outside image bounds, or with non-positive width/height
- duplicate annotations on one unit
- units with an entry and zero labels — distinguish **"annotated as empty"** from **"never
  annotated"**; they are two facts and only one is a defect, and if the format cannot tell them
  apart, record that as the finding
- category ids absent from the declared class list
- negative values where the schema says positive
- degenerate or self-intersecting polygons

Write the scan against the actual format (see "Why there is no script" below). Report counts **with
the naming rule above**, and offer the sample list.

## Step 4 — Statistics (advisory)

Per selected field: mean, std, min, max, median, and what falls outside 3σ — **listed, not just
counted.** For discrete fields: frequency distribution and a long-tail flag (classes under 1% of the
mean count). Derived metrics follow the data type — bbox area and aspect ratio, objects per unit,
class balance, polygon area.

**These are advisory and must be said as advisory.** A 3σ outlier is not a defect; it is a sample
worth a human's eye. An audit that reports its outliers in the same voice as its fatals teaches its
reader that fatals are negotiable.

## Step 5 — Schema diff against the prior version

Ask for a prior dataset, snapshot, or audit to diff against; skip cleanly if there is none.

**Only the schema half belongs here.** Statistical drift between a frozen set and production is
`/data-drift`'s, whose reference side is `/data-freeze` and whose live side is
`/data-online-sample` — a dated reading, which this skill has no way to take. Do not compute a drift
verdict from two directories on disk; that is the comparison every drift tool fakes.

What is this skill's, because it is a **format** fact rather than a distribution fact:

- fields added, removed, or retyped
- category list changes — added, removed, **renumbered** (the dangerous one: same names, shifted ids,
  and every model trained on the old numbering now reads as trained on a different task)
- coordinate convention changes — normalized ↔ absolute, xywh ↔ xyxy, origin flips
- unit count and per-class count deltas, reported as context for the above rather than as a verdict

A schema change is `fatal` when Step 2's references would no longer hold. Say which one.

## Step 6 — Whatever the user asks

Ask what else to check. Domain checks are the point of a conversational tool — "night scenes only",
"aspect ratio over 10:1", "anything the vendor touched last week". Write and run it. Record it in
`audit.json -> ad_hoc`; when the same one recurs across audits, that is the signal to promote it to a
numbered step above, which is the same promotion rule as `steps.ad_hoc` in
`lifecycle/references/skill-graph.md`.

Keep asking until the user says done.

## Step 7 — Record

Write `datasets/<id>/audits/<audit_id>/audit.json`: the three references it judged against, per-layer
verdict, every finding with its file and record locator, the sample sizes, and what was skipped and
why. **A skipped check is a recorded field, never an absent one** — an audit missing its
compatibility section reads identically to one that passed it.

**The per-layer verdict is a fixed key, because a consumer reads it.** `audit.json -> layers` maps each
layer to `{verdict, ...}`, and `verdict` is one of `FATAL` / `WARN` / `INFO` / `SKIP` — the same
vocabulary the display below already prints. Also record `audited_at` (UTC, explicit offset) and
`snapshot`, the snapshot this audit judged.

Those three fields are what `audit_gate.py` reads before a production run, and the gate is why the
`SKIP` rule above stops being advice: an audit with no `layers` map is ruled `unreadable`, and a layer
that is `SKIP` or simply missing makes the dataset `unverifiable` — **never clean**. An audit whose
`snapshot` differs from the one a run cites is `stale`: a verdict about different bytes. Everything
else in this file is yours to shape per format; these three are not.

```
Audit  boxes @ 260731   vs stages/training code
  [FATAL] compatibility : class list has 81 entries, model config says 80
  [WARN ] consistency   : 23 bbox outside image, 12 units annotated-vs-unannotated ambiguous
  [INFO ] statistics    : 18 area outliers; class "bicycle" at 8 samples (0.4% of mean)
  [INFO ] schema vs v1  : +200 units, "electric_scooter" added at id 80  ← the FATAL above
  [SKIP ] integrity     : full decode not run, sampled 500/5240
```

The last two lines carry most of this skill's value and neither is a count: one connects a schema
change to the fatal it caused, the other says what the audit did **not** look at.

## Why the AUDIT has no script — and why the GATE does

`lifecycle/scripts/data-audit/` holds exactly one file, `audit_gate.py`, and it is not an
auditor. It is the **consumer's** check: it reads the `layers` verdict map a finished audit
wrote and refuses a production run whose data is `fatal` / `never_audited` / `unverifiable` /
`stale` / `unreadable`. That is format-independent by construction — it never opens a sample.
The argument below is about the checks themselves, and it still holds in full.

Every other skill on this line calls one. This one writes its checks per audit, and the deviation is
deliberate: the checks are a function of the format, and there is no format. COCO, YOLO, VOC, a
parquet table, someone's in-house JSON — a generic implementation would either handle four formats
and refuse the fifth, or degrade to checks so weak they pass anything. Both are worse than an agent
reading the actual schema and writing eight lines against it.

What that costs is stability across audits, so pay it back deliberately: `audit.json` records the
check source alongside its finding, and an audit of the same dataset re-runs the recorded checks
before adding new ones. Otherwise "23 bboxes outside the image" last month and "31" today are not
comparable numbers, and the whole point of running it twice is comparing them.

CLAUDE.md "Script Integration" still applies in the other direction: if an ad_hoc check earns
promotion by recurring, it earns a script.

## Requires / suggests

- **Requires**: `project.json`, and either a declared dataset (`datasets/<id>/dataset.json`) or a
  concrete path the user names. Step 2 additionally requires a stage whose `config.json ->
  entry_command` is non-empty — without consuming code there is nothing to be compatible *with*, and
  the honest move is to record that section `skipped` and say so, not to invent a target.
- **Suggests**, by what was found, never a generic next step:
  - `label_wrong`-shaped findings → a `/data-label` rework. Same owner as `/eval-triage`'s, and the
    same rule: never fix a label in place.
  - a schema or format defect → `/data-curate`, which is where a conversion is *recorded*.
  - a fatal on data already frozen → `/data-freeze` for a corrected snapshot, and say plainly that
    every run citing the old one inherits the defect. Per CLAUDE.md "Never delete data a frozen
    snapshot still names", the old snapshot does not get quietly replaced.
  - clean → whatever the user was heading for: `/train-run`, `/eval-run`.
  - `/data-audit-report` whenever there is anything a person should look at rather than read.

Per `lifecycle/references/skill-graph.md` → "Workflow State Protocol", push on entry and pop on exit.
`stage: null` (a dataset is not a stage — same reason as `/data-check`), `execution: <audit_id>`,
`step` one of `integrity` / `compatibility` / `consistency` / `statistics` / `schema_diff` /
`ad_hoc` / `record`. Unlike a census, an audit **does** have a step chain and is resumable: it is a
process MLClaw runs, and Step 1 refusing means Steps 2-4 genuinely did not happen.

**Pop before reporting the findings.** A finished audit is not unfinished work, and leaving it on
the stack opens the next session with a false resume prompt.
