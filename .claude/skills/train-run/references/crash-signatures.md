# Training Crash Signatures

Lookup table for diagnosing crashed training runs. The skill agent reads this in Step 4c (after Step 4b classifies a run as "crashed"), matches patterns against the last 50 lines of stdout (and last 10 lines of jsonl), and surfaces the most likely diagnosis + fix priority to the user.

**Matching strategy**: scan in declaration order. First family with any pattern hit wins. Within a family, multiple sub-signatures may match — report all and let the user pick.

If nothing matches, fall through to "Unknown" at the bottom — never guess; dump raw lines and let the user decide.

---

## OOM family

### Signature: GPU OOM (torch / CUDA)

**Patterns** (substring or regex against stdout):
- `torch.cuda.OutOfMemoryError`
- `RuntimeError: CUDA out of memory. Tried to allocate`
- `CUBLAS_STATUS_ALLOC_FAILED`
- `cuDNN error: CUDNN_STATUS_INTERNAL_ERROR` followed by OOM trace within 10 lines

**Diagnosis**: GPU memory exhausted. Common causes: batch_size too large, sequence/image too long, mixed precision not enabled, KV-cache (LLM) overflow, intermediate activations too big.

**Fix priority** (try top-down, surface first 2-3 to user):
1. `runtime_params.batch_size ÷ 2`
2. Add or raise `gradient_accumulation_steps` to keep effective batch the same
3. Enable `mixed_precision: bf16` (preferred) or `fp16`
4. Reduce `max_seq_len` / image resolution
5. Enable gradient checkpointing (`gradient_checkpointing: true`)
6. Switch to ZeRO-2/3 or FSDP if currently single-GPU / DDP

### Signature: GPU OOM (TensorFlow / JAX)

**Patterns**:
- `Resource exhausted: OOM when allocating tensor`
- `RESOURCE_EXHAUSTED:`
- `XlaRuntimeError: RESOURCE_EXHAUSTED`

**Diagnosis**: Same as torch OOM, in TF/JAX wording.

**Fix priority**: Same as above. For JAX, also consider `jax.config.update("jax_default_matmul_precision", "bfloat16")`.

### Signature: Host-level OOM (process killed)

**Patterns**:
- Process exited with code `137` AND no torch OOM in last 50 lines
- `Killed` (last word in stdout)
- `dmesg` / `journalctl` shows `Out of memory: Killed process <pid> (python)` (skill should `dmesg | tail` if accessible)

**Diagnosis**: System RAM exhausted (not GPU). Common causes: dataloader workers buffering too much, memory leak in custom code, large in-memory dataset.

**Fix priority**:
1. `num_workers ÷ 2`
2. Reduce prefetch_factor (default 2 → 1)
3. Audit dataset code: large in-memory tables / `pd.DataFrame` / `np.array` held across epochs
4. Use `mmap_mode='r'` for large numpy artifacts
5. If using HF datasets: `streaming=True`

---

## NaN family

### Signature: Loss diverged to NaN

**Patterns**:
- `loss=nan` / `loss: nan` / `Loss is nan`
- jsonl: a `train_step` record with `"loss": NaN` or `"loss": null` (json doesn't allow NaN; check for null where loss is expected)
- `assert not torch.isnan(loss)` traceback
- `RuntimeError: Function 'XXBackward' returned nan values`

**Diagnosis**: Numerical instability. Common causes: lr too high, gradient explosion, bad input data (inf/very large values), fp16 underflow/overflow, division by zero in custom loss.

**Fix priority**:
1. `learning_rate × 0.5` (or × 0.1 for severe cases)
2. Enable `grad_clip: 1.0` (or whatever framework calls it; e.g., `max_grad_norm`)
3. Add warmup if not present (`warmup_ratio: 0.03` typical)
4. Check data: scan first 100 batches for inf/-inf, all-zero labels, malformed targets
5. If using fp16: switch to bf16 (better dynamic range) or fp32 to isolate
6. Inspect custom loss for `log(0)` / `1/0` / `sqrt(negative)`

### Signature: NaN in gradients (not loss)

**Patterns**:
- `gradient contains nan` / `inf gradient`
- Loss decreases normally but model weights become NaN after step

**Diagnosis**: Loss numerically OK but backward pass produces inf/NaN. Often a custom op / autograd function bug.

**Fix priority**:
1. Enable anomaly detection for one debug epoch: `torch.autograd.set_detect_anomaly(True)` — slow but pinpoints the offending op
2. Add `grad_clip` (covers the symptom)
3. Audit custom autograd functions / `Function.backward` overrides

---

## Hang family

### Signature: Process alive but no progress

**Conditions** (all true):
- Process PID alive (or tmux session active)
- jsonl file mtime > 2× expected interval (e.g., > 10 min when expected_interval = 5 min)
- GPU utilization low or zero (check `nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits` — if all GPUs report < 5% sustained, hang likely)

**Diagnosis**: Training stuck. Common causes:
- **Dataloader hang**: worker deadlock (most common). Often due to fork+thread issues, signal handlers, or a dataset method that blocks.
- **Distributed deadlock**: rank N waiting for rank M that crashed silently. Check all ranks' stdout — if some have errors, others are stuck on collective.
- **NCCL hang**: communication deadlock. Often after an OOM on one rank that didn't propagate.
- **Disk I/O**: writing a huge ckpt to a slow filesystem.

**Diagnostic actions** (in order):
1. `py-spy dump --pid <pid>` — get Python stack of the main process. Look for `socket.recv` (NCCL), `multiprocessing.connection.recv` (dataloader), or custom blocking call.
2. `nvidia-smi` — check GPU mem (if held but util 0% = stuck mid-batch), check for "(P0/P8)" power state mismatch (P8 = idle).
3. For distributed: read all per-rank stdout files. If one rank has a stack trace and others are silent → that rank crashed, others deadlocked.

**Fix priority**:
1. Kill the run, retry with `num_workers: 0` (rules out dataloader for diagnosis)
2. Add NCCL timeout: `NCCL_TIMEOUT=1800` env var, so deadlocks fail fast next time
3. If distributed: check that all ranks see the same data shard count

### Signature: All ranks reached barrier, never resume

**Patterns**:
- All processes' stdout end with the same step number then go silent
- `nccl WARN ... timeout` (after timeout, you'll see this; before timeout it's pure hang)

**Diagnosis**: Collective op (allreduce, allgather) deadlock. Usually a control-flow divergence — one rank takes a different code path than the others (e.g., `if rank == 0: skip eval`).

**Fix priority**:
1. Audit code for rank-conditional paths that affect collectives
2. Set `NCCL_TIMEOUT` so future hangs surface within 30 min instead of forever

---

## Preempt family

### Signature: SLURM / cloud preemption

**Patterns**:
- `slurmstepd: error: *** STEP ... CANCELLED ...`
- `Received signal 15, exiting gracefully`
- `Spot instance termination notice` (AWS) / `Preemption notice` (GCP)
- Process killed with SIGTERM (exit code 143)

**Diagnosis**: External scheduler/cloud reclaimed the resource. Not a bug — expected on spot/preemptible.

**Fix priority**:
1. **Mark run as `preempted` (not `failed`)** in `run.json -> status`
2. Suggest the user re-invoke `/train-run` — on re-entry, skill should offer "Continue from last ckpt? (fork self + load last.pt as init weights, parents += [self])"
3. If preemption is frequent: increase ckpt frequency (`save_every` lower) so less work is lost per preempt

### Signature: Manual cancel

**Patterns**:
- `KeyboardInterrupt`
- Process killed with SIGINT (exit code 130)
- User invoked `tmux kill-session` or similar

**Diagnosis**: User intentionally stopped. Not an error.

**Fix priority**:
1. Mark `status: cancelled`, no diagnosis needed
2. Skill should NOT offer "retry" automatically; user knows what they did

---

## Driver / Hardware family

### Signature: CUDA driver mismatch / unhealthy GPU

**Patterns**:
- `CUDA driver version is insufficient for CUDA runtime version`
- `CUDA error: an illegal memory access was encountered`
- `Xid 79 / 31 / 13` in `dmesg` (GPU unrecoverable error)
- `nvidia-smi` returns "Unable to determine the device handle"

**Diagnosis**: Not a training bug — host environment issue. Driver mismatch, GPU hardware fault, ECC error, thermal throttling crash.

**Fix priority**:
1. Run `nvidia-smi` — confirm GPUs visible and healthy
2. If GPU "fell off the bus": needs reboot (or `nvidia-smi -r` if hot-reset supported)
3. Check `dmesg | grep -i nvidia` for kernel-level errors
4. If on a remote server: notify the server owner / migrate to a different node
5. Mark run as `failed` with reason "host_fault" — distinguish from training-side failures

---

## Unknown — fallback

If none of the above pattern families match:

1. **Don't guess.** Surface raw evidence:
   ```
   Diagnosis: unknown crash. No known signature matched.

   Last 50 lines of stdout:
   <dump>

   Last 10 lines of jsonl:
   <dump>

   Process exit code: <code>
   GPU state at exit: <nvidia-smi snapshot if available>
   ```
2. Ask the user: "Do you recognize this? Want to retry as-is, retry with a fix, or save state and inspect manually?"
3. If user identifies a new pattern, suggest adding it to this file — track recurring unknowns as a signal that this catalog is missing coverage.

---

## Adding new signatures

When the skill encounters an unknown crash and the user identifies the cause + fix, add a new entry to the appropriate family:

```markdown
### Signature: <one-line name>
**Patterns**: ...
**Diagnosis**: ...
**Fix priority**: ...
```

The skill is more useful the more this catalog grows. Recurring unknowns over multiple runs is a signal to formally enumerate them here.
