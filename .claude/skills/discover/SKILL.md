---
name: discover
description: >
  Find out what exists when nobody can tell you — the taking-over case. Sweeps every reachable
  source (repo code and git history, docs and wikis, S3, cloud consoles, servers, ten tracking
  backends, the people who might know), records each lead with its evidence, then probes it and
  classifies it verified / gone / unreachable. Finds data, weights, somebody's recorded results,
  and the credentials the other probes turned out to need — one lead register, because a missing
  key and the runs behind it are one fact. Trigger for: inheriting someone else's project, a handover, data
  nobody documented, "where is the training data", "what data do we even have", a path in a config
  that may not exist any more, or a wiki page you do not trust. Also trigger for Chinese requests
  like "接手别人的工作", "翻历史数据", "数据在哪都不知道", "到处找数据", "有什么数据",
  "以前的数据还在吗", "文档里说的那个 bucket 还有吗". Not for censusing a dataset you have already
  described (use /data-check) and not for pulling data (use /data-collect).
---

# /discover — data archaeology

**The case this exists for.** You have taken over a project. There is a Confluence page from two
years ago, three repos with hardcoded paths, an S3 bucket you have partial credentials for, a cloud
console you were added to yesterday, and the person who knew has left. Nothing declares what data
exists.

`/data-check` cannot help: it censuses a dataset you have already *described*, and describing it is
the problem. This skill is the step before, and it is the data-side counterpart of `/train-init`'s
source sweep — same discipline, same `provenance`-style honesty about what was read versus guessed.

```bash
S=<mlclaw_root>/lifecycle/scripts/discover/discover.py

python $S sources --project <p>
python $S record  --project <p> --path <loc> --on local|s3|server:<key> \
                  --source-type code|tracking|git_history|server|s3|cloud_console|doc|person|other \
                  --evidence "<the doc, file:line, commit or person>" [--what "..."]
python $S probe   --project <p> [--id lead_0003] [--all] [--recheck-days N]
python $S reconcile --project <p> --stage training
python $S report  --project <p>
```

`record` also takes `--access-expires-at <ISO>` — see "Access expires, it does not
only go stale".

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

It reports each source as usable-now or blocked, with the reason, and every family in Step 2 gets a
row — including the ones no credential can reach. `outsourcing` parties are always listed as
not-usable on purpose: a vendor may be holding the only copy of a batch, and they are a source of
**answers**, not of listings. That is `/ask-human`.

Two fields on each row decide what you do with it, and neither is decoration:

- **`kind`** — `mine` (a place to grep for candidate locations) · `probe` (a location kind something
  can go and classify) · `ask` (a person). A wiki page and a NAS are both "sources" and nothing else
  about them is alike.
- **`blocked_by`** — `credential` · `human` · `registration` · `absent`. **Read this before offering a
  next step.** "Blocked" alone routes a wiki page to the same queue as an expired AWS key, and no
  credential ever unblocks a wiki page.

**Lead with the credential-free rows, and say the count out loud.** On day one nothing is registered
and most rows are blocked, so the temptation — and the shape the old output actively encouraged — is
to report that the sweep must wait for access. It must not: `code:*`, `git_history:*` and
`tracking_disk` need no key and are usually where the answer is. The `note` field names them.

## Step 2 — sweep, in this order

Ordered by what a mention is actually worth. Work down; do not start at the wiki.

| Source | Why this rank | What to look for |
|---|---|---|
| **code** | a path a script actually read is a path that existed | data roots, dataset classes, config YAMLs, `--data-dir` defaults, docker mounts |
| **git history** | a *removed* path is evidence of data that existed and moved | `git log -S/mnt`, `git log --diff-filter=D`, old config files |
| **tracking backend** | a run that trained recorded where it read from | W&B/MLflow run configs and artifact paths |
| **servers / S3 / cloud** | listings are evidence, not assertions | walk one level, do not walk terabytes |
| **tracking on disk** | **needs no credential, so it works on day one** | `events.out.tfevents.*`, `mlruns/*/meta.yaml`, `lightning_logs/version_*`, offline `wandb/run-*`, `.aim/`, `dvclive/` |
| **docs / wikis** | somebody wrote it, possibly from memory, possibly years ago | bucket names, share paths, dataset names — all `claim` |
| **people** | the last resort and often the only one | `/ask-human`, and their answer is a `claim` |

**Record every lead as you find it, with `--evidence`, which is not optional.** Six months on nobody
can tell which lead came from a config file that ran and which came from a wiki page written from
memory — and that difference is the whole basis for deciding which `gone` to panic about.

**Inherited repos are often still moving.** A path read out of the previous owner's repo is a
reading, not a fact — they may commit over it tomorrow, and every `file:line` in your evidence goes
stale the moment you pull. `probe` re-checks anything whose last probe is older than
`--recheck-days` (default 7) for exactly this reason.

## Tracking backends split in two, and one half needs no key

`on: tracking:<backend>` covers ten backends, and the split that matters is
whether probing needs access at all.

| Family | Probe | Backends |
|---|---|---|
| **disk** | a marker glob under a path. No import, no network, no credential | `tensorboard`, `mlruns`, `lightning`, `wandb_local`, `aim`, `dvclive` |
| **service** | credential → package → listing | `wandb`, `mlflow`, `clearml`, `neptune`, `comet` |

**Probe the disk family first.** It is the only tracking history readable before
access arrives, which is the state a handover starts in — and `train-init` Step 0
already treats it that way, marking local leftovers a default-yes for the same
reason. An empty directory is `gone` there: it listed, and it holds no runs.

For the service family the probe stages, and **each stage is a different
answer**: no credential (a worklist entry, not an absence) / credential but no
adapter / the server answered and the project is not there (`gone`) / reachable
with N runs.

| Backend | Listing | Exercised against |
|---|---|---|
| `mlflow` | REST over urllib | a stub **and a real server** |
| `wandb` | the `wandb` package (`pixi run -e probes`) | **a real account** — 25 runs on one project, a 10-project entity listing, both matching the raw API |
| `clearml` | REST: Basic → `auth.login` → Bearer; needs **both** halves of the key pair | a stub |
| `neptune` | REST: `X-Neptune-Api-Token` → oauth. The token is base64 JSON **carrying the host**, so decoding it is also how a self-hosted deployment is found | a stub |
| `comet` | REST: the key raw in `Authorization`, no `Bearer` | a stub |

**Prefer a REST surface over a vendor package when a backend offers both.** urllib
runs on the bare interpreter a handover starts with, and it can be tested against a
stub — which is why only `wandb`, whose API has no equivalent REST surface, needed a
real account before it could be trusted at all.

**A stub proves the parsing and the dispatch, not the endpoint.** So what carries the
three stub-only backends is a narrower property, and it is worth knowing when you
read one of their answers: being wrong about a URL, an auth scheme or a body shape
can only ever produce `unreachable`. A 200 whose body does not parse says
`NOTHING WAS COUNTED`; only a listing that succeeded and did not contain the name
says `gone`. **If one of the three answers `unreachable` naming an endpoint and a
code, suspect this file before you suspect the server** — and say so to the user
rather than reporting it as an access problem.

A backend with no listing adapter still says **both halves**: that a credential was
found, and that nobody counted the runs. Either half alone sends the reader the
wrong way. No shipped backend is in that state; it is where a new one lands.

**`verified` on a tracking lead means the RECORD exists, never that a number in
it is true.** A run summary reporting mAP 48.5 is a machine-made assertion, still
`claimed` in `origin.confidence` terms, and only a closed `/repro` session moves
it. Two words spelled the same with opposite bars, so every detail says which one
it means. `references/searches.md` → "Where the vocabulary breaks".

**Treat the first real listing on any stub-only backend as a test** — check the count
against what the web UI shows for the same project, and say you are doing it. That is
how `wandb`'s listing stopped being a promise, and "built" and "ran once" are
different facts. `clearml`, `neptune` and `comet` are still at "built".

## Access expires, it does not only go stale

`--recheck-days` says *the world may have changed, go look again*. It cannot say
the other thing: **this source stops being resolvable on a known date.** A
departing account's tracking history, a wiki page in a personal space, a key
pending rotation — all read identically to a lead that resolves next month, and
the standing advice for `unreachable` ("come back when access arrives") is exactly
wrong when access is about to be revoked instead of granted.

`record --access-expires-at <ISO>` records it. `report` then surfaces two things
before any count: leads expiring within `--expiring-soon-days` (default 14), and
leads whose access has **lapsed while still unresolved** — the transition nothing
else observes, because before the date and after it the record reads the same.

`/ask-human`'s `valid_until` exists for the same reason; this is its counterpart
for a lead.

## `reconcile` — the leads and a stage's candidates are one fact

A lead in `leads.json` and a candidate in `input.json` / `artifacts.json` both
answer *is this data here*, and nothing joined them. So a lead probed
`unreachable` could sit behind a candidate still marked `ok`, and `/train-run`
would launch against a path nobody can reach.

```bash
python $S reconcile --project <p> --stage training
```

Two directions, gaps first, exit 1 when either fires:

| Direction | What it catches |
|---|---|
| **coverage** | a declared `items` entry with no usable candidate **and no lead looking for it** — a need nothing is searching for. This is the item-driven half of discovery, and it is invisible otherwise: an item with no candidates looks exactly like one whose candidates all failed |
| **drift** | a candidate whose `match` its lead's status does not permit |

The permitted pairs, and the load-bearing row is the last:

| Lead status | Candidate `match` may be |
|---|---|
| `verified` | `ok`, `mismatch`, `pending` — the lead says it is there; whether it *fits* is the init's own judgment |
| `unreachable` | `unreachable` only. "Could not look" must not become "not there", and must never become usable |
| `gone` | `absent` |
| **`claim`** | `unreachable`, `absent`, `pending` — **never `ok`.** A candidate is not usable on a document's or a person's word. "Never let somebody's word become a checked fact", applied to whether data exists |

`code_default` and `downloadable` entries carry no `lead_id` by design — they come
from the code, not from a sweep — and are reported as unlinked rather than as
drift.

**It reports and never writes.** `candidates` is filled by the stage's init skill
with the user confirming each entry; a discovery script reaching in to "fix" one
would make that confirmation a formality.

## The access worklist

Every `unreachable` names what was missing, so the distinct blockers already
exist. `report` aggregates them, most-blocking first: which key to go and get,
and how many leads it unblocks. Grouped on the blocker each probe **asserts**,
never on its prose — grouping on the message put one `AccessDenied` across three
buckets into three rows.

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
