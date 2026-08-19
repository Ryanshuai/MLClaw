---
name: evacuate
description: >
  Get everything off a machine before it is released or destroyed, prove it arrived intact, and
  store it as an ARA-shaped artifact — input (code + config) and output (weights, metrics,
  conclusions, the ablation graph). Trigger BEFORE any release or destroy: "这台可以关了",
  "释放机器", "跑完了把东西拉回来", "存到 S3", "销毁前要留什么", "训练结果拉全了吗",
  "artifact 拉回来了没有", "上次那台机器上的东西还在吗". Also trigger when a pull looks
  finished but nobody checked — a half-transferred checkpoint has a plausible name and passes
  every `exists()` check. Not for pulling DATA in (that is /data-collect) and not for deleting
  data against evidence (that is /data-retire). Releasing the machine itself stays /lease's;
  this is what makes that release safe.
---

# /evacuate — emptying a machine that is about to disappear

**This is the one place in MLClaw where doing nothing is itself the destructive act.**
Everywhere else a record written wrongly can be corrected later; here the lease ends, the disk
goes with it, and whatever was not pulled off is gone — with no `rm` in the log and nothing
raising anywhere.

The failure has happened several times and always in the same shape: training finishes, the
checkpoint transfers half-way, nobody looks, the machine is released. What is left is a `.pth`
with a perfectly plausible name that **will not open**, beside a metrics table missing its
tail. `os.path.exists` said yes the entire time.

And the two things that could have caught it were both looking elsewhere:

- `lease.py release` only verifies the **machine** is gone (`verified_gone`, the billing side);
- `pool.py release --artifacts recovered` simply **took the operator's word for it**.

## ‼️ This skill rests on one sentence

**Leaving a file on a machine you are about to destroy IS deleting it.**

So CLAUDE.md's *"Never delete a checkpoint outside `retention.py plan` → `apply` … Never
delete a file you cannot rank"* **applies here unchanged**. `plan` refuses to leave behind a
checkpoint that nothing ranked — the same stop condition, except that what performs the
deletion is "the machine disappears" rather than `rm`.

Want to drop something? Bring `--retention-plan <path>` and put **the thing that produced that
ranking** in the room. A list of filenames is not evidence here, exactly as it is not evidence
there.

## What it is stored as: an ARA

The artifact belongs to **`/ara`**; this only calls it — that is what the `bundle` verb is.
The five layers (`src` / `evidence` / `logic` / `trace` / `weights`, the last being the one ARA
does not have), `ARTIFACT.md`, and the reproducibility verdict all live over there, not here.

**Why it is not this skill's:** an evacuation's scope is **one machine** — which may hold
fragments of three rounds, or no artifact at all, plus a pile of files belonging to **no
artifact** (the `unclassified` bucket is the proof: `/ara` has no reason to carry them, and
here they must be carried, because the machine is about to be gone). And it is gated by a
**lease**. An artifact's scope is **one round**, with no deadline.

What is true instead: **the moment before a machine disappears is the last moment its source
can be read.** So that deadline *forces* the artifact to be completed — a call, not a
containment. The layering predicate is **imported** from `/ara` rather than rewritten here:
two classifiers would put the same checkpoint in `weights/` on one side and `src/` on the
other, and only the artifact would ever show it.

The one thing this skill owns and `/ara` cannot know is the **Transfer section of
`ARTIFACT.md`**: whether the bytes it names actually arrived.

## The order cannot be changed

The script is `<mlclaw_root>/scripts/evacuate/evacuate.py`, with eight verbs:

```
plan → freeze → push ────→ verify          → bundle → clearance   (+ status)
              └→ recover → verify --local
```

**`freeze` must come before `push`, and that is where the entire guarantee lives.** A manifest
taken after the transfer lists *what arrived* — which is a tautology, and by construction it
passes **every** partial transfer. The manifest is frozen **at the source**, and completeness
is computed against it. A `push` that runs before `freeze` is refused.

‼️ **Remembering to freeze once the machine is already gone gives `unverifiable`, permanently.**
What arrived cannot testify for what did not.

## Two destinations, and neither replaces the other

**`push` sends the bytes offsite; `recover` brings them into the project** at
`evacuations/<id>/recovered/`. The offsite copy is the one that survives this disk; the
in-project one is reachable without credentials and sits beside the record that describes it.

**Neither destination is a value you supply.** `--bucket` derives from
`resources.json -> aws.s3_bucket`, `--prefix` from `{project}/{evacuation_id}/`, and the
recovery path from the evacuation id. Both were free-form once, and `build_push_cmd` will
assemble `s3://None/` out of an omitted bucket and hand it to the aws cli. The full rule, and
what an invented destination costs: `<mlclaw_root>/references/layout.md` -> "Where a pull lands".

‼️ **Each is verified against the same frozen manifest, and recorded separately** — `verify` for
the offsite copy, `verify --local` for the recovery. One slot for both would let the second
stand in for the first: a local copy that arrived whole could clear a machine whose offsite copy
never went, and the record would say `verified` meaning something different than last time.

‼️ **The project directory is one disk, and the recovery now shares it with the workspace.**
`plan` reads free space against the manifest total and says so; `recover` **refuses** rather than
running out of room mid-transfer, because that produces precisely the right-name/wrong-length
file this skill exists to catch — and produces it silently. `--anyway` takes a partial copy
deliberately, which `verify --local` will mark `truncated`.

## Six arrival states, and `exists()` tells none of them apart

| | What it means | What can see it |
|---|---|---|
| `verified` | the hash matched | the hash |
| `size_only` | present, right length, **never hash-compared** | length. Catches truncation, misses corruption |
| `truncated` | present, **wrong length** ← **this is the half-pull** | length |
| `corrupt` | present, right length, **wrong bytes** | only the hash |
| `missing` | not there | — |
| `unverifiable` | **the destination did not answer** | nothing saw anything — **must never be written as `missing`** |

`size_only` is not laziness: an S3 ETag equals the MD5 only for single-part uploads, so it is
**precisely the large checkpoints** that come back with a hash that can never be compared.
Upload with `--checksum-algorithm SHA256` and `head-object` will return `ChecksumSHA256`. When
it cannot, the answer is `size_only` — which is **not** `verified`.

## Clearance: three verdicts, computed rather than declared

**One clean destination clears the machine, not both.** The gate is *the bytes are somewhere
that was read back against the frozen manifest* — holding a box because the second copy is still
running would be paying for a machine over a redundancy, and a project with no bucket could
never clear at all. Every destination's state is reported regardless, and clearing on a single
copy says so: a verdict that hides which copy is broken is how one surviving copy gets mistaken
for two.


| verdict | When | Who reads it |
|---|---|---|
| `clear` | every file hash-verified | `lease.py release` / `pool.py release` |
| `clear_size_only` | all present, all lengths right, N never hash-compared. **Cleared, with the gap stated** | same |
| `blocked` | any missing / truncated / corrupt / unverifiable / unranked / **cited but not moved** | exits 1 |

There is no fourth verdict meaning "probably fine". **When `clearance` exits 1, do not release.**

### Cited but not moved — nothing else performs this join

A conclusion cites `stages/training/runs/run_X`. Destroy the machine and **the citation does
not break** — it still resolves inside `conclusions.json`, that conclusion still reads as
sound, and only weeks later does it quietly become `unverifiable`. This is CLAUDE.md's *"Never
delete data a frozen snapshot still names"* seen from the model side — **and on the model side
there is no `retire.py` that knows about it.**

Three cases count as safe: it is already in the local project (the common one — do not raise
on it), the source root *is* it, or the manifest covers it. Everything else is reported for a
person to look at.

## Reproducibility is **read**, not claimed

`code_snapshot.py` computed `code.reproducible` at launch; this only **reads** it. `false`
means something specific: a changed file was too large to embed, so `git checkout && git apply`
rebuilds **a different tree**.

‼️ **This never blocks.** Losing bytes is far worse than an imprecise label, so everything is
still moved — but that verdict is printed on the first screen of `ARTIFACT.md`, the same rule
as a census recording `complete: false` rather than withholding itself.

A bundle whose `src` layer is empty gets named as such: that is a **backup**, not an artifact,
and nothing in it says what actually differed between two arms of an ablation.

## Three things it does not do

- **It does not release the machine.** That is `/lease`'s. This skill only makes that release
  safe.
- **It does not move bytes.** `aws s3` does that — the same division as `/data-collect`: this
  decides what must go, freezes what that was, and rules on what arrived.
- **It does not delete the source.** Moved ≠ emptied. Deletion goes through `/data-retire` or
  `retention.py`.
