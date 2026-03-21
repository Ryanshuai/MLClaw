---
name: infer-run
description: Run inference using the configured pipeline — resolve sources, execute, collect results
---

# /infer-run — Run Inference

Execute an inference run using the configured pipeline.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Record the answer, then ask the next. Never list multiple questions at once.
When filling sources, ask for each item's path one at a time — do not list all items at once.

## Workflow State

On entry: push `{ "skill": "infer-run", "step": "locate_project" }` to `history.json` stack.
Update step as you progress. On completion: pop from stack, append `completed` to history.
When calling `/resources`: update own status to `paused` in history, push `/resources` to stack with current PROJECT path as context. On return: pop `/resources`, append `resumed` to history, continue.

## Dependency Check

On entry, check upstream requirements per the Skill Dependency Graph in CLAUDE.md:
- **infer-init done**: `config.json → entry_command` must be non-empty. If not, offer to run `/infer-init` first.
- **resources.json for credentials**: checked lazily in Step 1 when a source needs non-local access.

## Locate Project

Show recent projects and let user pick. Ask the user which one:

1. **Recent projects (top 5)** — scan `history.json` history across all projects in the workspace root (default `D:\agent_space\mlclaw\projects\`). List up to 5 most recently used projects with their paths.
2. **Provide a path** — user gives a path to the project directory.
3. **Other** — user has another way to locate it.

Once `project.json` is found, set `PROJECT = {project.root}`:
- Read `{PROJECT}/stages/inference/config.json` — check dependency: `entry_command` must be non-empty. If not, offer `/infer-init` as upstream dependency.
- Read `{PROJECT}/stages/inference/artifacts.json`
- Read `{PROJECT}/stages/inference/input.json`
- Read `{PROJECT}/stages/inference/output.json`

## Variable reference syntax `${}`

See CLAUDE.md → Conventions → Variable Reference Syntax. All `${}` references are resolved before execution. Stage-specific paths use `{PROJECT}/stages/inference/`.

## Fork Check

Update workflow step to `fork_check`.

Ask: "Base on a previous run? (run ID, or skip for fresh run)"

If user provides a run ID:
1. Read `{PROJECT}/stages/inference/runs/{base_run_id}/config_snapshot.json`, `sources.json`, and `run.json → lineage.parents`
2. Load those as the starting config, sources, and parents for this run
3. Set `FORK_OF = {base_run_id}` (will be written to `run.json → lineage.fork_of`)
4. Inherit `lineage.parents` from the base run. If user changes the model artifact, update parents accordingly.
5. Show what was loaded: "Starting from {base_run_id}'s config. What do you want to change?"
6. User specifies changes (one at a time) → apply to the loaded config
7. Sources from the base run are reused — skip Step 1 unless user wants to change them

If user skips → proceed normally with `FORK_OF = null`.

## Step 1: Check Sources

Update workflow step to `check_sources`.

Read `artifacts.json` and `input.json` → `sources` sections.

**If sources not filled** (paths are empty):
- Ask user for each item's path, one at a time
- Write paths into the corresponding JSON file's `sources` section
- If source is not `local`, also ask which credentials key to use

**If sources filled:**
- Validate all local paths exist on disk
- For non-local sources, run a connectivity test (see below)
- Show summary, ask user to confirm or update

### Server matching

When user references a server by description (e.g., "A100 server", "8-GPU machine", "the 4090 one"),
read `{PROJECT}/resources.json → servers` and match against `alias`, `description`, `gpu`, `gpu_count` fields.

- If exactly one match → show it and ask user to confirm (e.g., "Found: a100_8gpu (8x A100, 192.168.1.100). Use this?")
- If multiple matches → list them, ask user to pick one
- If no match → ask user which server, or offer to add a new one

The matched server key (e.g., `a100_8gpu`) becomes the `credentials` value in the source entry.

### Connectivity test for non-local sources

When a source is not `local`, test access using:

```
python lifecycle/scripts/infer-run/test_connection.py ssh <host> <username> <ssh_key_path> <port> <remote_path>
python lifecycle/scripts/infer-run/test_connection.py s3 <s3_path> <region> <profile>
```

**Fallback**: if script fails, run the SSH/AWS commands directly via Bash.

For `server` type: resolve `<user>` and `<host>` from `{PROJECT}/resources.json → servers → <matched_key>`.

**If test succeeds:** proceed.

**If test fails:** trigger the credentials configuration flow:
1. Tell user: "Cannot access <path>. Credentials may not be configured."
2. Pause own workflow, invoke `/resources` for the relevant credential type (SSH, AWS, etc.)
3. If `/resources` finds something → ask user to confirm and link it to `{PROJECT}/resources.json`
4. If `/resources` finds nothing → fall back to manual configuration:
   - Check if the server entry exists in `resources.json → servers`:
     - If exists but credentials wrong → ask user to update fields, ONE at a time
     - If not exists → ask user to add a new server. Only ask required fields:
       1. host (required)
       2. username (required, default: ubuntu)
       3. ssh_key_path (required, suggest from resources.json → ssh)
       Optional fields (alias, description, gpu, gpu_count, port) can be filled later or auto-detected via GPU probe.
   - Write values into `{PROJECT}/resources.json`
5. Re-run the connectivity test
6. If still fails → show error, ask user to debug manually or try different credentials

## Step 2: Create Run

Update workflow step to `create_run`.

Run `python lifecycle/scripts/infer-run/create_run.py {PROJECT}/stages/inference lifecycle/run.json` to create the run directory and initialize run.json.

**Fallback**: if script fails, manually create `runs/run_{YYYYMMDD}_{HHmmss}/` with subdirs outputs/ and logs/, copy run.json template and fill run_id/stage/created_at.

Run ID format: `run_{YYYYMMDD}_{HHmmss}` (e.g., `run_20260316_153024`). Timestamp-based, no counter needed.
For cross-stage lineage references, use full path: `{stage}/run_{YYYYMMDD}_{HHmmss}` (e.g., `inference/run_20260316_153024`).

Set `RUN_DIR = {PROJECT}/stages/inference/runs/run_{YYYYMMDD}_{HHmmss}` — all subsequent steps use `{RUN_DIR}` for absolute paths.

```
runs/run_20260316_153024/
  run.json              # run metadata + env snapshot (fixed keys)
  sources.json          # snapshot of sources used
  config_snapshot.json  # frozen config
  outputs/              # output files
  logs/                 # run logs
```

### Code snapshot

Fill `run.json → code` from two sources:

- `origin_commit`: the original GitHub commit when code was cloned. Read from `project.json → stages.{stage}.commit`. This never changes.
- `project_commit`: the current commit in the **project git** (`git -C {PROJECT} rev-parse HEAD`). This captures any local modifications made during debug iterations.

Also copy `repo` and `branch` from `project.json → stages.{stage}`.

This way you can always trace: original code (origin_commit) → what was actually run (project_commit) → diff between them shows all local changes.

### Environment snapshot

Run `python lifecycle/scripts/infer-run/capture_env.py` and write the output into `run.json → env` (not a separate file).
For remote execution, run the script on the remote server via SSH.

**Fallback**: if script fails, manually run `python --version`, `pip show <packages>`, `nvidia-smi` and fill run.json → env.

The package list to check is defined in `lifecycle/run.json → env.packages` (source of truth). Only check the keys listed there. Use `null` if not installed. Do NOT dump the full `pip freeze`. **NEVER install missing packages just to fill this list. Only record what is already installed.**

This runs automatically — no user interaction needed.

### Dependency check

Run `python lifecycle/scripts/infer-run/check_deps.py {PROJECT}/stages/inference/config.json {RUN_DIR}/env.json`

**Fallback**: if script fails, manually compare required_packages against env.packages:
- Package required but not installed → **error**, tell user
- Version mismatch → **warning**
- All good → proceed silently

**run.json** — copy from `lifecycle/run.json`, fill values:
```json
{
  "run_id": "run_20260316_153024",
  "stage": "inference",
  "project": "${project.name}",
  "status": "pending",
  "execution": "local",
  "server": null,
  "pid": null,
  "created_at": "<ISO timestamp>",
  "started_at": null,
  "finished_at": null,
  "duration_s": null,
  "error": null,
  "metrics": {}
}
```

If `FORK_OF` was set in Fork Check, write it to `run.json → lineage.fork_of`.

## Code modifications

If config and code don't match (e.g., hardcoded paths, missing args):
1. **Prefer fixing config** — adjust config.json / runtime_params to match code behavior
2. **If code must change** — edit the code in `stages/inference/code/`, then commit to the **project git** (not the original repo). Record what was changed in `history.json` history.

## Step 3: Build & Execute Command

Update workflow step to `execute`.

1. Read `config.json` → `entry_command` and `runtime_params`
2. Resolve all `${}` references to actual values
3. Build the final command based on `config.json → config_format`:
   - `argparse`: `--key value` for each runtime_param
   - `yaml`/`omegaconf`: write resolved config file, pass via original config argument
   - `hydra`: `key=value` override syntax
   - `json`: write resolved JSON config
   - **Hybrid** (e.g., `argparse+omegaconf`): write resolved config file AND pass it via CLI arg (e.g., `python main.py --config resolved_config.yaml`). The config file contains omegaconf params, the CLI passes the file path.
4. Save resolved config as `{RUN_DIR}/config_snapshot.json`
5. Save resolved sources as `{RUN_DIR}/sources.json`
6. Show the resolved command to user for confirmation

### Execution modes

Ask user: debug mode or production mode?

**Debug mode (default for first run):**

1. **Limit data scope**: Before running, analyze the input data and propose a minimal subset:
   - Video: first N seconds or 1 clip (ask user how many, default: 10s)
   - Images: first N images (default: 5)
   - Text/tabular: first N rows (default: 10)
   - If code supports a `--num_samples`, `--max_frames`, `--limit` or similar arg, use it
   - If not, truncate/copy a small slice of the input data to a temp location and point to that

2. **Run synchronously**, stream stdout/stderr in real time

3. **If command fails:**
   - Show the error
   - Analyze the error and propose a fix (config change or code edit)
   - Ask user: "Apply this fix and re-run?" (y/n)
   - If yes → apply fix, commit code changes to project git, re-run automatically
   - If no → let user make their own changes, then user says "retry" to re-run

4. **If command succeeds:**
   - Show where output files are stored (clickable paths):
     ```
     Run completed (debug mode, 5 images).
     Results at: D:\agent_space\mlclaw\projects\detection\stages\inference\runs\run_20260316_153024\outputs\
     Files:
       - results.json (12KB)
       - viz/frame_001.jpg (45KB)
     ```
   - Ask: "Results look right? Options:"
     - **production** → switch to production mode with full dataset
     - **retry** → re-run debug with different params or more data
     - **inspect** → look at output files in detail

**Production mode:**
Ask user: run locally or on a server?

**Local execution:**
1. Run command in background (`run_in_background`)
2. Redirect stdout/stderr to `{RUN_DIR}/logs/stdout.log` and `stderr.log`
3. Update `run.json → pid` with the process ID, `status` to `running`
5. Tell user: "Running in background. Use `/infer-run` again to check status, or `/loop 5m /infer-run` for auto-polling."

**Remote execution (server):**
1. Resolve server from `resources.json → servers`. Use `python_path` from the server entry as the Python executable (e.g., `/home/ubuntu/miniforge3/bin/python`). If `python_path` is empty, fall back to `python3`.
2. SSH into server, start command in tmux session. Use a wrapper script to avoid quoting issues:
   - First, `scp` the resolved config and a `run.sh` script to the remote server
   - `run.sh` contains: `cd <code_path> && <command> > stdout.log 2> stderr.log; echo $? > exit_code.txt`
   - Then: `ssh <user>@<host> "tmux new-session -d -s run_NNN 'bash <remote_path>/run.sh'"`
3. Update `run.json → status` to `running`, add `server` field with server key
4. Tell user: "Running on <server_alias>. Use `/infer-run` again to check status."

### Status check (when /infer-run is invoked and a run is already running)

If `run.json → status` is `running`:

**Local:**
1. Check if PID in `run.json → pid` is still alive
2. If alive → show tail of stdout.log, report "still running"
3. If dead → read exit code, proceed to Step 4 (Collect Results)

**Remote:**
1. `ssh <user>@<host> "tmux has-session -t run_NNN 2>/dev/null && echo running || echo done"`
2. If running → `ssh <host> "tail -20 <log_path>/stdout.log"`, report "still running"
3. If done → `scp` logs back to `{RUN_DIR}/logs/`, read exit code, proceed to Step 4

When status is `running`, also offer: "Cancel this run?"
- **Local**: kill PID, update manifest status to `cancelled`
- **Remote**: `ssh <user>@<host> "tmux kill-session -t run_NNN"`, update manifest status to `cancelled`

**CRITICAL: Never block waiting for a long-running command. Always return immediately and let user check back.**

## Step 4: Collect Results

Update workflow step to `collect_results`.

After command finishes (detected via status check):

1. Finalize run: `python lifecycle/scripts/infer-run/finalize_run.py {RUN_DIR}/run.json completed` (or `failed`/`cancelled`). Calculates duration, reads stderr for error message if failed.
   **Fallback**: manually update run.json status, finished_at, duration_s.

2. Check declared outputs:
   - Look in `{RUN_DIR}/outputs/` for expected files
   - Search code directory for outputs that weren't redirected
   - Report found vs declared

3. **Collect metrics**: `python lifecycle/scripts/infer-run/extract_metrics.py {PROJECT}/stages/inference/output.json {RUN_DIR}/`
   Store the output in `run.json → metrics`.
   **Fallback**: manually read stdout.log and result files to extract watched metrics.

4. Ask user for an optional `alias` and `description` for this run (or skip). Write to `run.json`.
   Then: `python lifecycle/scripts/infer-run/update_index.py {PROJECT}/runs_index.json {RUN_DIR}/run.json`
   **Fallback**: manually read run.json and append summary to runs_index.json.

5. Show summary:
   ```
   Run: run_20260316_153024 | Status: completed | Duration: 3m 24s
   Metrics:
     fps: 42.5
     mAP: 0.78
   Outputs: results.json (12KB), viz/ (5 files)
   ```

6. Pop self from `history.json` stack, append `completed` to history

## Quick mode

If user provides paths inline (e.g., "run inference on /path/to/video with model /path/to/model.onnx"):
1. Match paths to declared items by type/extension
2. Fill sources in artifacts.json / input.json
3. Proceed directly to run
