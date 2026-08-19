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
S=<mlclaw_root>/scripts/discover/discover.py

python $S sources --project <p>
python $S introspect --project <p> --checkpoint <a .pt on disk> [--record]
python $S record  --project <p> --path <loc> --on local|s3|server:<key>|host_unknown \
                  --source-type checkpoint|code|tracking|git_history|server|s3|cloud_console|doc|person|other \
                  --subject data|weights|results|credentials \
                  [--url <where a person opens the THING>] \
                  [--evidence-url <where a person opens the CLAIM>] \
                  --evidence "<the doc, file:line, commit or person>" [--what "..."]
python $S probe   --project <p> [--id lead_0003] [--all] [--recheck-days N]
python $S reconcile --project <p> --stage training
python $S brief   --project <p>
python $S save    --project <p> [--message "..."]
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
`unreachable`, not `gone`. Anything mentioning credentials, expiry or access is `unreachable`. An
ssh probe requires **both** a zero exit and a sentinel, because a shell that dies mid-command
returns zero and an empty listing.

S3 needs the same care in the other direction: `aws s3 ls` exits **non-zero when it matched
nothing**, so exit code alone cannot separate "empty" from "failed". `--summarize` is what resolves
it — `Total Objects: 0` is printed only when the listing actually ran, so sentinel present *and*
stderr empty is a real reading of `gone`. Without that branch a prefix that is genuinely empty could
never be reported, and a document's claim that something is empty could never be confirmed.

**S3 credentials come from `resources.json → aws`, and the probe says which key answered.** Not from
whatever the CLI resolves ambiently — those can be different IAM users, and when they are, the sweep
reports no access over data it can read. When a probe is refused, read the blocker: it distinguishes
`s3:denied_with_registered_key` (**the registered key lacks the permission — this is a policy ask to
the bucket owner**) from `s3:no_usable_credential` (nobody has registered a key). Sending someone to
request access they already hold is how a worklist stops being believed.

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

### The s3 row states a reach it has not measured, unless something measured it

`resources.json → aws.s3_bucket` is **the bucket a run writes to, not the surface a sweep can
cover.** A working key routinely reaches many more — on the project this skill was built against,
twenty, of which the registry named one.

That under-report is worse than a refusal, and the asymmetry is the whole point: *"no access"* sends
somebody to get a key, while **one bucket out of twenty reads as the whole world and sends nobody
anywhere.** A sweep over 5% of the surface produces a findings list that looks complete — the exact
failure this verb exists to prevent, committed by the verb itself.

So the reach is a *measurement*, and it has its own verb:

```bash
python $S surface --project <p>      # NETWORK. Enumerates every bucket the credential
                                     # can see, then classifies each: listable / access_denied
```

`sources` stays records-only and reports whatever the last dated reading found — the same split as
`census.py scan` (goes and looks, dated, may be partial) versus `dataset.json` (the durable
contract). **Do not merge them**: `sources` is called at conversation start, and four network
timeouts before the user's first sentence is not a greeting.

Read three fields off the row and say them:

| | |
|---|---|
| `reachable_buckets: null` + `surface_warning` | **nobody has ever measured the reach.** Say so before quoting any sweep as complete, and offer `surface` |
| `surface_measured_days_ago` | a reading, like a census, and it moves. State the age before the count |
| `surface_by_state.access_denied` | **a POLICY ask against the bucket's owner, not a key ask.** The credential is already accepted; a request for a *new key* comes back a week later having changed nothing |

`enumerated: false` means `ListAllMyBuckets` was denied — an account-level permission routinely
absent on a key that reads specific buckets fine. The list is then a **lower bound** on the reach,
never the reach.

**Lead with the credential-free rows, and say the count out loud.** On day one nothing is registered
and most rows are blocked, so the temptation — and the shape the old output actively encouraged — is
to report that the sweep must wait for access. It must not: `code:*`, `git_history:*` and
`tracking_disk` need no key and are usually where the answer is. The `note` field names them.

## Step 2 — sweep, in this order

Ordered by what a mention is actually worth. Work down; do not start at the wiki.

| Source | Why this rank | What to look for |
|---|---|---|
| **checkpoint** | code shows what a script *could* read; a checkpoint records what it *did*, and the run wrote it — plus it needs no host and no key | `introspect` it: `train_args.data` is **the val split**, `train_args.model` is the parent, `train_metrics` are its own numbers, `git.commit` is the code axis |
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

## Step 3 — `brief`, which is the deliverable

`report` is a table and `leads.json` is the record. **Neither is what you hand somebody**, and
`references/searches.md` already specifies the shape that is: its source × subject table, filled in,
plus the reading of it. `brief` renders that to `discovery/brief.md`.

The section order is the argument, and it is the same rule as the report's:

| Section | Answers |
|---|---|
| **What is odd** | status flips, duplicate objects, verified-but-empty locations, a source a probe already contradicted, stale claims — urgent first |
| **What to look at next** | the cells of the table with no lead, **ranked**, and only for sources reachable right now |
| **Who is blocking** | the access worklist — one row per thing to go and get |
| **Coverage** | the grid, every ranked source as a row including the empty ones |
| **What was found** | last, deliberately |
| **Reading** | **empty until a person writes it** |

**Findings last, because a findings list read first becomes an inventory** and the caveats under it
get skipped. Handing that to somebody on day three is how a missing dataset is discovered in month
four.

**`--subject` is what makes the grid computable, and it is never inferred.** Four searches share one
engine — data, weights, results, credentials — and a lead used to carry no record of which one it
belonged to, so "did anybody look in git history for weights" had no answer. Guessing the subject
from a path would put a lead in a cell nobody chose and make the table evidence for coverage that was
never decided; `unclassified` is reported as a gap instead. One exception worth knowing: **an empty
`credentials` row is not a gap when the worklist is non-empty** — that search is generated by the
other three failing, so its findings are the blocking list rather than leads.

**It is markdown so a reader can open things, and a lead carries two URLs, not one.**
`--url` is where the **thing** is (a console, a UI); `--evidence-url` is where the **claim** came from
(the wiki page, the commit, the ticket). Same split as `on` versus `source_type`, and one link cannot
be both — a brief that conflated them sends somebody to a Confluence page when they asked to see the
bucket. The findings table renders both, and the evidence column matters most: it is what separates a
path a script read from a line somebody wrote from memory.

`s3`, `local` and `tracking:wandb` links are **derived at render time and never stored**, and marked
with `†`. Storing a console route would put a claim about somebody's web UI inside a record of what
exists, and the two rot on completely different schedules. The mark is the load-bearing part: a
guessed route that 404s must not read like a verified location. Nothing is derived for `server:`,
`clearml`, `doc` or `person` — a remote path has no URL a browser opens, and a document whose links
are half dead is one nobody clicks twice. Pass `--url` for those.

Two smaller things the links get right, both of which are the same confusion this skill exists to
prevent, reintroduced by a URL: an S3 **object** gets `/s3/object/…` rather than the prefix view,
because sending an object to the prefix view lands on an empty listing that reads exactly like the
data being gone; and **no region is put in the URL**, because `resources.json → aws.region` is one
global setting while a bucket has its own — the sweep that exercised this had `us-west-2` configured
and hit a bucket named `…-repo-ohio`.

**Every section above the last one is computed. The last one is yours.** A detector can say two
objects share a name and a byte count, that a page was wrong about one of four claims, that nobody has
looked in the code. It cannot say which of those matters here. Write that in — and if the anomalies
list is empty, say what you checked that the detectors do not cover, because an unwritten Reading
section and a genuinely quiet sweep look identical.

## Step 4 — `report`, which states the gaps first

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

## The record is the handover artifact — `save` is what makes that true

`{PROJECT}/discovery/leads.json` — one living file, not a dated scan, because a lead is long-lived
and its status changes as access arrives. It is the thing you hand the next person instead of a
Confluence page: every path, where the claim came from, what was actually found, and when it was
last checked.

**Writing it and keeping it are different things, so `save` is a step, not an implementation
detail.** The write is crash-safe on one disk; that is all it is. Until the record is committed it
goes nowhere on a clone, a push or a `git clean` — which is how a handover actually happens. A
handover artifact that does not survive the handover is the failure this whole skill was written
against, so `report` says `UNSAVED` until it is done, and **offer the save after any probe that
changed something.** It is one command and the user confirms it, per "confirm before saving".

```bash
python $S save --project <p>          # → "discover: 10 lead(s) — 6 verified, 1 gone, 2 unreachable, 1 claim"
```

Two things it will not do. It commits **only** `discovery/leads.json` — never `git add -A`, because
sweeping the user's half-finished work into a commit about a dataset sweep is a real harm they would
find much later. And it refuses (**exit 1**, worked-and-the-answer-is-no) on a non-git tree, on a
record `.gitignore` excludes, and before there is anything to save. Running it twice is safe and
makes no empty commit.

**The commit history is itself a finding.** `leads.json` holds only the *current* status, so the one
thing it cannot show is access arriving — nine `unreachable` in July, six `verified` in August. That
trend lives in `git log -- discovery/`, which is why the default commit subject is the status counts
rather than a fixed string.

## Requires / suggests

- **Requires**: `project.json`. Nothing else — the entire point is that nothing has been declared
  yet. `resources.json` missing is a normal day-one state and is reported as a blocked source rather
  than an error.
- **Suggests**: `/data-check` to declare and census anything `verified`; `/data-collect` to pull it;
  `/ask-human` for anything `gone` or still only `claim`ed, while there is somebody to ask;
  `/resources` when `sources` says the blocker is credentials.

Per `references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: null`,
`execution: null` — a sweep is an observation, not an execution to resume, and the leads file is what
persists.
