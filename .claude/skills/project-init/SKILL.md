---
name: project-init
description: Initialize a new MLClaw project — create directory structure and project.json
---

# /project-init — Initialize MLClaw Project

One-time project setup. Creates directory structure, `project.json`, and stage config templates.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Record the answer, then ask the next. Never list multiple questions at once.

## Workflow State

On entry: push `{ "skill": "project-init", "step": "start" }` to `history.json` stack.
Update step as you progress. On completion: pop from stack, append `completed` to history.

## Conversation flow

1. Ask: What is the project name? (lowercase, underscores, e.g., `rtdetr_detection`)
   → User answers → record

2. Ask: Where to create the project? (default: `D:\agent_space\mlclaw\projects`)
   → User answers → record

3. Ask: Which stages to enable? Options: data, training, tracking, evaluation, inference, registry, deployment, monitoring
   → User answers → record

4. For each enabled stage, ask one at a time: Code source for {stage}? (git URL / local path / skip for now)
   → User answers → record → next stage

5. **After all questions, show a FULL summary** with all fields and their values (including defaults):
   ```
   Project: detection
   Workspace: D:\agent_space\mlclaw\projects
   Root: D:\agent_space\mlclaw\projects\detection
   Created: 2026-03-16

   Stages:
     data:       disabled
     training:   enabled  repo: https://github.com/...
     tracking:   disabled
     evaluation: disabled
     inference:  enabled  repo: skipped
     registry:   disabled
     deployment: disabled
     monitoring: disabled

   Paths:
     stages: stages
     runs_pattern: stages/{stage}/runs
     artifacts_pattern: stages/{stage}/artifacts
     data_pattern: stages/{stage}/data
     secrets: secrets.json

   Create? Anything to change?
   ```

6. User confirms → create everything
   User wants changes → update and show summary again

## Write project.json

Fixed keys, agent only modifies values.

```json
{
  "name": "",
  "root": "",
  "workspace": "",
  "created": "",
  "stages": {
    "data":       { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/data/code" },
    "training":   { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/training/code" },
    "tracking":   { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/tracking/code" },
    "evaluation": { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/evaluation/code" },
    "inference":  { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/inference/code" },
    "registry":   { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/registry/code" },
    "deployment": { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/deployment/code" },
    "monitoring": { "enabled": false, "repo": null, "branch": null, "commit": null, "code_path": "stages/monitoring/code" }
  },
  "paths": {
    "stages": "stages",
    "runs_pattern": "stages/{stage}/runs",
    "artifacts_pattern": "stages/{stage}/artifacts",
    "data_pattern": "stages/{stage}/data",
    "secrets": "secrets.json"
  }
}
```

## Directory structure to create

```
{root}/
├── project.json
├── secrets.json              ← from lifecycle/secrets.json
├── history.json             ← from lifecycle/history.json
├── runs_index.json          ← from lifecycle/runs_index.json
├── .gitignore
├── stages/
│   ├── inference/
│   │   ├── code/
│   │   ├── artifacts.json    ← from lifecycle/inference/artifacts.json
│   │   ├── config.json       ← from lifecycle/inference/config.json
│   │   ├── input.json        ← from lifecycle/inference/input.json
│   │   ├── output.json       ← from lifecycle/inference/output.json
│   │   ├── runs/
│   │   ├── artifacts/          ← actual artifact files (models, etc.) stored here
│   │   └── data/               ← actual input data stored here
│   ├── training/
│   │   ├── code/
│   │   ├── artifacts.json
│   │   ├── config.json
│   │   ├── input.json
│   │   ├── output.json
│   │   ├── runs/
│   │   ├── artifacts/
│   │   └── data/
│   └── ... (same for each enabled stage)
```

## Create project

Run the init script:
```
python lifecycle/scripts/project-init/init_project.py '<project_json_string>' 'd:\10_projects\MLClaw'
```

This creates the entire directory structure, copies templates, writes .gitignore, runs git init, and makes the initial commit — all in one step.

**Fallback**: If the script fails, do these steps manually using Bash/Write tools:
1. Create directories (stages/{stage}/code, runs, artifacts, data for each enabled stage)
2. Copy JSON templates from `lifecycle/` and `lifecycle/inference/`
3. Write .gitignore, git init, git add, git commit

## Clone / Link code

For each stage with a repo value:
- **Git URL** (http/https/git@): `git clone` into `stages/{stage}/code/`, then:
  1. Record branch: `git -C stages/{stage}/code rev-parse --abbrev-ref HEAD` → save to `project.json → stages.{stage}.branch`
  2. Record commit hash: `git -C stages/{stage}/code rev-parse HEAD` → save to `project.json → stages.{stage}.commit`
  3. Remove `stages/{stage}/code/.git` so the code becomes plain files tracked by the project git
  To reproduce: `git clone {repo} -b {branch}` + `git checkout {commit}`
- **Local path**: symlink `stages/{stage}/code/_source` → local path

This way, any code modifications during runs (e.g., fixing hardcoded paths) are committed to the project git, not pushed back to the original repo.

## After creation

1. Verify the project is under the `workspace_root` path in `CLAUDE.md`. If the user chose a different workspace, update `workspace_root` in CLAUDE.md.

2. Tell the user: Project created at `{root}`.

3. Ask ONE question: "Want to init any of the enabled stages now? (or skip for now)"
   - Only offer stages that have a corresponding `/{stage}-init` skill (currently: inference)
   - For stages without an init skill yet, tell user: "{stage} init not available yet"
   - User declines → done
