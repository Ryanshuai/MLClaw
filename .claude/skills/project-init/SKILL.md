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
2. Ask: Where to create the project? (default: `D:\agent_space\mlclaw\projects`)
3. Ask: Which stages to enable? Options: data, exploration, training, evaluation, inference, deployment
4. For each enabled stage, ask one at a time: Code source for {stage}? (git URL / local path / skip for now)

5. **Show a full summary** with all fields and values (including defaults):
   ```
   Project: detection
   Workspace: D:\agent_space\mlclaw\projects
   Root: D:\agent_space\mlclaw\projects\detection
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

Run `python lifecycle/scripts/project-init/init_project.py '<project_json_string>' 'd:\10_projects\MLClaw'` — creates directories, copies templates, writes .gitignore, runs git init + initial commit.

**Fallback**: if script fails, manually create directories (`stages/{stage}/code`, `runs`, `artifacts`, `data` for each enabled stage), copy JSON templates from `lifecycle/`, write .gitignore, git init + commit.

## Clone / Link code

For each stage with a repo value:
- **Git URL**: `git clone` into `stages/{stage}/code/`, record branch + commit in `project.json`, remove `.git` so code becomes plain files tracked by project git.
- **Local path**: symlink `stages/{stage}/code/_source` -> local path.

Code modifications during runs stay in project git — never pushed back to original repo.

## After creation

1. Verify project is under `workspace_root` from CLAUDE.md. If user chose a different workspace, update `workspace_root`.
2. Tell user: Project created at `{root}`.
3. **Downstream**: offer `/resources` and available `/{stage}-init` skills for enabled stages. For stages without an init skill yet, note it's not available.
