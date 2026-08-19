# Compute: capacity, GPU model, parallelism, release

‼️ **In MLClaw this layer already has an owner.** Renting a machine is `/lease` (seven verbs
plus history), holding N of them for one search is `scripts/shared/pool.py`, and the layering
rules are in `<mlclaw_root>/references/fleet.md`. **Read that one before opening an arm, not
this one.**

What is left here is the half `fleet.md` does **not** cover — judging capacity, rather than the
mechanics of holding it:

| This file | `fleet.md` |
|---|---|
| The stable-capacity inequality: an arm whose "preemptible residency < checkpoint interval" produces **structurally zero** | how a slot is taken and returned, who sends the heartbeat |
| The four **silent** capacity failures (paging truncation, empty set, quota edge, region drift) | owned-before-rented, why preemptible is the search default |
| GPU selection is decided by **clock ÷ price**, not FLOPS | what a partially finished sweep is worth |
| ‼️ Arms compared within one group **must be the same GPU model** (a different card is a different kernel); to change it, change the whole group | a preempted trial **must never be read as a refuted hypothesis** |

The last row states two sides of one fact, and both must be honoured: `fleet.md` says
preemption is not evidence, and in this file `graph.py close --killed-by` has no death called
"preempted" — such an arm is **re-queued**, and the card stays `running`.

The output ceiling of an ablation round is often decided not by its design but by **whether
GPUs can be held stably**. This file is about judging that, orchestrating around it, and
confirming things were really released.

‼️ **Every number here is a criterion to be re-measured each time, not a constant.** The same
pool can be "zero preemptions in six hours" in the morning and "preempted every 28 minutes on
average" in the afternoon. Check the date before trusting any figure below.

---

## 1. Three numbers to measure before starting

| Quantity | How to get it |
|---|---|
| **Stable residency** | count intervals between PREEMPTED entries in the watchdog log (this repo: `state/watchdog.log`) |
| **Time per epoch** | compute directly from `wall_time` in tfevents, dropping the first 20% to exclude warmup |
| **Checkpoint interval** | read the training script — commonly it writes **only on epoch boundaries** |

### ‼️ The inequality: residency < checkpoint interval ⇒ **output is structurally zero**

Not "slow progress" — **progress is mathematically impossible**. Every preemption discards the
whole unfinished epoch, whether or not a checkpoint file exists.

Measured in this repo (2026-08-14, L40S preemptible pool): residency **24–36 min** against
**0.73–1.06 h** per epoch. Of seven arms, the ones preempted every 28–50 minutes **never wrote
a first checkpoint**, while `e0b/e1/e3/e5` were preempted **zero times**.

‼️ **The second finding matters as much: all the churn happens among the arms competing for the
leftover slots, and whoever holds a slot keeps holding it.** Which implies: **extra arms do not
merely produce nothing themselves — they churn the arms that were already stable.** So
parallelism above stable capacity is a **total loss**, not "the queue moves a bit slower".

---

## 2. Four ways capacity fails, none of which raises an error

| Failure | Symptom | Criterion |
|---|---|---|
| **Silent no-room** | `create` succeeds → `STARTING` → **`STOPPED`**, with no error | check `capacity resource-advice` beforehand; treat a `LOW` report as unobtainable |
| **`available` is an instantaneous value, not a quota** | you see `available: 1 / limit 32`, and minutes later it is 0 | somebody else will take it. **Seeing it available ≠ you can get it** |
| **A preemptible pool cannot be converted in place** | `instance update` has no preemptible flag at all | a non-preemptible box is always a **new box with an empty disk** ⇒ an arm whose checkpoint is on the old disk either restarts from zero or waits for the old box to free up so it can be moved |
| **GPU model not enabled** | the pricing API returns `NotFound` (where a working model returns a price) | **no price = not enabled**, not a missing config. Combined with the compile-time arch ceiling, you may get one and still not be able to run on it |

---

## 3. Choosing a GPU: the criterion is **clock ÷ price**, not FLOPS

Measured in this repo (2026-08-14, single card, identical code / data / config):

| | h/epoch (no CDN / CDN) | $/h | relative cost per epoch |
|---|---|---|---|
| L40S | **0.73 / 1.06** | 2.14 | 1.0× |
| H100 | **0.92 / 1.13** | 3.85 | **2.3×** |

**The more expensive, more powerful card is slower** — by 1.26×. The cause was measured: the
H100's SM clock is pinned at **1980 MHz** against the L40S's **~2520** boost, a ratio of
**1.27×**, matching the observed 1.26×. Meanwhile the GPU reports 92–100% utilisation while
drawing only **209–274 W of 700 W**, and load average is 1.68 at `nproc=16` (**so it is not
data-loader starvation**).

⇒ **This workload is clock-bound**, so neither FLOPS nor memory bandwidth is a selection
criterion. **Changing card buys no speed — it only buys "will not be preempted"**, which may
still be worth it, because an arm producing nothing is wasted however cheap it was.

### ‼️ A hard gate: compile-time arch

`TORCH_CUDA_ARCH_LIST` decides which arch custom operators are compiled for (this repo:
pointnet2). **sm_89 vs sm_90 means a different card is a different kernel**, and every
conclusion here is a difference.

⇒ **Arms that will be compared against each other must be on the same GPU model. To change it,
change the whole group.** Cross-model deltas are not comparable — a harder rule than capacity,
because capacity only decides whether a run finishes, while this decides whether the finished
number counts.

---

## 4. Orchestration rules

1. **Offline nodes (0 GPU) are not capacity-limited** — always first, always fully
   parallelisable. When the queue has "nothing to do", the right move is to push the offline
   batch, not to open another arm.
2. **GPU parallelism ≤ stable capacity** (the inequality in §1). Exceeding it is a total loss.
3. **One GPU model per comparison group** (§3).
4. ‼️ **The noise floor blocks no arm.** It does **not** have to run alongside the main arms —
   the run card pins code, data and config, so measuring it days later is equally valid. The
   noise floor only decides the wording of a reported number (`[T1 trend]` vs "measured").
   This repo once treated it as a precondition and believed the whole round was stuck.
5. **Declare the code snapshot hash at the moment each arm starts.** Run things serially and
   you know when you changed the code; **run them in parallel and "the current code" is
   ambiguous** — several arms may not be running the same thing, and nothing raises.

---

## 5. Release: **do not treat it as a wrap-up step**

‼️ **"Walk a checklist at the end of the round" is itself the bug.** A checklist needs somebody
alive to execute it, and the occasions when a machine is left running are precisely the ones
where **nobody is present**: the session ran out of context, you walked away, the arm crashed
on its own. Billing does not require anyone to remember. So the cloud-side sweep has been moved
out of the checklist and given to a resident process:

**`net.miniclaw.gpujanitor`** (mac-mini, every 15 minutes, read-only) → results land in
`~/.claude/miniclaw/gpu_janitor.json`. **Your job is to read it, not to run it.**

```bash
python3 -c 'import json;d=json.load(open("'"$HOME"'/.claude/miniclaw/gpu_janitor.json"));print(d["generated_at"],d["ok"],len(d.get("running",[])),d.get("alerts"))'
```

‼️ **Check `generated_at` first.** Nothing for more than ~45 minutes means the janitor died or
was never installed (that timestamp *is* its liveness signal; a second layer of monitoring is
deliberately absent). Only then fall back to doing it by hand: `nebius_scan.py --running`
(walking every project) plus `nebius_audit.py` (the audit log, the only source that can answer
"was it actually deleted"). The criteria live in the `nebius_server` document and are not
repeated here.

**What a person still has to do at the end is only the two things the janitor cannot see** — it
can enumerate cloud resources, and neither of the following exists in a cloud API at all:

| | What to check |
|---|---|
| checkpoints | anything worth keeping has been moved somewhere durable (a preemptible disk disappears with its instance, and **a rolling file is overwritten by the next epoch** — the dangerous case is an arm still RUNNING, not a stopped one) |
| run cards | each arm's code hash and data hash are written to disk — **this is the only thing that makes "measure the noise floor days later" valid** |
