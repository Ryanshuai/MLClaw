---
name: data-discover
description: >
  Find out what data exists when nobody can tell you — the taking-over case. Sweeps every reachable
  source (repo code and git history, docs and wikis, S3, cloud consoles, servers, tracking backends,
  the people who might know), records each lead with its evidence, then probes it and classifies it
  verified / gone / unreachable. Trigger for: inheriting someone else's project, a handover, data
  nobody documented, "where is the training data", "what data do we even have", a path in a config
  that may not exist any more, or a wiki page you do not trust. Also trigger for Chinese requests
  like "接手别人的工作", "翻历史数据", "数据在哪都不知道", "到处找数据", "有什么数据",
  "以前的数据还在吗", "文档里说的那个 bucket 还有吗". Not for censusing a dataset you have already
  described (use /data-check) and not for pulling data (use /data-collect).
---

# /data-discover — data archaeology

**The case this exists for.** You have taken over a project. There is a Confluence page from two
years ago, three repos with hardcoded paths, an S3 bucket you have partial credentials for, a cloud
console you were added to yesterday, and the person who knew has left. Nothing declares what data
exists.

`/data-check` cannot help: it censuses a dataset you have already *described*, and describing it is
the problem. This skill is the step before, and it is the data-side counterpart of `/train-init`'s
source sweep — same discipline, same `provenance`-style honesty about what was read versus guessed.

```bash
S=<mlclaw_root>/lifecycle/scripts/data-discover/discover.py

python $S sources --project <p>
python $S record  --project <p> --path <loc> --on local|s3|server:<key> \
                  --source-type code|tracking|git_history|server|s3|cloud_console|doc|person|other \
                  --evidence "<the doc, file:line, commit or person>" [--what "..."]
python $S probe   --project <p> [--id lead_0003] [--all] [--recheck-days N]
python $S report  --project <p>
```

Exit 2 = broke, do it by hand. **Exit 1 = worked, the answer is no** — `probe` exits 1 when a lead
came back `gone`, and `record` exits 1 on a duplicate. Neither is a crash.

## Four statuses, and the value is in the last two

| Status | Means |
|---|---|
| `claim` | a doc, a code path or a person says so. **This is what a handover hands you, and it is not evidence.** |
| `verified` | something other than a sentence listed it, and it is there now |
| **`gone`** | we looked where the claim pointed and it is not there |
| **`unreachable`** | we could not look — no credentials, host down, private repo |

**`gone` is the finding to act on today.** A path in a config that no longer resolves is data that
moved or was deleted, and every week that passes lowers the odds that anybody still remembers where.
Escalate it while there is someone to ask — that is `/ask-human`, and the answer is a `claim` until
a probe agrees.

**`unreachable` is never `gone`, and on a handover it is the majority state for weeks.** Access
arrives after responsibility does. A sweep that spelled "I have no AWS key" as "the data is not
there" would have you chasing datasets that are perfectly fine, and would make the one real loss
invisible in the noise.

The probe code is deliberately fussy about this. Permission-denied on a local directory is
`unreachable`, not `gone`. An `aws s3 ls` failure is only `gone` when the wording says the bucket or
prefix genuinely has nothing in it; anything mentioning credentials, expiry or access is
`unreachable`. An ssh probe requires **both** a zero exit and a sentinel, because a shell that dies
mid-command returns zero and an empty listing.

## Step 1 — `sources`, before looking anywhere

This is the checklist, and its real job is making *"what did you not check"* answerable. Without a
list of what could have been checked, a findings list is unfalsifiable — it looks equally complete
whether you swept eight sources or one.

It reports each source as usable-now or blocked, with the reason. `outsourcing` parties are always
listed as not-usable on purpose: a vendor may be holding the only copy of a batch, and they are a
source of **answers**, not of listings. That is `/ask-human`.

## Step 2 — sweep, in this order

Ordered by what a mention is actually worth. Work down; do not start at the wiki.

| Source | Why this rank | What to look for |
|---|---|---|
| **code** | a path a script actually read is a path that existed | data roots, dataset classes, config YAMLs, `--data-dir` defaults, docker mounts |
| **git history** | a *removed* path is evidence of data that existed and moved | `git log -S/mnt`, `git log --diff-filter=D`, old config files |
| **tracking backend** | a run that trained recorded where it read from | W&B/MLflow run configs and artifact paths |
| **servers / S3 / cloud** | listings are evidence, not assertions | walk one level, do not walk terabytes |
| **docs / wikis** | somebody wrote it, possibly from memory, possibly years ago | bucket names, share paths, dataset names — all `claim` |
| **people** | the last resort and often the only one | `/ask-human`, and their answer is a `claim` |

**Record every lead as you find it, with `--evidence`, which is not optional.** Six months on nobody
can tell which lead came from a config file that ran and which came from a wiki page written from
memory — and that difference is the whole basis for deciding which `gone` to panic about.

**Inherited repos are often still moving.** A path read out of the previous owner's repo is a
reading, not a fact — they may commit over it tomorrow, and every `file:line` in your evidence goes
stale the moment you pull. `probe` re-checks anything whose last probe is older than
`--recheck-days` (default 7) for exactly this reason.

## Step 3 — `report`, which states the gaps first

Unprobed leads, unreachable leads and stale probes come **before** any count. A findings list with
its caveats underneath is a findings list read as an inventory, and handing that to somebody on day
three of a handover is how a missing dataset gets discovered in month four. Same ordering rule as
the board's partial-census banner, same reason.

`exhaustive` is hardcoded `false`, and this is not modesty. **A sweep finds what somebody wrote down
or left a path to.** Data that nobody documented and no surviving code points at will not appear
here, and no number of clean probes changes that. Say so when you report; the person you are telling
will otherwise assume the list is the world.

## What it deliberately does not do

**It never declares a dataset.** Its output is leads; `/data-check` Step 1 declares the layout
contract with the user. The reason is specific: `identity.unit_glob`'s *depth* decides every unit id,
a wrong depth yields zero units with no error, and every count downstream is then correct about a set
that excludes an entire machine. A guessed glob is worse than none, so this skill hands over a
verified path and stops.

**It never pulls.** Once a lead is `verified` and you want the bytes local, that is `/data-collect`.

**It never writes to a source.** Every verb here reads, and `probe` lists one level rather than
walking a tree — a discovery sweep that spends four hours on a NAS is a sweep nobody runs twice.

## The record is the handover artifact

`{PROJECT}/discovery/leads.json` — one living file, not a dated scan, because a lead is long-lived
and its status changes as access arrives. It is git-tracked, and it is the thing you hand the next
person instead of a Confluence page: every path, where the claim came from, what was actually found,
and when it was last checked.

## Requires / suggests

- **Requires**: `project.json`. Nothing else — the entire point is that nothing has been declared
  yet. `resources.json` missing is a normal day-one state and is reported as a blocked source rather
  than an error.
- **Suggests**: `/data-check` to declare and census anything `verified`; `/data-collect` to pull it;
  `/ask-human` for anything `gone` or still only `claim`ed, while there is somebody to ask;
  `/resources` when `sources` says the blocker is credentials.

Per `lifecycle/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: null`,
`execution: null` — a sweep is an observation, not an execution to resume, and the leads file is what
persists.
