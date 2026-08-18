---
name: resources
description: "Use this skill to discover and configure compute resources \u2014 SSH keys, AWS credentials, GPU servers, model files, data directories, and Python environment managers. Triggers when the user asks about available resources, credentials, servers, or when another skill needs non-local access. Use for: '\u770B\u770B\u6709\u4EC0\u4E48\u8D44\u6E90', 'scan for GPUs', 'find credentials', 'set up server access', 'what envs do I have'. Also called automatically by run skills when credentials are missing."
---

# /resources — Resource Discovery

Search the local machine for credentials, models, and data in common default locations. Can be invoked standalone or triggered by other skills when a resource is needed.

Ask one question at a time — multiple questions at once overwhelms users. **And only what only they know** — a value you can read is not a question, and a value nobody has is recorded absent rather than asked for: CLAUDE.md "Decide what evidence can decide".

Follow `lifecycle/references/skill-graph.md` -> "Workflow State Protocol": push on entry, update step as you progress, pop on completion.

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

### Rented compute providers

`servers` above is hardware the user owns; `compute` is accounts they can rent from. The
split is not cosmetic — a lease against an owned box creates nothing and bills nothing,
and a lease against a provider creates a machine that bills until something destroys it.

There is a CLI to look for, and that is all this step probes. Registering a provider is
otherwise **asked, not discovered**, for the same reason as `outsourcing`: having a
credential on disk does not mean the user intends MLClaw to spend on it.

| Provider | Tell | Liveness check |
|---|---|---|
| Nebius | `~/.nebius/bin/nebius` | `nebius iam whoami --format json` |
| Lambda | `~/.config/lambda/api_key` | `GET /instances` with the key as the Basic username |

**Probe liveness, never scope.** `whoami` is cheap and answers "is this credential
alive". Do **not** enumerate tenants, projects or capacity during `/resources` — that is
several dozen API calls, and every one of them is answered again by `lease.py capacity`
at the moment the answer is actually needed. What belongs in `resources.json` is the
preference (which region, which key, which image), never the discovered scope: ids drift,
and a pinned id is a wrong answer that looks like a configured one.

Ask, in this order, and only `cli_path` is required: `cli_path`, `ssh_public_key_path`
(which key the boxes should trust), `allow_regions` (usually the region the data bucket
is in — an out-of-region box pays cross-region egress on every pull), `image_family`,
`boot_disk_gib`. The template's `_example_nebius` block lists them with the reasoning.

**What to ask differs per provider, and the `_example_<name>` block in the template is
the authority for which.** Lambda takes no image or disk keys at all — its launch API has
no such fields — and instead takes `ssh_key_names` (keys already registered in its
console; a box created without one can never be reached) and a **`dead_man_key_path`**
without which `up` refuses. That refusal is worth explaining when you ask: Lambda
instances cannot be stopped, only terminated, so a guest-side `shutdown -h` stops no
billing and removes the last way in. The only switch that works calls the terminate API
from the box, which puts a key on rented hardware — a decision for the user, which is why
it is a separate key from `api_key_path` and why it is asked rather than assumed.

**A federated / SSO credential's TTL is a fact worth recording at registration time**,
because Money rule 5 refuses a job longer than it and the refusal is much cheaper than
the alternative: a token that expires mid-run leaves boxes that cannot be reached,
monitored or destroyed, and that are still billing.

### Python Environment Manager

Check in preference order: **pixi -> mamba -> conda -> uv** — `pixi` first because it pins a per-directory lockfile, so an env it resolves can be rebuilt from the repo instead of depending on a named env still existing on somebody's machine. Record first found in `resources.json -> local.env_manager`. Also run `conda env list` (and `pixi info` where present) to record existing environments.

If none found, warn that `/refactor-init` needs pixi, mamba or conda.

### Models & Artifacts

| Type | Locations |
|------|-----------|
| HuggingFace cache | `~/.cache/huggingface/hub/` |
| Torch hub | `~/.cache/torch/hub/` |
| ONNX/TensorRT | `~/models/`, `~/weights/`, `D:\models\`, stage `artifacts/` |

### Data

Common dirs: `~/data/`, `~/datasets/`, `D:\data\`, `D:\datasets\`, `{PROJECT}/stages/*/data/`.

### Outsourcing parties

The one category with **nothing to scan for** — an annotation vendor leaves no trace on the
filesystem. It is here because `resources.json -> outsourcing` is where they belong, for the
same reason `servers` is: a route to a capability the user doesn't own, registered once and reused
by every batch, workspace-level because a vendor is not project-specific.

So this category is asked, not discovered, and only ever on demand: `/data-label` sends the user here
when they name a party for the second time. Never volunteer a "let's register your vendors" pass —
there is no list to enumerate and the questions would be pure interrogation.

Fields worth asking for, in this order: `name`, `kind` (annotation / data_owner / reviewer /
customer), contact (person + one reachable channel — email or an IM handle is usually enough; do not
collect a phone number just because the field exists), `channel` + `channel_ref` (how batches
actually travel), `turnaround_days`, `spec_default`, `rate`. Only `name` is required; the rest earn
their place by being reused.

`turnaround_days` is the one that pays for itself immediately — it is what lets `/data-label` propose
a real `--due` instead of asking the user to invent one.

**This is personal data in the never-committed file.** That is deliberate and it is the reason
handoff records store only the `outsourcing` key: `{PROJECT}/handoffs/` is git-tracked, so a contact
copied into a handoff record is a contact committed to the project repo. Resolve contact details
live when reporting; never write them into a project file.

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
