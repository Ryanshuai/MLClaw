# chain.md template (full worked example)

This is the canonical layout `/train-tune-report` should produce. All sections required.
Empty sections rendered with "—" (don't omit).

---

```markdown
# Train-tune session: 2026-04-28_lr_search

**Best**: trial_15 (val_acc=0.972) | **18 trials** | **22h wall** | **converged**

## Headline

- lr 是主导轴。best lr=2.5e-4，区间 [2e-4, 3e-4] 内差异 < 0.1%
- warmup_ratio 弱信号：0.03-0.05 几乎一样
- weight_decay 在测试范围内无信号

## Best-so-far curve

```
Iter:   0   1   2   3   4   5   6   7   8   9   10  11  12  13  14  15  16  17
Val:   .965 .963 .968 .969 .969 .971 .972 .972 .972 .971 .971 .972 .971 .972 .972 .972 .971 .972
       ▁   ▂   ▃   ▄   ▄   ▅   ▆   ▆   ▆   ▅   ▅   █   ▅   █   █   █   ▅   █
```

## Coverage map

| Axis | Range tested | Trials | Best @ | Coverage |
|---|---|---|---|---|
| lr | log[1e-5, 5e-3] | 8 | 2.5e-4 | ✅ well-covered |
| warmup_ratio | [0, 0.1] | 4 | 0.03 | ⚠️ sparse but signal flat |
| weight_decay | log[1e-6, 1e-2] | 3 | 1e-4 | ⚠️ sparse, no signal |

## Decision timeline

### Iter 0 · [baseline] · trial_0
- **Hypothesis**: 建立基准. lr=1e-4 (领域常识默认)
- **Diff vs base**: — (no fork)
- **Outcome**: val_acc=0.965

### Iter 1 · [fill_grid] · trial_1
- **Hypothesis**: 试 lr=2e-4，看更高 lr 是否更优
- **Diff vs base**: lr 1e-4 → 2e-4
- **Outcome**: val_acc=0.963 (-0.2%) — refuted

### Iter 2 · [fill_grid] · trial_2
- **Hypothesis**: 反向试 lr=5e-5，可能更稳
- **Diff vs base**: lr 1e-4 → 5e-5
- **Outcome**: val_acc=0.968 (+0.3%) — confirmed, 更小 lr 更稳

### Iter 5 · [refine_best] · trial_5
- **Hypothesis**: lr=5e-5 优于基线。试 [3e-5, 7e-5] 看是否还能往下
- **Diff vs base**: lr 5e-5 → 3e-5 (fork_of trial_2)
- **Outcome**: val_acc=0.969 (+0.001) — 信号弱，达到平台

### Iter 8 · [add_axis] · trial_8
- **Hypothesis**: lr 探索充分，best 区间 [2e-4, 3e-4] 确认。引入 warmup 看是否还有空间
- **Diff vs base**: warmup 0 → 0.03 (lr 锁 2.5e-4)
- **Outcome**: val_acc=0.971 (+0.2%) — confirmed, warmup 有用

### Iter 15 · [refine_best] · trial_15  ← BEST
- **Hypothesis**: lr=2.5e-4 + warmup=0.03 当前 best。再试 ±10% 微调
- **Diff vs base**: lr 2.5e-4 → 2.6e-4 (fork_of trial_8)
- **Outcome**: val_acc=0.972 — same as 2.5e-4, plateau detected

(其余 iter 省略)

## Confirmed

- lr 区间 [2e-4, 3e-4] 是 sweet spot，内部差异 < 0.1%
- warmup_ratio=0.03 优于 0（微弱信号）

## Refuted

- lr ≥ 5e-3：数值不稳，loss 在 epoch 30 前后发散
- warmup_ratio ≥ 0.1：前期收敛慢，最终 val_acc 落后
- weight_decay = 1e-2：val_acc 下降 2%

## Open questions

- weight_decay 微小区间 [1e-7, 1e-5] 未测
- batch_size 被 fixed=256，未变
- **单 seed 结果** — best 未多 seed 复验

## Recipe (locked in by this session)

```yaml
lr: 2.5e-4
lr_scheduler: cosine
warmup_ratio: 0.03
weight_decay: 1e-4
```

**Recommend**: 跑 3-seed 验证 trial_15 config 后再 deploy。
```

---

## Decision-tag conventions

The hypothesis text on each run starts with one of these brackets (set by `/train-tune`):

| Tag | When agent uses it |
|---|---|
| `[baseline]` | iter 0 only — establishing the reference point |
| `[fill_grid]` | adding new value within an existing axis's range to fill gaps |
| `[refine_best]` | densifying around current best, smaller deltas |
| `[add_axis]` | introducing a new axis for the first time |
| `[verify]` | re-running a prior config (often with different seed) for verification |
| `[unlabeled]` | rendered when hypothesis text has no tag prefix (legacy / manual run) |

Render the tag in `### Iter <N> · [<tag>] · <run_id>` heading. If hypothesis text begins
with the tag, strip it from the body so it doesn't appear twice.

## Edge cases

- **No completed runs in session**: render headline + state, then "No completed trials yet"
  in timeline, skip Coverage / Confirmed / Refuted / Recipe sections (replace with —).
- **Single trial session**: same headline format, timeline has one entry, no curve
  (a single bar is meaningless).
- **Best is from a `[verify]` run**: note in headline "(verified, multi-seed N=3)".
- **Outcome field is null** (run completed but agent hasn't filled it): render
  "_outcome not yet annotated_" as a hint to invoke /train-tune to fill it.
- **Mid-session preview** (`--include-running`): mark in-progress trials with
  `(running, current val_acc=X at step Y/Z)`.
