---
name: ara
description: >
  Assemble a round's work into an Agent-Native Research Artifact — the five layers, an
  ARTIFACT.md a person can read a year later, and a check that says whether the frozen copy
  still agrees with the live record. Trigger at the end of an exploration or a tune session,
  after /conclude, and whenever somebody says "把这轮的东西整理成一份", "出一份工件",
  "交接给别人", "这轮的产出在哪", "ARA", "整理成可复现的一份", "归档这一轮". Also trigger
  before citing an old artifact, because its statuses and tiers were frozen at build time and
  do not update themselves. Not for getting bytes off a machine before it is destroyed
  (that is /evacuate, which calls this) and not for writing the conclusions themselves
  (that is /conclude, which fills the logic layer).
---

# /ara — a round's work, made into an artifact somebody can read

ARA (arXiv:2604.24658) argues that **the artifact is itself the object of research**, not a
by-product of it. It gives four layers: `logic/` (what is claimed), `src/` (what produced
it), `evidence/` (the numbers), `trace/` (the exploration DAG).

MLClaw already produces all four. What it has never had is **somewhere to put them**. What a
finished round leaves behind is a pile of run directories — and that is not the same thing as
an artifact another person can read.

## The five layers

| Layer | What goes in it |
|---|---|
| `src/` | code snapshot, `config_snapshot.json`, `sources.json`, env — **plus what each stage's `-init` declared**: its `config.json`, `artifacts.json`, `input.json`, `output.json`, `provenance.json`, `recipe.md`. **In an architecture search the code IS the variable**, so this layer is not background — **it is the reproducibility claim itself** |
| `evidence/` | `stream.jsonl`, metrics, `tb/`, **the raw logs**. Logs belong here because MLClaw's grounding rule requires a number to cite the transcribed line it came from — **that line is the evidence** |
| `logic/` | `knowledge/conclusions.json` — what `/conclude` produced |
| `trace/` | every dated record of a multi-step process: `/explore`'s `graph.json`, `findings.json`, `baseline.json`, `audit.json` — **and a tune session's `state.json` + `chain.md`, a repro loop's `session.json`, an adaptation campaign's.** **This layer decides whether that ablation is still legible a year from now** |
| **`weights/`** | **‼️ ARA does not have this layer.** Not an oversight: a paper's artifact is **knowledge**, and knowledge regrows from `src + evidence`. **A checkpoint does not.** It is the one layer that cannot be rebuilt |

Plus one bucket that is deliberately **not** a layer: `unclassified` — whatever the rules did
not recognise is **kept anyway, and named**. A sweep that keeps only what it recognises is
exactly how the file nobody thought of gets lost, and it reports success while doing it.

## Two verbs

```
<mlclaw_root>/scripts/ara/ara.py build --project <P> [--root <R>] [--out <D>]
<mlclaw_root>/scripts/ara/ara.py check --project <P>
```

`build` layers a **project** by default; `--root` points it at a different tree (`/evacuate`
passes the path on the machine that is about to disappear).

‼️ **Every `build` produces a new dated artifact and never overwrites the last one.** An
artifact is a **dated reading**, the same kind of thing as a census or an evacuation record.
A second round overwriting the first destroys the only record of *what the first round
believed at the time* — and that is precisely what makes the first round's runs legible.
Rebuilding in place requires an explicit `--id <existing>`.

## One artifact shape, two amounts of detail

**A tune round's artifact and an exploration round's artifact are the same object.** Both are
these five layers; an exploration simply fills `trace/` far more heavily, because it *has*
more process to record — a graph of arms, a noise floor, a four-state audit. A tune session
fills the same layer with its `state.json` and `chain.md`, and a project that has only ever
trained fills it with nothing at all and is still an artifact.

‼️ **Nothing here knows what a stage is called.** `classify()` decides a file's layer from its
path, and it is the **only** thing that decides it. That was not always true: the copy step
used to name `knowledge/` → `logic/` and `stages/exploration/` → `trace/` as literal
directories, which made it a second author of a fact `classify()` already owned — and the two
disagreed **in both directions at once**. `stages/exploration/config.json` was *counted*
`unclassified` and *copied* into `trace/`; a tune session's `chain.md` was counted `trace` and
copied nowhere. **An index that names a file the directory beside it does not hold** is the
failure this skill exists to report, and it was doing it to itself.

## What is copied, and what is in by reference

Every `.json` / `.md` in the project **outside** `code/`, `artifacts/`, `data/`, `runs/` and
`original/` is a **record**, and records are **physically copied into** the artifact — each
into the layer `classify()` assigns it, keeping **its own path** (`src/stages/training/config.json`,
`trace/stages/training/tune_sessions/s1/chain.md`). They have to be readable without
downloading the weights, and they are what survives when the weights are gone. The path is
kept whole rather than flattened to a basename because two stages both have a `config.json`,
and flattening would have one silently overwrite the other **inside the artifact**.

**Runs are in by reference, not by copy.** `runs/` is what `--root` already walks; copying it
would duplicate every run record into every dated artifact and grow with the run count
forever. A record the rules do not recognise still gets copied — into `unclassified/`, named.

## ‼️ What `check` catches: a frozen belief does not update itself

This is CLAUDE.md's *"Never repeat a conclusion without re-reading its status"* **one level
up**, happening in the copy people actually read.

`/conclude`'s `status` and `tier` are **computed**; the artifact **freezes** them. When the
evidence rots, the frozen copy **does not change a single character** — and the frozen copy is
the one that gets handed over.

```
K01: the artifact froze `supported` and the record now says `unverifiable`.
     The frozen copy is the one people read, and nothing about it changed
     when its evidence did
```

**It reports; it does not repair.** Repairing erases the only evidence that the artifact
outlived its evidence, and turns the report green while doing it — the same rule as
`graph.py check` and `conclude.py check`.

Run `check` before citing an old artifact.

## Reproducibility is read, not re-derived

`code_snapshot.py` computed `code.reproducible` at launch. This only reads it and never
re-derives it — that check already refused what it had to refuse, and a "second opinion" here
could only disagree with it.

`false` means something specific: a changed file was too large to embed, so
`git checkout && git apply` rebuilds **a different tree**.

‼️ **This never refuses.** Losing the bytes is far worse than an imprecise label, and printing
the verdict on the first screen is enough — the same rule as a census recording
`complete: false` rather than withholding itself.

**An artifact with no `src/` layer gets named as such: that is a backup, not an artifact**,
and nothing in it says what actually differed between two arms of an ablation. One with no
`logic/` layer is pointed at `/conclude`.

## Its relationship to `/evacuate`: called by it, not contained in it

An evacuation's scope is **one machine** — which may hold fragments of three rounds, or no
artifact at all, plus a pile of files belonging to no artifact, and it is gated by a **lease**.
This skill's scope is **one round**, and it has **no deadline**.

What is actually true is this: **the moment before a machine disappears is the last moment its
source can be read.** So that deadline *forces* the artifact to be completed; it does not
contain it.

> The same shape elsewhere: `/train-run` calls `/eval-run` to measure a base model when
> fine-tuning. That does not make train-run a stage of eval — it only means **that is the one
> moment the measurement can still be taken**.

## Every round produces one from here on

Not because this document says so. Two things enforce it:

- **`graph.py check`** — a round with settled cards but no artifact (or an artifact **older
  than** the last settlement, in which case it describes a different round) reports **major**.
  Major and not critical: a missing cover page must not block the next arm, and CLAUDE.md
  reserves refusal for things that would make the next measurement wrong.
- **`evacuate.py clearance`** — if nobody built one, it **builds one on the spot**. Not
  "no artifact, no clearance": stalling a machine that is still burning money over a markdown
  file is a deadlock, and losing bytes is worse than anything. **Building it falls in the
  "just do it" bucket** — cheap, local, reversible. The one thing it may not do is **fail
  quietly**.

## Three things it does not do

- **It does not write conclusions.** That is `/conclude`'s, and it fills the `logic/` layer.
- **It does not move bytes and does not touch machines.** That is `/evacuate`'s.
- **It does not repair an artifact.** `check` only reports. Rebuilding means running `build`
  again.
