---
name: infer-run
description: >
  Use this skill whenever the user wants to run inference — feeding data through a trained model to get
  predictions, detections, embeddings, or any output. Trigger for: running inference on images/video/data,
  executing a model on new inputs, testing a model quickly, checking inference speed/FPS, running in debug
  or production mode, checking status of a running inference job, forking a previous run with different
  inputs or model. Also trigger for Chinese requests like "跑推理", "推一下", "跑一下模型", "测试一下",
  "看看效果". This is the execution skill — not for initial config setup (use infer-init).
---

# /infer-run — Run Inference

Execute an inference run: resolve sources, run the model, collect outputs.

**One question at a time** — asking multiple questions at once is overwhelming. Ask one, record, ask next.

**Workflow state, dependency checks, locate project, variable references** — follow CLAUDE.md (Workflow State Protocol, Skill Dependency Graph, Variable Reference Syntax). Stage = `inference`, upstream = `/infer-init` (check `config.json -> entry_command` non-empty).

## Fork Check

Ask: "Base on a previous run? (run ID, or skip for fresh run)"

If forking: load the base run's `config_snapshot.json`, `sources.json`, and `lineage.parents` as starting point. Set `lineage.fork_of`. User changes only what they want. Sources from the base run are reused — skip Step 1 unless user wants to change them. If user changes the model artifact, update `lineage.parents` accordingly.

If skip: fresh run, `fork_of = null`.

## Steps 1-3: Shared Run Mechanics

Follow CLAUDE.md "Run Skill Internal Dependencies" for the shared step flow:

1. **Resolve Assets** (step `check_sources`) — fill concrete paths in `artifacts.json` and `input.json` sources. For server matching, connectivity tests, and credential flows, see CLAUDE.md "Run Skill Internal Dependencies" Step 1. Scripts in `lifecycle/scripts/infer-run/` (test_connection.py, etc). If any script fails, do the same work manually with Bash.
2. **Create Run** (step `create_run`) — create run dir, initialize run.json, code snapshot, env snapshot, dependency check. Scripts: `create_run.py`, `capture_env.py`, `check_deps.py`. For code source resolution and environment resolution, see CLAUDE.md conventions.
3. **Build & Execute** (step `execute`) — resolve `${}` references, build command per `config_format`, save `config_snapshot.json` and `sources.json`, confirm with user.

### Execution Modes

**Debug mode** (default for first run):
- Limit data to a small subset (video: 10s, images: 5, text: 10 rows). Use code's own limiting args if available (--num_samples, --max_frames, --limit), otherwise copy a slice to a temp location.
- Run synchronously, stream output.
- On failure: diagnose, propose fix, ask "Apply and re-run?". On success: show output files, ask "production / retry / inspect?"

**Production mode** — ask local or server:
- **Local**: run in background (`run_in_background`), log to `{RUN_DIR}/logs/`. Return immediately so the user can continue working. They can check back with `/infer-run` again, or `/loop 5m /infer-run` for auto-polling.
- **Remote**: resolve server from resources.json, use `python_path` from server entry. SCP config + run.sh to server, launch in tmux. Return immediately.

For local/remote execution details and path mapping, see CLAUDE.md "Run Skill Internal Dependencies" Step 3 and "Path Mapping".

### Status Check

When `/infer-run` is invoked and a run has `status: "running"`:
- **Local**: check PID alive. If alive, show log tail + "still running". If dead, proceed to collect results.
- **Remote**: check tmux session. If alive, show log tail. If done, SCP logs back, proceed to collect results.
- Offer "Cancel this run?" (kill PID or tmux session, set status to `cancelled`).

Never block waiting for a long-running command. Return immediately so the user can continue working — they check back by invoking `/infer-run` again.

## Step 4: Collect Results

After execution finishes:

1. **Finalize run** — update `run.json` status, finished_at, duration_s. Script: `finalize_run.py`. Fallback: manual update.

2. **Check outputs** — look in `{RUN_DIR}/outputs/` for expected files, search code dir for misplaced outputs.

3. **Collect metrics** — extract from stdout/result files into `run.json -> metrics`. Script: `extract_metrics.py`. Fallback: parse logs manually.

4. **Alias** — ask user for optional alias/description; write into `run.json -> alias` / `description`. No separate index file to update — `run.json` files are the source of truth, queried via `jq` on demand (see CLAUDE.md "Listing runs (no separate index)").

5. **Show summary**:
   ```
   Run: run_20260316_153024 | Status: completed | Duration: 3m 24s
   Metrics:
     fps: 42.5
     detections: 1247
   Outputs: results.json (12KB), viz/ (5 files)
   ```

6. Pop from workflow stack, append `completed` to history.

## Quick Mode

When user provides paths inline (e.g., "run inference on /path/to/video with model /path/to/model.onnx"):
1. Match paths to declared items by type/extension
2. Fill sources in artifacts.json / input.json
3. Skip source-filling dialogue, proceed directly to execution
