---
name: project-init
description: "Use this skill to create a new MLClaw project. Triggers when the user wants to start tracking an ML model, set up a project workspace, or initialize project structure. Use for: '\u65B0\u5EFA\u9879\u76EE', 'create project', 'initialize', 'set up a new model'. Not for running inference/evaluation (use the stage-specific skills)."
---

# /project-init — Initialize MLClaw Project

One-time project setup. Creates directory structure, `project.json`, and stage config templates.

Ask one question at a time — multiple questions at once overwhelms users.

Follow the Workflow State Protocol from CLAUDE.md: push on entry, update step as you progress, pop on completion.

## Conversation flow

1. Ask: What is the project name? (lowercase, underscores, e.g., `rtdetr_detection`)
2. Resolve workspace. Default = `workspace_root:` line at the top of CLAUDE.md (read it once, expand `~/`). Confirm with user; if they pick a different path, replace the `workspace_root:` line in CLAUDE.md so subsequent invocations get the new default. If `--workspace <path>` was passed in the user's prompt, skip the confirm and use it directly. `mkdir -p` whichever path won — workspaces are cheap, accidentally creating one is harmless.
3. Ask: Which stages to enable? Options: data, exploration, training, evaluation, inference, deployment
4. For each enabled stage, ask one at a time: Code source for {stage}? (git URL / local path / skip for now)

5. **Show a full summary** with all fields and values (including defaults):
   ```
   Project: detection
   Workspace: ~/agent_space/mlclaw/projects
   Root: ~/agent_space/mlclaw/projects/detection
   Created: 2026-03-16

   Stages:
     data:        disabled
     training:    enabled  repo: https://github.com/...
     inference:   enabled  repo: skipped
     ...

   Create? Anything to change?
   ```

6. User confirms -> create everything. User wants changes -> update and show summary again.

## Write project.json

Fixed keys, agent only modifies values. Template: `lifecycle/project.json`.

Each stage has: `enabled`, `code_path` (`stages/{stage}/code`), `code_source` (`source`, `path`, `branch`, `commit`, `credentials`). Paths section: `stages`, `runs_pattern`, `artifacts_pattern`, `data_pattern`.

## Create project

```
MLCLAW_ROOT=$(python <repo>/lifecycle/scripts/shared/workspaces.py tool)
python "$MLCLAW_ROOT/lifecycle/scripts/project-init/init_project.py" '<project_json_string>' "$MLCLAW_ROOT"
```

Creates directories, copies templates, writes `.gitignore`, runs git init + initial commit. `$HOME`-relative paths (project root, workspace, each stage's `code_source.path`) are rewritten to `~/`-prefixed form in `project.json` so the file survives rsync across machines.

**Fallback**: if script fails, manually create directories (`stages/{stage}/code`, `runs`, `artifacts`, `data` for each enabled stage), copy JSON templates from `lifecycle/`, write .gitignore, git init + commit.

## Clone / Link code

The unified contract (see CLAUDE.md "Code Source Resolution") is `code_dir = stages/{stage}/code/_source if exists else stages/{stage}/code`. Per source mode:

- **Git URL** (`code_source.source == "github"`): `git clone <code_source.path>` into `stages/{stage}/code/`, record `branch` + `commit` (HEAD SHA) in `project.json`, remove `.git` so the code becomes plain files tracked by project git. No `_source` symlink for this mode.
- **Local path** (`code_source.source == "local"`): `init_project.py` already creates the symlink `stages/{stage}/code/_source -> expanduser(code_source.path)` during creation. **Do not copy — the user iterates in their own repo, copy creates bidirectional sync friction**. Reproducibility comes from `code_snapshot.py` at run-time, not from a project-wide copy. After rsync to a new machine the symlink will dangle (it stores an expanded absolute path); recreate with `ln -sfn $(python -c "import json,os;print(os.path.expanduser(json.load(open('project.json'))['stages']['<stage>']['code_source']['path']))") stages/<stage>/code/_source`.
- **Server** (`code_source.source == "server"`): `scp` into `stages/{stage}/code/`, no `_source` symlink.

Code modifications during runs stay in project git (for github/server) or are captured per-run via `code_snapshot.py` SHA + dirty patch (for local) — never pushed back to the original repo.

## After creation

1. The chosen workspace was already upserted in step 2d — `last_used` is fresh and `n_projects` is now incremented to include this new project. If it wasn't yet upserted (unusual — only when the script was unavailable and you used the bootstrap fallback path), run `python lifecycle/scripts/shared/workspaces.py upsert <workspace>` now.
2. Tell user: Project created at `{root}`.
3. **Downstream**: offer `/resources` and available `/{stage}-init` skills for enabled stages. For stages without an init skill yet, note it's not available.
