---
name: resources
description: "Use this skill to discover and configure compute resources \u2014 SSH keys, AWS credentials, GPU servers, model files, data directories, and Python environment managers. Triggers when the user asks about available resources, credentials, servers, or when another skill needs non-local access. Use for: '\u770B\u770B\u6709\u4EC0\u4E48\u8D44\u6E90', 'scan for GPUs', 'find credentials', 'set up server access', 'what envs do I have'. Also called automatically by run skills when credentials are missing."
---

# /resources — Resource Discovery

Search the local machine for credentials, models, and data in common default locations. Can be invoked standalone or triggered by other skills when a resource is needed.

Ask one question at a time — multiple questions at once overwhelms users.

Follow the Workflow State Protocol from CLAUDE.md: push on entry, update step as you progress, pop on completion.

## Prerequisites

Ensure `{WORKSPACE}/resources.json` exists (workspace root, shared across projects). If not, copy from `lifecycle/resources.json`. Resolve `{WORKSPACE}` from `project.json -> workspace` or parent directory of project root.

## What to search

### Credentials

| Type | Default locations |
|------|------------------|
| SSH keys | `~/.ssh/id_rsa`, `~/.ssh/id_ed25519`, `~/.ssh/*.pem`, `~/.ssh/config` |
| AWS | `~/.aws/credentials`, `~/.aws/config`, env vars `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_PROFILE` |
| Docker/Registry | `~/.docker/config.json` |
| Kubernetes | `~/.kube/config` |
| GCP | `~/.config/gcloud/application_default_credentials.json` |
| Azure | `~/.azure/` |
| Custom tokens | env vars matching `*_TOKEN`, `*_API_KEY`, `*_SECRET` |

### Servers

Discover from `~/.ssh/config` (Host, HostName, User, IdentityFile) and `~/.ssh/known_hosts`.

Run `python lifecycle/scripts/resources/parse_ssh_config.py`. **Fallback**: manually read `~/.ssh/config`.

For each server found, create an entry with host, username, ssh_key_path, alias (SSH config Host name). Ask user for `mlclaw_root` (remote workspace root for path mapping) and `python_path` (try `ssh <host> "which python3"` to auto-detect). Leave gpu/description empty for user to fill.

**Optional GPU probe**: with user permission, try `ssh <host> "nvidia-smi --query-gpu=name,count --format=csv,noheader"`.

### Python Environment Manager

Check in preference order: mamba -> conda -> uv. Record first found in `resources.json -> local.env_manager`. Also run `conda env list` to record existing environments.

If none found, warn that `/refactor-init` needs mamba or conda.

### Models & Artifacts

| Type | Locations |
|------|-----------|
| HuggingFace cache | `~/.cache/huggingface/hub/` |
| Torch hub | `~/.cache/torch/hub/` |
| ONNX/TensorRT | `~/models/`, `~/weights/`, `D:\models\`, stage `artifacts/` |

### Data

Common dirs: `~/data/`, `~/datasets/`, `D:\data\`, `D:\datasets\`, `{PROJECT}/stages/*/data/`.

## Flow

### Step 1: Check resources.json first

If already has non-empty values for the requested type, show what's cached and ask whether to re-search. If user says no, use cached values.

### Step 2: Search

Run search for requested category. Report findings with types and sizes.

### Step 3: Auto-save

Show proposed writes to `{WORKSPACE}/resources.json`. User confirms (y/n/edit). Existing non-empty values are shown side-by-side with new values so the user can choose — overwriting silently would lose manual configuration.

### Step 4: If nothing found

Report what's missing, ask if user has credentials to provide manually (one field at a time).

## Usage modes

**Standalone** (`/resources`): ask what to search for (credentials / models / data / all), then follow the flow.

**Called by another skill**: check cache first -> if valid, return immediately -> if not, search relevant category only -> save -> return.

## AWS Credential Troubleshooting

When AWS SSO (`aws sso login`) fails:

| Problem | Symptom | Fix |
|---------|---------|-----|
| SSO config expired | `InvalidRequestException` on `RegisterClient` | Confirm `sso_start_url` with admin |
| Region mismatch | `InvalidRequestException` | Check `sso_region` matches Identity Center region |
| Network/proxy blocking | SSO hangs | Check corporate firewall/proxy |
| SSO cache corrupted | Various errors | Clear `~/.aws/sso/cache/` and retry |

**Fallback**: SSO fails -> try default profile -> try static credentials in `~/.aws/credentials` -> ask user. Report SSO error as warning so user can fix later, but continue with whatever works.

## Safety

Secret values (keys, passwords, tokens) are never displayed — only paths, profiles, and metadata. This protects against accidental exposure in logs or shared screens.
