# Identity — how many kinds of id, and who maintains them

Outcome of a four-position debate with cross-rebuttal, adjudicated against the code rather
than against principle. It is kept for the reason `roadmap.md` is kept: every entry below is
a decision that would otherwise be re-argued from scratch, and several of them were argued
wrong the first time by the person who opened the question.

Nothing here is a contract yet. The normative half — event vs entity, the two syntaxes, the
add-a-row-to-both rule — belongs in `layout.md -> "Dataset identity and census records"`,
which is already in `contract_docs.py`'s `DOC_FILES`. Putting it there is what makes
`contract_identity.py` admissible with no change to the contract harness. **Do not admit
`roadmap.md` to `DOC_FILES`** — it is by construction the document of things with no code,
and admitting it converts `contract_docs.py -> report()` from "how much of the record layer
is checked" into "how much design work is outstanding."

## The rule, and the thing it cannot see

The rule the debate started from:

> **An id's boundary is the boundary of what you can deterministically reach holding it.
> Where you cannot reach is where the next id begins.**

Deterministic means three things at once: the answer is unique, it does not depend on when
you ask, and it does not depend on whether a third party has acted. Reachability is
**directional**, and the id goes to the side you cannot reach.

It settles most cases correctly. It says `handoffs/h1 -> rounds/round_2` needs no id
(always there, always the same), that `boxes -> v3` does (which v3? "the latest" moves), that
`data_resolved.jsonl` needs none (regenerable from snapshot + `dataset.json` + `--at`), and
that `run.json -> steps.*` are sub-events under a stable parent rather than names of their own.

**And applied literally it rejects `models/<id>@<release>`, the one kind nobody disputes is
missing.** From `training/run_X`, `outputs.best_checkpoint` is a unique, clock-independent,
third-party-independent path. The rule says: reachable, no id needed. It is wrong, and the
reason is the correction the whole debate turned on:

> **Reachability is blind to the deleter.** `retention.py` removes that file on its own
> correct terms and — `skills/repro/references/axes.md` — "has no idea who cited
> them." A path that resolves today and is deleted tomorrow by a process that could not have
> known better was never deterministic; the rule just cannot see the future actor.

So the operative test is narrower than the rule, and it is what every admitted kind below
passes:

> **An id is earned where something can be independently destroyed AND an irreversible
> operation must ask "who still cites this" before acting.**

`cited_by_snapshot` (`retire.py`) is the only reverse-edge exclusion in the repo today. The
missing `cited_by_release` is its twin, and it is the whole justification for kind #4.

**The rule is also not computable, and no contract can make it so.** "Deterministic
reachability decides the boundary" is a judgment about the world, not a property of a string.
A check can assert that a bare `datasets/boxes` is refused. Nothing can assert that `datasets`
deserved to be an entity in the first place.

## The verdict: 4 + 3 + 4, and zero machine ids

The debate's central error — made by three of four positions and by the question as posed —
was treating "has an id" as one category. It is two, and collapsing them produces the worst
failure on the board (below).

### Tier 1 — the citation grammar (4)

These may appear in `run.json -> lineage.parents`. The repo mints exactly two `cite_as`
strings today, both printing *"cite in run.json -> lineage.parents as:"*, with runs matched by
complement.

| id form | kind | why it is earned | present |
|---|---|---|---|
| `<stage>/<run_id>` | event | the majority citation kind; uniqueness guarded by `create_run.py` + `contract_run_record.py`, behind `run-mechanics.md`'s "a run_id identifies exactly one run" | ✅ |
| `datasets/<id>@<snapshot>` | entity + pin | the paradigm: minted with a `cite_as`, parsed, refuses id reuse in words (`census.py`, *"an identity is never reused"*), **and consulted by the repo's only reverse-edge exclusion before an irreversible act** | ✅ |
| `handoffs/<handoff_id>` | event | admitted **despite failing the reverse-edge test** — there is no `cited_by_handoff` anywhere — because it is a boundary to a party MLClaw does not control, the one thing no internal record can be reconstructed from. Status-checked on dereference: a run downgrades to `drifted` when the handoff is no longer `accepted` | ✅ |
| `models/<id>@<release>` | entity + pin | **the earned gap.** Pins the checkpoint's **content hash**, not its path | ❌ |

### Tier 2 — scoped-resolvable names (3)

Dereferenced by id, with dangling-reference refusals, but **never in `lineage.parents`** and
with no `cite_as`. Keeping this line is not pedantry — see "the worst string in the grammar".

| name | dereferenced where | note |
|---|---|---|
| `census_<ts>` | `phase.py` builds `census/<census_id>.json` and errors `no census <id>`; frozen into `snapshot.json -> from_census`, the retire plan, and the deletion record — the only artifact that outlives the bytes | minted as a bare f-string, **no collision guard** |
| `window_<ts>` | the best-guarded dereference in the repo: `collect.py` builds the path from the cited id and **refuses the pull** when it does not resolve — *"a denominator that is not on record is not a denominator"* | separated from census by retakeability: *"a reading can never be retaken"*, a census can be re-scanned. Carries three distinct times no parent-plus-timestamp address can encode. Same missing guard |
| `lead_<nnnn>` | `discover.py` resolves it, raises on a dangling reference, and cross-checks the citer's `match` against the cited `status` — more machinery than any other non-lineage name | **worst defect on the board**, below |

### Tier 3 — borrowed, never minted (4)

`code.origin_commit` (git sha) · `code.framework_version` (PyPI spec + `RECORD` sha256) ·
wandb / mlflow run ids · email / `channel_ref`.

Two of these are already **content-addressed**, which is the precedent for the release hash:
today MLClaw can verify code byte-for-byte and cannot verify weights at all.

## Rejected, and why — so it is not re-argued

- **`resources/<key>@<pin>` — a machine.** Refuted by grep: `retire.py` and `census.py` never
  open `resources.json`. The server string goes straight into `["ssh", …]` as an OpenSSH
  destination resolved by `~/.ssh/config`, `/etc/hosts` and DNS — none of which MLClaw owns or
  can pin. And `resources.json` is never committed, so a pin does not survive a clone, and a
  handover is a clone. `rig.py` contains executable code refusing exactly this construct.
  **The hazard is real and the instrument was wrong**: `server` is cited bare from five
  template sites and the schema records no edit. The fix is a **host fingerprint measured at
  plan and re-measured at apply** — a fact, not a name. See build step 8.
- **tune `session_id`.** The field exists, but every use is a filter value and its
  load-bearing use is `is None`. Nothing resolves a session to a record. That is a tag, and
  `lineage.local_tags` / `pipeline_tags` are already the tag mechanism.
- **`asks/<ask_id>`.** Zero citers outside its own skill and its own contract. That nothing
  points back at an ask is a **real defect** — a value that came from a person has no
  provenance link — but the repair is to add the citer, not to declare the kind.
- **`rigs/<rig_id>@<config_pin>`.** `rig.py` refuses it in code and says why: watch a cheap
  proxy, read the expensive truth live. A `@config_pin` stores exactly the fact class that
  file exists to keep unstored.
- **`leases/<lease_id>`.** `leases.json` sits beside `resources.json`, which is never
  committed — a citation form whose resolution target cannot be committed is not a *risk* of a
  dangling citation, it is a certainty on the first clone.
- **`retire_id` / curate `plan_id` / repro & triage `session_id`.** Equality keys, not
  dereferenced references — no path is built, nothing is opened. Counting minted names is
  unbounded by construction: `from_census`, `confirm_token` and `taken_under.identity` all
  pass the same test and nothing stops the count at 16.
- **Deployment binding.** Given `(resource, instant)` the binding is uniquely reachable, so it
  is a **row in an interval table, not a node**. Deploy introduces no identity of its own; it
  is the forward direction of a release's, plus a duration.

## The worst string in the current grammar

A run citing `datasets/boxes/census/census_<ts>` — reachable today by **one typo**, no new
kind required — fails silently in *both* directions: the data probe rejects it for lacking
`@`, the artifact probe rejects it for the `datasets/` prefix, and `/repro` reports
`citations: 0, "the run cited no frozen dataset"` and advises the user to start citing.
**A run that did cite is told it did not.** This is why fixing the parser outranks adding any
kind, and why tier 2 must stay out of the citation grammar.

## Who maintains it: library + contract

Not a skill, not a service.

`lifecycle/scripts/shared/cite.py` — one parser, one `KINDS` table, one `cited_by` walk — plus
`contracts/contract_identity.py` citing `layout.md`'s identity section. The repo has run this
exact play four times (`shared/compare.py`, `shared/_records.py`,
`contract_candidate_match.py`, and `contract_docs.py`'s grep that *forbids a hand-rolled
duplicate of a shared module*, which is the precise enforcement shape "no second citation
parser" needs). `_dataset_paths.py` states the doctrine: *"Three implementations of it is
three answers waiting to differ."*

**The service loses on a verified counter-precedent**: `run-mechanics.md` — *"There is no
`runs_index.json` cache. The source of truth is the run.json files themselves"* — deliberate,
to avoid drift after rsync and manual deletion. An index answer also carries an `as_of`, which
makes it a **claim**, and `retention.py` / `retire.py` must then re-verify from records and pay
the full walk anyway. It cannot serve the one query that would justify it.

**The skill is not a competitor — it is what ships with kind #4.** Its genuine claim stands:
only a reader can tell a hardware revision from a correction to a value that was always wrong,
byte-identical diff, opposite consequences. That judgment belongs in a `/model` skill. Its own
champion conceded a skill "cannot enforce anything."

### What library + contract cannot cover

1. **It cannot make a missing kind exist.** `models/<id>@<release>` has been absent for the
   entire life of this repo with zero checks red.
2. **It never sees a user's records.** Contracts run over the tool repo; real projects live
   under `workspace_root` and CI has no access. This compounds with the fallback rule: an
   agent that has decided to fall back can hand-write `datasets/boxes` with no pin, and no
   check will ever run on that file.

**The gap is covered by the conversation-start scan**, the one surface that already sees the
user's corpus. `cite.py doctor --project <P>` is a fifth entry of exactly the existing shape:
records-only, no network, reports parents matching no declared form and entity cites with no
pin. Same split the repo already draws between `census.py` (goes and looks, may be partial)
and `dataset.json` (the durable contract).

## Build order

Steps 1–3 and 5 are **live bugs found while adjudicating**, not preparation for future work.

0. **Move the rule into `layout.md`**, extending "Dataset identity and census records".
1. **Fix `repro.py`'s parser before adding any kind.** A parent `models/detector@r3` splits
   into two slash-parts, so `resolve_run_ref` accepts it as `stage=models`, never reaches the
   `unverifiable` refusal, and the run reports `gone` → `not_reproducible`. In `/repro`'s own
   vocabulary `gone` means *looked, not there* — about a record type it has never heard of.
   **A well-formed citation of a new kind fails harder than a malformed one** (`gone` outranks
   `unverifiable`). Unknown prefix → `unverifiable`, always; reject a ref containing `@`.
2. **Fix `tag_lineage.py`.** With `parents: ["training/run_A"]` — the only form anything
   writes — it raises `TypeError` *after* the tag was already saved, leaving the target tagged
   with zero ancestors propagated: the half-applied state `pipeline_tags` exists to prevent.
   The uncaught exception exits 1, which the fallback rule reads as *"the script worked and the
   answer is no"*, so a crash passes through as a legitimate refusal. Dead for the entire life
   of the string form, with a green suite.
3. **Ship `cite.py` + `_comment_parents` in `run.json` + `contract_identity.py`.** `parents`
   and `fork_of` are the only fields in the lineage block with no comment. Migrate the six
   readers. Budget `build_dag` as real work — it starts *seeing* dataset and handoff parents
   for the first time and its HTML has no column for them.
4. **Collision guards** on the three tier-2 names. `lead_id` first, and not a tie: it is a
   positional counter over array length with dedup on path only, so removing one lead by hand
   makes the next mint **reuse a live id** — reconcile then resolves a stale candidate to the
   wrong lead, finds it present, and raises no drift. *An id that resolves to something else
   beats an id that resolves to nothing.* Then `census_id` and `window_id`, where two
   same-second scans overwrite silently.
5. **Resolve the phantom.** `lineage.parent_checkpoint` is read by `baseline_delta.py` and
   asserted by six cases in `contract_baseline_delta.py`, and **does not exist in `run.json`** —
   written by nothing. Declare it or delete it with its six cases. It is kind #4's absence
   improvised at a call site, green in CI.
6. **Build `models/<id>@<release>`** — content hash, not path — with `cited_by_release` as the
   second caller of `cite.py`'s shared reverse-edge helper.
7. **`retire.py plan --census <id>`.** Today `plan` always takes the latest census and refuses
   a partial one with *"this refusal has no override"*, so a partial rescan after a good scan
   **deadlocks the operator permanently** — full capture rig, no route out but deleting a
   census record by hand. `phase.py assess` already accepts `--census`.
8. **Host fingerprint, not a pin** — measured into `retire.py plan`, re-checked at apply, on
   `rig.py`'s tripwire shape. It must **block** here, unlike `rig.py`'s own tripwire which
   deliberately only warns: non-blocking is right for a capture operator who cannot re-shoot
   the frames, and wrong for a script whose next statement is `rm -rf` over ssh.
9. **Add `cite.py doctor --project` to "On Conversation Start"** as a fifth entry in the
   existing single-pass scan.

## The strongest surviving objection

**Kind #4 was certified on a criterion census also meets on the delete path, and census was
then demoted.** `retire.py` names a census by id in two refusals that stop a deletion and
freezes `from_census` into the deletion record. The escape is that `plan` resolves the census
by *recency*, not by id — so the id is a provenance label there rather than a lookup. That
escape is thin, and **step 7 above breaks it**: the moment `--census <id>` lands, a census is
resolved by id on the irreversible path.

What survives if it does: census still never enters `lineage.parents`, still has no `cite_as`,
and still must not — the failure above shows what admitting it to the citation grammar costs.
The count holds at 4; the tier-2 justification weakens from *"not resolved on the irreversible
path"* to *"not in the citation grammar"* — thinner, still real.

Second, smaller: `handoffs/<hid>` is admitted while conceding it fails the reverse-edge test,
justified on external-boundary grounds — two criteria, and a fair reader can say each was
applied where it admitted the wanted answer.
