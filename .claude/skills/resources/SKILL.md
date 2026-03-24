---
name: resources
description: Search local default locations for credentials, models, and data resources
---

# /resources — Resource Discovery

Search the local machine for credentials, models, and data in common default locations.
Can be invoked standalone or triggered by other skills (e.g., infer-run) when a resource is needed.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Report findings, then ask what to do.

## Workflow State

On entry: push `{ "skill": "resources", "step": "start", "project": "<PROJECT path>" }` to `history.json` stack.
If called by another skill: read `project` from the parent entry in the stack (the skill that called us).
On completion: pop from stack, append `completed` to history.

## Prerequisites

Ensure `{WORKSPACE}/resources.json` exists (workspace root, shared across all projects). If not, copy from `lifecycle/resources.json` and create it.

`{WORKSPACE}` = the workspace root directory (e.g., `D:\agent_space\mlclaw\projects`). Resolve from `project.json → workspace`, or from the parent directory of the project root.

All discovery results are written to `{WORKSPACE}/resources.json` so they persist across sessions and are shared by all projects.

## What to search

### Credentials

| Type | Default locations to check |
|------|--------------------------|
| SSH keys | `~/.ssh/id_rsa`, `~/.ssh/id_ed25519`, `~/.ssh/*.pem`, `~/.ssh/config` |
| AWS | `~/.aws/credentials`, `~/.aws/config`, env vars `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_PROFILE` |
| Docker/Registry | `~/.docker/config.json` |
| Kubernetes | `~/.kube/config` |
| GCP | `~/.config/gcloud/application_default_credentials.json` |
| Azure | `~/.azure/` |
| Custom tokens | env vars matching `*_TOKEN`, `*_API_KEY`, `*_SECRET` |

### Servers

Actively discover known servers from:

| Source | How to parse |
|--------|-------------|
| `~/.ssh/config` | Parse `Host`, `HostName`, `User`, `IdentityFile` entries → create server entries |
| `~/.ssh/known_hosts` | Extract unique hostnames/IPs (less info, but shows previously connected hosts) |

Run `python lifecycle/scripts/resources/parse_ssh_config.py` to extract server entries from SSH config.

**Fallback**: if script fails, manually read `~/.ssh/config` and extract Host/HostName/User/IdentityFile entries.

For each server found:
1. Create an entry in `resources.json → servers` with host, username, ssh_key_path
2. `alias`: use the SSH config `Host` name
3. `gpu`, `gpu_count`, `description`: leave empty (user fills later, or try SSH probe below)
4. `mlclaw_root`: ask user "MLClaw workspace root on this server?" (e.g., `/home/ubuntu/agent_space/mlclaw`). Required for any server that will execute code — remote paths are mapped from local `mlclaw_root` to the server's `mlclaw_root`
5. `python_path`: ask user "Python executable path on this server?" (e.g., `/home/ubuntu/miniconda3/envs/ml/bin/python`). If unsure, try `ssh <host> "which python3"` to auto-detect

**Optional GPU probe**: If user agrees, try `ssh <host> "nvidia-smi --query-gpu=name,count --format=csv,noheader"` to auto-fill gpu info. Only attempt with user permission.

### Models & Artifacts

| Type | Default locations to check |
|------|--------------------------|
| HuggingFace cache | `~/.cache/huggingface/hub/` |
| Torch hub | `~/.cache/torch/hub/` |
| ONNX models | scan common dirs: `~/models/`, `~/weights/`, `D:\models\`, stage `artifacts/` |
| TensorRT engines | `*.engine`, `*.trt` in above locations |

### Data

| Type | Default locations to check |
|------|--------------------------|
| Common data dirs | `~/data/`, `~/datasets/`, `D:\data\`, `D:\datasets\` |
| Project data | `{PROJECT}/stages/*/data/` |

## Flow

### Step 1: Check resources.json first

Update workflow step to `check_cache`.

Before searching, read `{WORKSPACE}/resources.json`. If it already has non-empty values for the requested resource type, show what's cached:
```
Already configured in resources.json:
  SSH: ~/.ssh/id_rsa
  AWS: profile "default", region us-east-1

Search again to update? (y/n)
```
If user says no → use cached values, done.
If user says yes or if resources.json has no values → proceed to search.

### Step 2: Search

Update workflow step to `search`.

Run the search for the requested category. Report findings:
```
Found credentials:
  SSH: ~/.ssh/id_rsa (RSA 4096)
  AWS: ~/.aws/credentials (profile: default, region: us-east-1)
  Docker: ~/.docker/config.json (1 registry)

Found models:
  ~/.cache/huggingface/hub/rtdetr-l/ (1.2GB)
  D:\models\yolov8.onnx (45MB)
```

### Step 3: Auto-save to resources.json

Update workflow step to `save`.

After showing results, propose what will be written to `{WORKSPACE}/resources.json`:
```
Will save to resources.json:
  aws.region: us-east-1
  aws.profile: default (from ~/.aws/credentials)
  ssh: ~/.ssh/id_rsa (RSA 4096)

Save these? (y/n/edit)
```

- **y** → write to resources.json
- **n** → skip, don't write
- **edit** → let user modify before saving

**CRITICAL: Never overwrite existing non-empty values without asking. If a field already has a value, show both old and new and ask which to keep.**

### Step 4: If nothing found

Update workflow step to `manual_input`.

If search finds nothing for the requested type:
1. Tell user: "No {type} credentials found in default locations."
2. Ask: "Do you have credentials? I'll save them to resources.json."
3. If yes → ask for each field ONE at a time, write to `{WORKSPACE}/resources.json`
4. If no → return to calling skill

## Usage modes

### Standalone: `/resources`

1. Ask: What are you looking for? Options:
   - credentials (SSH, AWS, registry, etc.)
   - models (weights, checkpoints, engines)
   - data (videos, images, datasets)
   - all
2. Follow the flow above (check cache → search → auto-save)

### Called by another skill

When another skill (e.g., infer-run) needs a non-local resource:

1. Check `{WORKSPACE}/resources.json` for cached values first
2. If cached and valid → return immediately, no search needed
3. If not cached → search only the relevant category, auto-save results
4. If nothing found → ask user for credentials, save to resources.json
5. Return to calling skill

## AWS Credential Troubleshooting

When AWS SSO (`aws sso login`) fails, common causes:

| Problem | Symptom | Fix |
|---------|---------|-----|
| SSO config expired | `InvalidRequestException` on `RegisterClient` | Confirm `sso_start_url` with admin — Identity Center may have migrated |
| Region mismatch | `InvalidRequestException` | Check `sso_region` matches actual Identity Center region |
| Network/proxy blocking | SSO hangs or returns unexpected responses | Check corporate firewall/proxy |
| SSO cache corrupted | Various errors | Clear `~/.aws/sso/cache/` and retry |

**Fallback rule**: If SSO profile fails, always try `default` profile or static credentials in `~/.aws/credentials` before giving up. Many setups have working static credentials alongside a broken SSO config.

```
SSO login failed → try default profile → try static credentials → ask user
```

Report the SSO error to the user as a warning (so they can fix it later), but continue with whatever credential method works.

## Safety

- NEVER display secret values (keys, passwords, tokens) in output. Only show paths, profiles, and metadata.
- When reporting AWS credentials, show profile name and region only.
- When reporting SSH keys, show path and key type only.
