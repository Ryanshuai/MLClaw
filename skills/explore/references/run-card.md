# The run card: how one experiment's conditions get recorded — and which parts MLClaw already covers

`SKILL.md`'s "experiment record standard" points here. **Write the card before the run; after
it, only results may be appended.**

‼️ **In MLClaw this card IS `run.json`. Do not build a second one.** An arm is an ordinary run
started by `/train-run` or `/eval-run`, and its `run.json` + `config_snapshot.json` +
`sources.json` + `code_dirty.patch` already are the card. The source project's
`scripts/runcard.py` reimplemented this layer in the absence of MLClaw — the two converged
independently on the same set of fields, so this document's value now is to **point at what
MLClaw still lacks**, not to build it a second time.

The seven items are compared one by one below. ‼️ **The three ⚠️ entries are genuine gaps: fill
them yourself before opening an arm, and do not assume MLClaw will stop you.**

‼️ The main document keeps only the judgement (why this must be recorded, and the one-line form
of the four hard rules); **the format and the measured lessons live here in full**.

---

‼️ **If an experiment's conditions live only in a chat log, it is not an experiment. It is an
anecdote.** Once there are several arms, several rounds, and **several different orderings**,
"why is this number different from that one" can no longer be answered from memory — and that
is precisely the question debate ① exists to adjudicate. **A debate with nothing to cite
produces opinions.**

The rule: **one card per run, written before the run, results appended only afterwards.** The
tool is `scripts/runcard.py` (`declare` / `event` / `attach` / `check` / `codediff`).

#### The seven things a card must carry, and why a command line is not enough

| What is recorded | MLClaw | Why the command line alone will not do |
|---|---|---|
| **The fully resolved config** | ✅ `config_snapshot.json` | Defaults drift. This project lost a result to it once: the eval script defaulted to 40000 points while the weights were trained at 145000, and the only symptom was one bad number. **A flag not written on the command line is a flag not recorded at all** |
| **Code identity, including uncommitted changes** | ✅ `code_snapshot.py` — `origin_commit` + `code_dirty.patch`, **untracked files included**, and `reproducible` states the outcome | ‼️ Nobody commits while a round is in flight, so **the git sha points at a tree that does not exist**. Hash the bytes that actually ran |
| **Data identity, not a path** | ✅ `datasets/<id>@<snapshot>` + `sources.json` | Paths get reused and contents get regenerated. Today's `box9dof/` is a levelled export whose npz format changed by a factor of 110, under an identical directory name |
| **Environment** | ✅ the env snapshot in run.json | Compiled artifacts are arch-specific: an sm_90 kernel will not load on sm_89. "It runs here and not there" has to be answerable |
| **Comparability fingerprint** | ⚠️ **partial** — `mode` + `scope` + `list_runs.py --comparable` compare the data and the convention, **not the metric script itself**. Two runs whose metric script differs still look comparable to MLClaw | see below |
| **Interruption / restart history** | ⚠️ **partial** — `pool.py` records `preempted`/`crashed`, and `workload` (declared) against `scope` (actual) exposes a difference, but there is no segment history saying "these 20 epochs were four sittings". ‼️ `/train-triage`, which would read it, **is not built** | On preemptible machines "20 epochs" may be four interrupted attempts. **An epoch count is not a training history** |
| **What was NOT run** | ⚠️ **absent** — the data side has `census.complete: false`; the run side has no counterpart. ‼️ Cut arms, sampling and top-N all depend on you leaving a card in `graph.json` | Silent truncation (top-N, sampling, cut arms) reads as full coverage |

#### Four hard rules

**1. ‼️ The delta must be computed, not described.** — **`graph.py check` now asserts this.**
Write `delta` on the card; check compares it against the run's `lineage.variation_summary` and
refuses on one extra key.

‼️ **It compares two fields, not one.** One of the drifts measured on 2026-08-14 was **8 GPUs →
1 GPU**, and `world_size` lives in `workload`, not in `runtime_params` — so a guard reading only
`variation_summary` would have **cleared the very round it exists to catch**, which is exactly
the "a rubber stamp is worse than no stamp" shape.

MLClaw computes it: `lineage.variation_summary` is the fork's actual `runtime_params`
difference against its base. ‼️ **But it does not assert that this difference equals the one you
declared.** The source project's `check` does make that assertion, and it caught a real case:
declared `[iou_aware_cls]`, actual `[dn_rot_noise_deg, iou_aware_cls]`. In MLClaw that step is
yours — before opening an arm, put `variation_summary` and the card's `delta` side by side.
(The principle: `check` diffs two cards' configs and asserts the result equals
`declared_delta`.) The prose says "this arm added CDN"; the config diff says this arm **also
picked up a default somebody moved last Wednesday**. Only one of those is checkable.
**More than one key of difference is not a controlled experiment, and no result can be
attributed to any one of them.**

**2. ‼️ `parent` is not `warm_start_from`.** MLClaw keeps them apart: `lineage.fork_of` is where
the configuration came from, `lineage.parents` are the artifacts consumed, and **the card's
`parent` is the attribution baseline** — three different things.

Two relationships, and confusing them is a real error: an arm may warm-start from run X while
its correct comparison baseline is run Y. `parent` = who it is read against;
`warm_start_from` = where the weights came from. **A run with no parent cannot be attributed.**

**3. ‼️ Comparability fingerprint = hash(data identity + metric script + thresholds).**
Two cards with different fingerprints **may not be compared**, and `check` refuses outright.
‼️ **MLClaw only does half of this**: `list_runs.py --comparable` covers data identity and
`scope`, and **does not cover the metric script or the thresholds**.
`stages/exploration/config.json -> fingerprint` is the slot reserved for it, and filling it in
still leaves nothing checking it — so the moment you change the metric script, void the old
curves yourself. This is the machine-executable form of two rules this project paid for:
*change the metric and every old curve is void*, and *comparing across sources requires one
convention, or what you measured IS the convention*. **Both used to be remembered, which is to
say not enforced.**

**4. ‼️ Store the bytes, not only the hash — and do not weigh storage cost.**
‼️ **MLClaw stores a patch, not a tarball**, and its reproduction contract is
`git checkout <sha> && git apply`. The two are not equivalent: a patch depends on that commit
still existing. After a force-push, a deleted branch or a GC, `checkout` fails where a tarball
would not. `/repro`'s `not_reproducible` (some axis `gone`) is the name of that failure — it
will **say so**, but saying so is not recovering it. For long-lived arms, keep a separate copy
of the bytes.

A hash only answers "these two differ"; **only a copy answers "where"**, and six weeks later
the second is what you need (by then the working tree has long since moved, and what you need
to see is precisely how it looked before). `codediff` reconstructs from two tarballs
independently, so **the current working tree is irrelevant to it**.

An arm costs tens of dollars of GPU time and a snapshot is a rounding error on that;
**storage should never enter this decision, and when in doubt store more**. Measured: 99 files,
414 KB — and not only `.py`: the shell launcher (half the hyperparameters actually live in
`train_anyware_9dof.sh`), CUDA sources, the lockfile and the configs all belong in it.

‼️ **The snapshot's file list must come from a filesystem walk, never from git.** This has been
changed twice, and both times were paid for:

The first version used `git ls-files` (tracked only): 59 files against 63, and among the four
missed was `models/denoising9d.py` — **the entire implementation of the technique under test**,
because nobody commits while a round is in flight. So it became
`git ls-files -co --exclude-standard`.

**The second version was worse, and it was wrong on every machine.** The training machines were
populated by `rsync --exclude '.git'`, so they **have no `.git` at all**, and git therefore
listed zero files **on every machine that ever actually ran an experiment**. Measured across six
boxes on 2026-08-14: `worktree_sha = e3b0c44298fc1c14` (the SHA-256 of the empty string) and a
`code_snapshot.tar.gz` of 63 bytes — an empty tar. `check` compared two empty hashes against
each other and reported **"same code"** — the one sentence it exists to prevent.

#### ‼️ The record layer breaks on its own, and it breaks more insidiously than the experiment does

Over one round I stepped on more traps in the **recording code** than in the model code. Four of
them, none of which raises:

| Failure | Symptom | Why it is dangerous |
|---|---|---|
| **Enumerating the empty set** | the hash is `sha256("")`, identical for every run | The guard reports **the very conclusion it exists to exclude**. A rubber stamp is worse than no stamp, because a stamp gets believed |
| **Absolute paths in the digest** | identical bytes hash differently under `/home/shuai` and `/home/ubuntu` | False positives. It reads as "a code difference was found", and sends somebody hunting a difference that does not exist |
| **Fixing the tool mid-round** | fixing `_sha_files` silently re-bases **every** hash the tool produces; stack an older card predating a field split on top of that | `check` reports "DATA DIFFERS — everything downstream is incomparable" while that manifest is md5-identical across all six machines. **A guard that alarms because of its own version drift is a guard people learn to ignore** |
| **Treating a page as the whole** | `instance list` returns 10 per page, so 13 machines shows as 10 | The fleet view loses the **baseline**, and a truncated list looks exactly like a complete one |

The countermeasures, all four required:

1. **The empty set is a bug, not an identity.** Below a file-count threshold, `SystemExit`
   outright; never write a card that hashed nothing.
2. **The digest consumes content only**; when a name must participate in identity, consume the
   **repo-relative path** (so a rename is visible and moving the whole tree is not).
3. **When the tool changes, re-derive every card's derived fields with that one version** (the
   original values stay in `superseded`, and only **identity** is re-derived — never `result` or
   `purpose`). Verify byte-for-byte that the underlying files really are identical **first**, and
   only *then* re-book them uniformly; the other order uses uniform bookkeeping to hide a real
   difference.
4. **Every "list all X" cloud API call must handle `next_page_token`**, behind a wrapper rather
   than called directly all over the place.

#### ‼️ A completely correct test can give completely wrong confidence

More insidious than a test that passes vacuously: **the test is right, and the half it verifies
is not the load-bearing half.**

`tests/test_one_to_one.py` pins the F1 arithmetic beyond reproach — repeat 5 leaves 1 negative
at G=51 and 0 at G=52, repeat 2's zero is at 128, and it reads the mask off the **real matcher**
through a spy rather than reimplementing it. Not one assertion is wrong, and the file header
even says "these assertions are cheap, and they are the floor the whole round stands on".

**The floor has two halves**: the arithmetic half is pinned, and the **"does real data's G ever
reach 52"** half is untouched — because the test constructs G itself via `_batch(n_gt)`, so it
stays green whether the real corpus has G of 25 or 250.

The rule: **when writing a test for a mechanism, write its input distribution as an assertion
too, against the real corpus.** Something of the form
`assert (G >= 52).mean() > 0.2, "F1's premise does not hold on this corpus"` — that goes red
before a machine is even started, rather than being reported to you by results after thirteen
machines have run for six hours. **"Is the formula right" and "what range do the formula's
inputs fall in" are two propositions, and testing the first is not testing the second.**

#### ‼️ Tests written for these traps also pass vacuously

After writing a regression test for the first trap above: 11 green. Putting the old
implementation back gave only 6 red. Going to look at why the other 5 stayed green — two of them
**passed empty against empty**: the old implementation enumerated zero files, both sides were
`sha256("")`, "equal" held, **and the reason it held was the exact fault the test was written to
catch**. With a built-in control added (assert `n_behaviour_files > 20` before comparing hashes)
it became 8 red.

**This is Stage 6's "the criterion selects itself" applied to tests** — the original was about
metrics, and the same statement holds for tests: **the criterion's failure mode satisfies the
criterion.** So a test for the record layer must be verified **both ways**: green against the
fixed implementation, and **red against the broken one, with the count of reds checked**. Fewer
reds than expected means going to look at whether the missing ones passed vacuously.

#### Debates must cite run ids

In the adjudication records of ⑤ and ①, every claim and every number must point at a run id.
**A claim that cannot point at one may not enter the adjudication table** — nobody next round
can re-examine it.

➜ Where it lands: the run card travels with the run's output directory (same reasoning as
`findings.json`: it has to be read and diffed by scripts). Adjudications and proposals continue
to go into `model_design.md`.
