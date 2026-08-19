#!/usr/bin/env python3
"""provider_nebius — Nebius Cloud adapter for /lease.

Contract: `skills/lease/references/contract.md`. Shape/price table:
`machines_nebius.json`. Fleet-level rules this serves: `references/fleet.md`.

  machine_type   `<region>:<platform>/<preset>@<pool>`, e.g.
                 `eu-north1:gpu-h100-sxm/1gpu-16vcpu-200gb@preemptible`
                 Everything `up` needs is in the string, because L2 passes nothing else.
                 `pool` is `on_demand` or `preemptible`; they are separate capacity rows
                 with separate stock and separate prices (contract, "Interruptible capacity").
  tag            `--labels mlclaw_tag=<tag>` at create. Money rule 4; also the only
                 ownership evidence on a shared tenant, where creation time proves nothing.
  dead-man       guest-side `shutdown -h +N` from cloud-init, re-armed by `renew` over ssh.
                 Nebius has no native TTL. See machines_nebius.json -> capabilities for why
                 this is only half a switch and why `--recovery-policy fail` is unconditional.

NOTHING IN THIS FILE NAMES A TENANT, A PROJECT, A SUBNET OR AN IMAGE ID. All of it is
discovered live from the caller's own credential: tenants from `iam whoami`, projects from
`iam project list`, each project's region from its own spec, the subnet from the project.
That is partly hygiene — ids are private infrastructure and this repo is public-shaped —
and partly correctness: ids drift, a pinned one silently points at the wrong place, and the
whole first trap in "Scope completeness" is exactly this failure.

  provider_nebius.py capacity [--gpu-count N] [--gpu-memory-gb G] [--arch-min sm_90]
  provider_nebius.py up --machine-type T --ttl-s N [--tag ...] [--run ...] [--project ...]
  provider_nebius.py addr|state|down <instance_id>
  provider_nebius.py renew <instance_id> --ttl-s N
  provider_nebius.py sweep [--tag-prefix <prefix>]
  provider_nebius.py history [--tag-prefix P] [--instance-id ID] [--window-s N]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (SSH_OPTS, TAG_PREFIX, add_shape_args, die, emit,  # noqa: E402
                     fan_out, load_resources, parse_arch,
                     resources_from_workspace_root, sweep_result)

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "machines_nebius.json")

CALL_TIMEOUT = 120
UP_POLL_TIMEOUT = 900   # 15 min. A create that has not become reachable by then is not
                        # slow, it is the silent STARTING->STOPPED placement failure.
UP_POLL_EVERY = 15

POOLS = ("on_demand", "preemptible")

# Provider state -> the contract's five. A table rather than an inline dict so a check can
# read it: the two entries below are the ones that cost money when they drift.
#
#   STOPPED -> `running`, NOT `gone`. Compute billing stops and storage billing does not,
#   so `gone` here teaches the caller the money stopped — and a fleet of stopped boxes
#   with multi-TB boot disks then bills indefinitely while every "what's running" report
#   says zero. A preempted box is also STOPPED, and it is genuinely still held.
#
#   STOPPING -> `running` for the same reason: it has not finished going away.
STATE_MAP = {"RUNNING": "running", "STARTING": "pending", "CREATING": "pending",
             "STOPPING": "running", "STOPPED": "running", "DELETING": "stopping",
             "ERROR": "failed", "FAILED": "failed"}
MT_RE = re.compile(r"^(?P<region>[a-z0-9-]+):(?P<platform>[a-z0-9-]+)/"
                   r"(?P<preset>[a-z0-9-]+)@(?P<pool>on_demand|preemptible)$")


# --- config -------------------------------------------------------------------

def table():
    with open(TABLE) as fh:
        return json.load(fh)


def conf(res_path):
    """`resources.json -> compute.nebius`, over `machines_nebius.json -> defaults`.

    The registry holds the machine-specific half (which key, which user, which region is
    preferred) and the table holds the provider-wide half. Neither holds an id.
    """
    res = load_resources(res_path)
    block = (res.get("compute") or {}).get("nebius") or {}
    cfg = dict(table()["defaults"])
    cfg.update({k: v for k, v in block.items() if not k.startswith("_")})
    cfg.setdefault("cli_path", "~/.nebius/bin/nebius")
    cfg["cli_path"] = os.path.expanduser(cfg["cli_path"])
    if not os.path.exists(cfg["cli_path"]):
        die("permission", f"nebius CLI not found at {cfg['cli_path']}",
            hint="install it, or set resources.json -> compute.nebius.cli_path")
    return cfg


# --- CLI plumbing -------------------------------------------------------------

def classify(text):
    """Provider error string -> contract error class.

    `no_capacity` and `quota` prescribe opposite next actions — wait/retry elsewhere
    versus open a ticket — so the contract says to return `quota` when they cannot be
    told apart. Nebius is unusually good here: `LIMIT_REACHED` really is the account
    limit, and a missing `available` really is empty stock, so `capacity` distinguishes
    them from the advice payload rather than from an error string.
    """
    t = (text or "").lower()
    if "unauthenticated" in t or "token" in t and "expired" in t:
        return "credential_expired"
    if "permissiondenied" in t or "permission denied" in t or "invalidnid" in t:
        return "permission"
    if "only \"fail\" recovery policy" in t or "validation error" in t or "invalidargument" in t:
        return "permission"
    if "resourceexhausted" in t or "quota" in t or "limit" in t:
        return "quota"
    if "no capacity" in t or "not enough capacity" in t or "cannot be placed" in t:
        return "no_capacity"
    return "transient"


def neb(cfg, *args, timeout=CALL_TIMEOUT):
    """(data, err_text). Never filters stderr.

    box.sh piped create output through `sed -n '/^{/,$p'` to keep only JSON, and an API
    error is not JSON — so the one message that said what was actually wrong was thrown
    away and surfaced as a generic failure. Contract: `fleet.md` traps, "A create error
    swallowed by output filtering". Both streams are captured, and stderr is what gets
    classified.
    """
    cmd = [cfg["cli_path"], *map(str, args), "--format", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout}s: {' '.join(map(str, args[:4]))}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "unknown failure").strip()
    out = proc.stdout.strip()
    if not out:
        return {}, None
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, f"non-JSON response: {out[:300]}"


def paged(cfg, *args):
    """Every page. `next_page_token` is checked even on lists that "obviously" fit —
    contract "Scope completeness" rule 2. One page taken for the whole list is an
    under-report that presents as an inventory."""
    items, token, guard = [], None, 0
    while True:
        guard += 1
        extra = ["--page-token", token] if token else []
        data, err = neb(cfg, *args, *extra)
        if err:
            return items, err
        items += data.get("items") or []
        token = data.get("next_page_token") or None
        if not token or guard >= 50:
            return items, None


# --- scope --------------------------------------------------------------------

def tenants(cfg):
    """The whole `whoami` payload, never a truncated one.

    The tenant list sits at the end of the response, so any head/limit on it drops
    tenants — and every later query then covers a subset while looking exhaustive.
    Contract "Scope completeness"; `fleet.md` traps, "Truncating an identity/scope
    response".
    """
    data, err = neb(cfg, "iam", "whoami")
    if err:
        die(classify(err), f"whoami failed: {err}",
            hint="federated tokens are short-lived; run any nebius command in an "
                 "interactive shell to refresh — the OAuth flow cannot complete headlessly")
    profile = data.get("user_profile") or data
    ids = [t["tenant_id"] for t in (profile.get("tenants") or []) if t.get("tenant_id")]
    if not ids:
        die("permission", "whoami returned no tenants")
    return ids


def projects(cfg, allow_regions=None):
    """[(project_id, name, region, tenant)] across every tenant, plus what went unread.

    A bare `instance list` uses the single parent-id in the CLI's own config and answers
    about one project — printing `{}` for an account with machines in eight others. This
    walk is what makes an empty result mean empty.
    """
    out, unreached = [], []
    for tenant in tenants(cfg):
        items, err = paged(cfg, "iam", "project", "list", "--parent-id", tenant)
        if err:
            unreached.append({"scope": tenant, "why": f"project list: {err[:160]}"})
            continue
        for item in items:
            meta, spec = item.get("metadata") or {}, item.get("spec") or {}
            region = spec.get("region") or (item.get("status") or {}).get("region")
            if not meta.get("id"):
                continue
            if allow_regions and region not in allow_regions:
                continue
            out.append((meta["id"], meta.get("name") or "?", region, tenant))
    return out, unreached


# --- capacity -----------------------------------------------------------------

def advice_rows(cfg):
    """`capacity resource-advice list` per TENANT, folded across placement domains.

    Two asymmetries live here and both are easy to trip on:

    * this endpoint wants a **tenant** parent-id and rejects a project, while every
      resource list wants the opposite;
    * it returns one row **per fabric**, so the same platform/preset/region appears many
      times with different free counts. Reading the first under-reports; summing them
      over-reports the moment quota binds first. fleet.md "Placement: capacity is not
      one number" states the fold: `min(sum over domains, quota)`, and the largest single
      domain for a single instance — which is what an `up` can actually place.
    """
    folded, unreached = {}, []
    for tenant in tenants(cfg):
        items, err = paged(cfg, "capacity", "resource-advice", "list", "--parent-id", tenant)
        if err:
            unreached.append({"scope": tenant, "why": f"resource-advice: {err[:160]}"})
            continue
        for item in items:
            spec, status = item.get("spec") or {}, item.get("status") or {}
            inst = spec.get("compute_instance") or {}
            preset = inst.get("preset") or {}
            rs = preset.get("resources") or {}
            for pool in POOLS:
                p = status.get(pool)
                if not p:
                    continue
                key = (spec.get("region"), inst.get("platform"), preset.get("name"), pool)
                cell = folded.setdefault(key, {
                    "domains": 0, "free_sum": 0, "free_max": 0, "limit": None,
                    "levels": set(), "gpu_count": rs.get("gpu_count"),
                    "vcpu": rs.get("vcpu_count"), "host_ram_gb": rs.get("memory_gibibytes"),
                    "gpu_memory_gb": inst.get("gpu_memory_gigabytes"),
                })
                # An ABSENT `available` is zero free, not unknown. This is the single most
                # expensive misread on this provider: quota reads healthy, the field is
                # simply not there, and the create then passes the quota check and cannot
                # be placed. `fleet.md` traps, "Created is not usable".
                free = p.get("available") or 0
                cell["domains"] += 1
                cell["free_sum"] += free
                cell["free_max"] = max(cell["free_max"], free)
                cell["levels"].add((p.get("availability_level") or "").replace(
                    "AVAILABILITY_LEVEL_", ""))
                if p.get("limit") is not None:
                    cell["limit"] = max(cell["limit"] or 0, p["limit"])
    return folded, unreached


def v_capacity(args):
    cfg = conf(args.res)
    tab = table()["platforms"]
    want_arch = parse_arch(args.arch_min)
    folded, unreached = advice_rows(cfg)
    allow = cfg.get("allow_regions")

    rows = []
    for (region, platform, preset, pool), cell in folded.items():
        if allow and region not in allow:
            continue
        spec = tab.get(platform) or {}
        arch = parse_arch(spec.get("arch"))
        limits = []
        if args.gpu_count and (cell["gpu_count"] or 0) < args.gpu_count:
            limits.append(f"preset has {cell['gpu_count']} GPU(s) < {args.gpu_count} required")
        if args.gpu_memory_gb and (cell["gpu_memory_gb"] or 0) + 0.5 < args.gpu_memory_gb:
            limits.append(f"{cell['gpu_memory_gb']}GB VRAM < {args.gpu_memory_gb}GB required")
        if want_arch and arch and arch < want_arch:
            limits.append(f"{spec.get('arch')} < {args.arch_min} required")
        if args.host_ram_gb and (cell["host_ram_gb"] or 0) + 1 < args.host_ram_gb:
            limits.append(f"host RAM {cell['host_ram_gb']}GB < {args.host_ram_gb}GB required")

        placeable = min(cell["free_sum"], cell["limit"]) if cell["limit"] is not None \
            else cell["free_sum"]
        if not limits:
            if "LIMIT_REACHED" in cell["levels"] and not placeable:
                # The account limit, not the hardware. Opposite remedy from no stock, and
                # the one case where a ticket is the right move rather than a retry.
                limits.append(f"account limit reached (quota={cell['limit']})")
            elif not placeable:
                limits.append(f"no free stock in {region} "
                              f"(quota={cell['limit']}, {cell['domains']} placement domain(s))")

        price_gpu = spec.get("price_gpu_hr")
        rows.append({
            "region": region,
            "machine_type": f"{region}:{platform}/{preset}@{pool}",
            "label": f"{spec.get('gpu') or platform} x{cell['gpu_count']}"
                     f"{' preemptible' if pool == 'preemptible' else ''} — {region}",
            "avail": 0 if limits else placeable,
            "max_single_instance": 0 if limits else cell["free_max"],
            "price_hr": round(price_gpu * (cell["gpu_count"] or 1), 2)
            if price_gpu is not None else None,
            # The caller quotes this number at people. Where it came from a note on a
            # console page rather than an API, it says so — contract, "A hand-maintained
            # `price_hr` is a claim".
            "price_status": spec.get("price_status") if price_gpu is not None else "unknown",
            "price_asof": spec.get("price_asof"),
            "arch": spec.get("arch"), "arch_status": spec.get("arch_status"),
            "gpu_count": cell["gpu_count"], "gpu_memory_gb": cell["gpu_memory_gb"],
            "host_ram_gb": cell["host_ram_gb"], "vcpu": cell["vcpu"],
            "pool": pool,
            "preemptible": pool == "preemptible",
            "availability_level": "/".join(sorted(x for x in cell["levels"] if x)) or None,
            "binding_limit": "; ".join(limits) or None,
        })
    rows.sort(key=lambda r: (-r["avail"], r["price_hr"] if r["price_hr"] is not None
                             else float("inf")))
    # Not the sweep envelope — `capacity` returns a list per the contract. The unreached
    # tenants still have to reach the caller, so they ride as a row with avail 0 whose
    # binding_limit says nobody looked. Silently dropping them is how "no capacity
    # anywhere" gets reported after one API timeout.
    for miss in unreached:
        rows.append({"region": None, "machine_type": None, "label": f"unread: {miss['scope']}",
                     "avail": 0, "price_hr": None, "binding_limit": f"NOT CHECKED — {miss['why']}"})
    emit(rows)


# --- identity helpers ---------------------------------------------------------

def parse_mt(machine_type):
    m = MT_RE.match(machine_type or "")
    if not m:
        die("permission", f"unparseable machine_type {machine_type!r}",
            hint="expected <region>:<platform>/<preset>@<on_demand|preemptible>, "
                 "exactly as a `capacity` row reports it")
    return m.group("region"), m.group("platform"), m.group("preset"), m.group("pool")


def label_safe(value):
    """Nebius label values are a restricted charset; anything else is rejected at create
    and the whole `up` fails on a cosmetic field."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))[:63]


def project_in(cfg, region):
    found, unreached = projects(cfg, allow_regions=[region])
    if not found:
        die("permission", f"no project in region {region}"
            + (f" (unread: {[u['scope'] for u in unreached]})" if unreached else ""),
            hint="the credential may not reach the tenant that holds it")
    preferred = cfg.get("project_name")
    for pid, name, _, _ in found:
        if preferred and name == preferred:
            return pid, name
    return found[0][0], found[0][1]


def subnet_in(cfg, project_id):
    items, err = paged(cfg, "vpc", "subnet", "list", "--parent-id", project_id)
    if err or not items:
        die(classify(err) if err else "permission",
            f"no subnet in {project_id}: {err or 'empty list'}")
    return items[0]["metadata"]["id"]


def get_instance(cfg, instance_id):
    data, err = neb(cfg, "compute", "instance", "get", "--id", instance_id)
    if err:
        low = err.lower()
        if "notfound" in low or "not found" in low:
            return None, None
        return None, err
    return data, None


def public_ip(inst):
    for nic in ((inst.get("status") or {}).get("network_interfaces") or []):
        addr = (nic.get("public_ip_address") or {}).get("address")
        if addr:
            return addr.split("/")[0]
    return None


# --- up -----------------------------------------------------------------------

def cloud_init(cfg, ttl_s):
    """ssh key plus the dead-man switch, which is the only reason this is not optional.

    `shutdown -h +N` is armed at first boot and re-armed by `renew`. It is scheduled in
    the guest, so a reboot loses it — contained only because `--recovery-policy fail`
    means nothing restarts this box behind our back. Which is the same flag that makes
    the switch work at all: under the default `recover`, a guest poweroff is treated as
    a failure and the instance is restarted **and billed**.
    """
    key_path = os.path.expanduser(cfg.get("ssh_public_key_path") or "~/.ssh/id_ed25519.pub")
    try:
        with open(key_path) as fh:
            pubkey = fh.read().strip()
    except OSError:
        die("permission", f"cannot read ssh public key at {key_path}",
            hint="set resources.json -> compute.nebius.ssh_public_key_path")
    user = cfg.get("ssh_user", "ubuntu")
    return (f"#cloud-config\n"
            f"users:\n"
            f"  - name: {user}\n"
            f"    sudo: ALL=(ALL) NOPASSWD:ALL\n"
            f"    shell: /bin/bash\n"
            f"    ssh_authorized_keys: [{pubkey}]\n"
            f"runcmd:\n"
            f"  - shutdown -h +{max(1, ttl_s // 60)}\n"
            f"  - touch /home/{user}/MLCLAW_READY\n"
            f"  - chown {user}:{user} /home/{user}/MLCLAW_READY\n")


def find_by_tag(cfg, project_id, tag):
    """Go and look. Called after a create whose response we could not read.

    A create that succeeded while its response was lost leaves an instance billing and no
    local id — and `down` then says "nothing to destroy". The lease row already exists
    (Money rule 1), so what is missing is only the id, and the tag we set at create is
    exactly how to recover it. `fleet.md` traps, "'Failed to return an id' treated as
    'nothing was created'".
    """
    items, _ = paged(cfg, "compute", "instance", "list", "--parent-id", project_id)
    return [i["metadata"]["id"] for i in items
            if ((i.get("metadata") or {}).get("labels") or {}).get("mlclaw_tag") == label_safe(tag)]


def v_up(args):
    cfg = conf(args.res)
    region, platform, preset, pool = parse_mt(args.machine_type)
    project_id, project_name = project_in(cfg, region)
    subnet = subnet_in(cfg, project_id)
    expires = int(time.time()) + args.ttl_s
    name = label_safe(f"{TAG_PREFIX}{args.run or 'run'}-{int(time.time())}").lower()[:63]

    netif = json.dumps([{"name": "eth0", "subnet_id": subnet,
                         "ip_address": {}, "public_ip_address": {}}])
    argv = [
        "compute", "instance", "create", "--parent-id", project_id, "--name", name,
        "--resources-platform", platform, "--resources-preset", preset,
        "--boot-disk-attach-mode", "read_write",
        "--boot-disk-managed-disk-name", f"{name}-boot",
        "--boot-disk-managed-disk-type", cfg.get("boot_disk_type", "network_ssd"),
        "--boot-disk-managed-disk-size-gibibytes", str(cfg.get("boot_disk_gib", 512)),
        "--boot-disk-managed-disk-source-image-family-image-family",
        cfg.get("image_family", "ubuntu24.04-cuda12"),
        "--network-interfaces", netif,
        "--cloud-init-user-data", cloud_init(cfg, args.ttl_s),
        # Unconditional, both halves. `fail` is required for preemptible, and is also what
        # keeps the guest-side dead-man switch from being undone by auto-recovery.
        "--recovery-policy", "fail",
        "--labels", f"mlclaw_tag={label_safe(args.tag)}",
        "--labels", f"mlclaw_expires={expires}",
        "--labels", f"mlclaw_run={label_safe(args.run)}",
        "--labels", f"mlclaw_project={label_safe(args.project)}",
    ]
    if pool == "preemptible":
        argv += ["--preemptible-on-preemption", "stop"]

    data, err = neb(cfg, *argv, timeout=300)
    instance_id = ((data or {}).get("metadata") or {}).get("id")
    if not instance_id:
        # Never retry blind: the box may exist and bill while we believe nothing happened.
        recovered = find_by_tag(cfg, project_id, args.tag)
        if recovered:
            instance_id = recovered[0]
        else:
            die(classify(err), f"create failed in {project_name}: {err or 'no instance id'}",
                checked_for_orphan=True, project=project_name)

    await_reachable(cfg, instance_id, args)   # returns only when the box answers
    emit([instance_id])


def await_reachable(cfg, instance_id, args):
    """`up` is not done until the box answers. Contract, "Created is not usable".

    The failure this exists for: create returns success, the instance appears in the list,
    and it then goes STARTING -> STOPPED with no message, no reason and no address, because
    it passed the quota check and could not be placed. It reads as a working box for as
    long as nobody tries to reach it — which for a fleet means every slot in the pool.
    """
    deadline = time.time() + UP_POLL_TIMEOUT
    seen_ip = None
    while time.time() < deadline:
        inst, err = get_instance(cfg, instance_id)
        if err:
            time.sleep(UP_POLL_EVERY)
            continue
        if inst is None:
            die("no_capacity", f"{instance_id} disappeared before becoming reachable")
        state = ((inst.get("status") or {}).get("state") or "").upper()
        seen_ip = public_ip(inst) or seen_ip
        if state == "RUNNING" and seen_ip:
            return True
        if state in ("STOPPED", "ERROR", "FAILED") and not seen_ip:
            # Placement failure. The remains still hold a boot disk, so tearing down is
            # part of answering, not a courtesy.
            teardown(cfg, instance_id)
            die("no_capacity",
                f"{args.machine_type} was created and never came up (state {state}, no "
                f"address) — this is placement, not quota; the instance has been deleted",
                binding_limit="created but could not be placed",
                hint="the preemptible pool often has stock when on-demand has none")
        time.sleep(UP_POLL_EVERY)
    die("transient",
        f"{instance_id} still not reachable after {UP_POLL_TIMEOUT}s — LEFT RUNNING AND "
        f"BILLING on purpose so it stays visible; release the lease to remove it",
        instance_id=instance_id)


# --- addr / state / down / renew ----------------------------------------------

def v_addr(args):
    cfg = conf(args.res)
    inst, err = get_instance(cfg, args.instance_id)
    if err:
        die(classify(err), f"addr: {err}")
    if inst is None:
        die("permission", f"{args.instance_id} does not exist")
    ip = public_ip(inst)
    if not ip:
        die("transient", f"{args.instance_id} has no public address yet")
    # Resolved live every time, never cached: a stop/start hands the box a different
    # address and hands the old one to somebody else.
    emit(f"ssh://{cfg.get('ssh_user', 'ubuntu')}@{ip}")


def v_state(args):
    cfg = conf(args.res)
    inst, err = get_instance(cfg, args.instance_id)
    if err:
        die(classify(err), f"state: {err}")
    if inst is None:
        emit("gone")
        return
    state = ((inst.get("status") or {}).get("state") or "").upper()
    emit(STATE_MAP.get(state, "pending"))


def teardown(cfg, instance_id):
    neb(cfg, "compute", "instance", "delete", "--id", instance_id, timeout=300)


def v_down(args):
    """Money rule 2: not done until nothing bills. On this provider that means the boot
    disk too — an instance delete that leaves a multi-TB volume behind is a box that
    reports `gone` while the meter runs."""
    cfg = conf(args.res)
    inst, err = get_instance(cfg, args.instance_id)
    if err:
        die(classify(err), f"down: could not read {args.instance_id}: {err}")

    tag = None
    if inst is not None:
        tag = ((inst.get("metadata") or {}).get("labels") or {}).get("mlclaw_tag")
        parent = (inst.get("metadata") or {}).get("parent_id")
        _, derr = neb(cfg, "compute", "instance", "delete", "--id", args.instance_id,
                      timeout=300)
        if derr and "notfound" not in derr.lower():
            die(classify(derr), f"delete failed for {args.instance_id}: {derr}")
    else:
        parent = None  # already gone; idempotent success, but still sweep for the disk

    residual = []
    if parent and tag:
        disks, _ = paged(cfg, "compute", "disk", "list", "--parent-id", parent)
        for d in disks:
            meta = d.get("metadata") or {}
            if (meta.get("labels") or {}).get("mlclaw_tag") != tag:
                continue
            _, derr = neb(cfg, "compute", "disk", "delete", "--id", meta["id"], timeout=300)
            if derr and "notfound" not in derr.lower():
                residual.append({"disk": meta["id"], "why": derr[:160]})
    if residual:
        die("transient", "instance deleted but storage survives — STILL BILLING",
            residual=residual)
    emit({"ok": True, "instance_id": args.instance_id, "residual_billing": False})


def v_renew(args):
    """Re-arm the guest-side switch. Must fail loudly when the hold is already gone —
    a renew that silently succeeds against a dead box is how a fleet believes it holds
    machines it does not."""
    cfg = conf(args.res)
    inst, err = get_instance(cfg, args.instance_id)
    if err:
        die(classify(err), f"renew: {err}")
    if inst is None:
        die("permission", f"{args.instance_id} no longer exists — acquire a fresh lease")
    ip = public_ip(inst)
    if not ip:
        die("transient", f"{args.instance_id} is unreachable; cannot re-arm its expiry")
    minutes = max(1, args.ttl_s // 60)
    cmd = ["ssh", *SSH_OPTS, f"{cfg.get('ssh_user', 'ubuntu')}@{ip}",
           f"sudo shutdown -c 2>/dev/null; sudo shutdown -h +{minutes}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        die("transient", f"could not re-arm the dead-man switch on {ip}: "
                         f"{(proc.stderr or '').strip()[:200]}")
    expires = int(time.time()) + args.ttl_s
    neb(cfg, "compute", "instance", "update", "--id", args.instance_id,
        "--labels", f"mlclaw_expires={expires}")
    emit({"expires_at": expires, "instance_id": args.instance_id})


# --- sweep / history ----------------------------------------------------------

def volume_attachment(obj):
    """Who is holding this volume, read from the VOLUME and keyed by INSTANCE ID.

    ‼️ This reads the volume side, and the first version of this file read the instance
    side on the reasoning that an API states attachment more reliably from the holder.
    On this provider that is backwards, and the real listing said so: EVERY disk came
    back `attached_to: null`, including the boot disk of a box that was RUNNING at the
    time. A **managed** boot disk is declared in the instance spec by NAME plus an inline
    spec — `boot_disk.managed_disk.name` — and the instance never carries the disk's id
    at all. The disk, meanwhile, carries `status.read_write_attachment` and
    `status.managed_by`, both of which ARE instance ids.

    So the rule is not "read it from the instance", it is **read it from whichever side
    names the other by id**, and record which side answered. Getting this wrong is not a
    cosmetic error: every live machine's boot disk reads as an orphaned volume, which is
    precisely the direction that sends a reap after a running box's data.

    Returns `(instance_id, how)`; `how` is `None` when the volume says nothing.
    """
    st = obj.get("status") or {}
    for key in ("read_write_attachment", "managed_by"):
        if st.get(key):
            return st[key], key
    for key in ("read_only_attachments", "attachments"):
        vals = st.get(key) or []
        if isinstance(vals, list) and vals:
            first = vals[0]
            return (first.get("instance_id") or first.get("id")
                    if isinstance(first, dict) else first), key
    return None, None


def instance_volume_names(instances):
    """name -> instance id, for volumes the instance declares WITHOUT an id.

    A weaker channel and used only where the volume itself said nothing. fleet.md "Group
    by id, never by name" is about joins ACROSS TIME, where a reused name reports a live
    box as released; this is a join inside one listing of one project, where the provider
    offers no id to use. It is still marked `instance_name_match` on the row, because a
    renamed volume silently drops out of it — and the failure direction there is a false
    orphan, which is the expensive one.
    """
    out = {}
    for inst in instances:
        iid = (inst.get("metadata") or {}).get("id")
        spec = inst.get("spec") or {}
        holders = [spec.get("boot_disk") or {}, *(spec.get("secondary_disks") or []),
                   *(spec.get("filesystems") or [])]
        for holder in holders:
            for key in ("managed_disk", "existing_disk", "existing_filesystem"):
                ref = (holder or {}).get(key) or {}
                if isinstance(ref, dict) and ref.get("name"):
                    out.setdefault(ref["name"], iid)
    return out


def _size_gib(spec, status=None):
    for src in (spec, status or {}):
        if src.get("size_gibibytes"):
            return int(src["size_gibibytes"])
        if src.get("size_bytes"):
            return round(int(src["size_bytes"]) / (1024 ** 3))
    return None


def storage_rows(cfg, pid, project, region, instances, tag_prefix, now):
    """Disks and filesystems, with what is holding each.

    Both are billed the moment they exist and go on billing when every instance is
    STOPPED or deleted — `instance delete` takes a managed boot disk with it, but an
    orphaned one (`--boot-disk-managed-disk-forbid-deletion`, a create whose instance
    never materialised, a console launch) survives with nothing pointing at it and
    nothing enumerating it. That is the volume this exists to find."""
    by_name = instance_volume_names(instances)
    prices = table().get("storage") or {}

    rows = []
    for kind, argv in (("disk", ("compute", "disk", "list")),
                       ("filesystem", ("compute", "filesystem", "list"))):
        items, err = paged(cfg, *argv, "--parent-id", pid)
        if err:
            return None, f"{kind} list: {err[:160]}"
        for obj in items:
            meta = obj.get("metadata") or {}
            spec, status = obj.get("spec") or {}, obj.get("status") or {}
            tag = (meta.get("labels") or {}).get("mlclaw_tag") or ""
            if tag_prefix and not tag.startswith(tag_prefix):
                continue
            gib = _size_gib(spec, status)
            rate = (prices.get((spec.get("type") or "").lower()) or {}).get("price_gib_hr")
            held_by, how = volume_attachment(obj)
            if not held_by and meta.get("name") in by_name:
                held_by, how = by_name[meta["name"]], "instance_name_match"
            rows.append({
                "storage_id": meta.get("id"), "kind": kind, "tag": tag,
                "name": meta.get("name"), "size_gib": gib,
                # null, never 0: Nebius publishes no per-hour storage price and nobody
                # has written one into the table, so this is unmeasured. L2 counts it as
                # an unpriced row and says the total is a lower bound.
                "price_hr": round(rate * gib, 4) if (rate and gib) else None,
                "attached_to": held_by,
                # WHICH side answered. An id-keyed attachment is a fact; a name match is
                # a guess that a rename breaks. A reader deciding whether to delete this
                # volume needs to know which one it is looking at.
                "attached_by": how,
                # `managed_by` set means the instance owns this disk and `instance delete`
                # takes it. That is the difference between "release the box" and "release
                # the box and erase what is on it", and it is not visible anywhere else.
                "managed": bool(status.get("managed_by")),
                "state": status.get("state"),
                "age_s": max(0, now - _epoch(meta.get("created_at") or "")),
                "created_at": (meta.get("created_at") or "")[:19] or None,
                "project": project, "region": region,
            })
    return rows, None


def creators(cfg, window_s):
    """resource id -> the completed CREATE event that made it, across every tenant.

    THE ONLY PLACE OWNERSHIP LIVES on a shared account. The resource lists carry no owner
    at all, and `iam tenant-user-account list` returns account ids with `email` and `name`
    both null — so it can report that a tenant has six members while identifying none of
    them. `authentication.subject.name` on an audit event is the practical id->person map,
    and it is why this is a join against the log rather than a field on a listing.

    Creation time is NOT ownership. Several people launch boxes into the same projects on
    the same night, and reading "created last night" as "mine" is how two of a colleague's
    scene-generation boxes were once reported as this user's training machines.

    Returns `(by_id, checked, unreached)`. A resource whose CREATE falls outside the
    window is simply absent — the caller must render that as **unknown**, never as
    unowned and never as yours.
    """
    now = int(time.time())
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_s))
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    by_id, checked, unreached = {}, [], []
    for tenant in tenants(cfg):
        items, err = paged(cfg, "audit", "v2", "audit-event", "list",
                           "--parent-id", tenant, "--start", start, "--end", end,
                           "--event-type", "control_plane",
                           "--filter", "action = 'CREATE'", "--page-size", "500")
        if err:
            unreached.append({"scope": tenant, "why": f"audit CREATE: {err[:160]}"})
            continue
        checked.append(tenant)
        for ev in items:
            row = _audit_row(ev)
            # Only completed events. A CREATE that ended in ERROR left nothing behind —
            # capacity failures produce these constantly — and counting one invents a
            # machine, then attributes it to somebody.
            if not row or (row["outcome"] or "DONE") != "DONE":
                continue
            by_id.setdefault(row["instance_id"], row)
    return by_id, checked, unreached, {"start": start, "end": end}


def attribute(rows, by_id):
    """Stamp each row with who made it. Three states, and the third is the one that has
    to survive being read quickly:

      operator: "<name>"  operator_status: audit_create             — known
      operator: null      operator_status: no_create_event_in_window — LOOKED, DID NOT FIND
      (no keys at all)                                              — nobody asked

    The middle state is not "unowned" and is emphatically not "yours". A box older than
    the window produces it, and so does a tenant whose log did not answer.
    """
    for row in rows:
        key = row.get("instance_id") or row.get("storage_id")
        ev = by_id.get(key)
        row["operator"] = ev["actor"] if ev else None
        row["operator_status"] = "audit_create" if ev else "no_create_event_in_window"
        # A SEPARATE field from `created_at`, which comes off the resource's own metadata
        # and is authoritative. These can differ — the audit event is when the CREATE was
        # logged — and one silently overwriting the other is two authors for one value.
        row["created_at_audit"] = ev["at"] if ev else None


def v_sweep(args):
    cfg = conf(args.res)
    tab = table()["platforms"]
    found, unreached = projects(cfg, allow_regions=cfg.get("allow_regions"))
    now = int(time.time())

    def one(entry):
        pid, name, region, _ = entry
        items, err = paged(cfg, "compute", "instance", "list", "--parent-id", pid)
        if err:
            return name, None, None, err
        rows = []
        for inst in items:
            meta = inst.get("metadata") or {}
            labels = meta.get("labels") or {}
            tag = labels.get("mlclaw_tag") or ""
            if args.tag_prefix and not tag.startswith(args.tag_prefix):
                continue
            res_spec = (inst.get("spec") or {}).get("resources") or {}
            spec = tab.get(res_spec.get("platform")) or {}
            gpus = _preset_gpus(res_spec.get("preset"))
            price = spec.get("price_gpu_hr")
            created = meta.get("created_at") or ""
            rows.append({
                "instance_id": meta.get("id"), "tag": tag,
                "age_s": max(0, now - _epoch(created)),
                "price_hr": round(price * gpus, 2) if price is not None else None,
                "price_status": spec.get("price_status") if price is not None else "unknown",
                "expired": _expired(labels.get("mlclaw_expires"), now),
                "state": ((inst.get("status") or {}).get("state") or "").upper(),
                "name": meta.get("name"), "project": name, "region": region,
                "run": labels.get("mlclaw_run") or None,
                # Shape and birth, carried rather than left for the caller to re-fetch.
                # A `state` with no `platform` cannot be priced or explained, and `age_s`
                # alone cannot be shown to a person deciding whether to kill something.
                "platform": res_spec.get("platform"), "preset": res_spec.get("preset"),
                "created_at": created[:19] or None,
                "public_ip": public_ip(inst),
                "project_id": pid, "tenant": entry[3],
            })
        # Storage is swept from the SAME instance listing, in the same pass. Two passes
        # would let a box be created between them and read as an orphaned disk.
        store, serr = storage_rows(cfg, pid, name, region, items, args.tag_prefix, now)
        if serr:
            return name, rows, None, serr
        return name, rows, store, None

    units, storage, checked = [], [], []
    for name, rows, store, err in fan_out(found, one):
        if rows is None or store is None:
            unreached.append({"scope": name, "why": err})
            units += rows or []          # whatever did answer is still worth reporting
        else:
            checked.append(name)
            units += rows
            storage += store
    payload = sweep_result(units, checked, unreached, storage=storage)
    if args.attribute:
        by_id, a_checked, a_unreached, window = creators(cfg, args.attribute_window_s)
        attribute(units + storage, by_id)
        # A SEPARATE envelope, deliberately not folded into `scope`. `scope` answers "did
        # I enumerate every resource"; this answers "do I know who made them". An audit
        # log that timed out leaves the orphan list complete and the ownership unknown,
        # and merging the two would turn every attribution hiccup into a reap that calls
        # its own count a lower bound when it is not.
        payload["attribution"] = {
            "window": window, "complete": not a_unreached,
            "checked": a_checked, "unreached": a_unreached,
            "note": "operator is null for anything created before the window — that is "
                    "UNKNOWN, not unowned and not yours"}
    emit(payload)


def _preset_gpus(preset):
    m = re.match(r"^(\d+)gpu", preset or "")
    return int(m.group(1)) if m else 1


def _epoch(iso_ts):
    try:
        return int(time.mktime(time.strptime(iso_ts[:19], "%Y-%m-%dT%H:%M:%S"))
                   - time.timezone)
    except (ValueError, TypeError):
        return int(time.time())


def _expired(value, now):
    try:
        return int(value) <= now
    except (TypeError, ValueError):
        # No expiry label means this box was not created by this layer, or was created
        # before it set one. Unknown is not "fine" — an untagged box is exactly what
        # `reap` is looking for, so it is reported rather than assumed live.
        return False


AUDIT_ACTIONS = ("CREATE", "DELETE", "START", "STOP", "UPDATE")


def v_history(args):
    """The past tense: what happened to a machine, including one that no longer exists.

    Three rules the audit log forces, each learned by getting the opposite:

    * **tenant parent-id.** A project id is rejected outright — the exact inverse of every
      resource list, which needs a project.
    * **filter by action or drown.** GET/LIST are ~98% of the log; unfiltered, tens of
      pages cover a few hours.
    * **only completed events count.** A CREATE that ended in ERROR left nothing behind —
      capacity failures produce these routinely — and counting it invents machines.
    """
    cfg = conf(args.res)
    events, unreached, checked = [], [], []
    now = int(time.time())
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - args.window_s))
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    for tenant in tenants(cfg):
        ok_any = False
        for action in AUDIT_ACTIONS:
            # `--page-size` above 500 is rejected outright, and the whole call is
            # tenant-scoped: a project id fails with InvalidNid, the exact inverse of
            # every resource list in this file.
            items, err = paged(cfg, "audit", "v2", "audit-event", "list",
                               "--parent-id", tenant, "--start", start, "--end", end,
                               "--event-type", "control_plane",
                               "--filter", f"action = '{action}'", "--page-size", "500")
            if err:
                unreached.append({"scope": f"{tenant}/{action}", "why": err[:160]})
                continue
            ok_any = True
            events += [r for r in (_audit_row(ev) for ev in items) if r]
        if ok_any:
            checked.append(tenant)

    if args.instance_id:
        events = [e for e in events if e["instance_id"] == args.instance_id]
    events.sort(key=lambda e: e["at"] or "")
    by_id = {}
    for ev in events:
        by_id.setdefault(ev["instance_id"], []).append(ev)

    emit({"events": events,
          "verdicts": {iid: _verdict(evs) for iid, evs in by_id.items()},
          "supported": True, "window": {"start": start, "end": end},
          "scope": {"complete": not unreached, "checked": checked, "unreached": unreached}})


def _audit_row(ev):
    """One event, flattened.

    ‼️ THE PAYLOAD IS ONE LEVEL DEEPER THAN IT LOOKS. Identity lives at
    `resource.metadata.{id,name}` — NOT `resource.{id,name}` — and reading the shallow
    path returned `name: None` on every event in a live log. The id survived only because
    of the regex fallback below, so nothing broke loudly; what broke quietly was every
    feature built on the name: `--name` filtering matched nothing, and `_verdict`'s `aka`
    was always empty, which means the rename tracking this layer makes a point of was
    dead while reading as implemented.

    `resource` also arrives as a bare string on some events, so every shape is probed and
    the regex is the last resort. Assuming one shape silently drops half the log, which
    then reads as "nothing happened".
    """
    resource = ev.get("resource")
    meta = (resource.get("metadata") or {}) if isinstance(resource, dict) else {}
    current = (((resource.get("state") or {}).get("current") or {})
               if isinstance(resource, dict) else {})
    cur_meta = current.get("metadata") or {}
    request = ev.get("request") if isinstance(ev.get("request"), dict) else {}

    rid = (meta.get("id") or cur_meta.get("id")
           or (resource.get("id") if isinstance(resource, dict) else None))
    name = (meta.get("name") or cur_meta.get("name")
            or (resource.get("name") if isinstance(resource, dict) else None)
            or (resource if isinstance(resource, str) else None)
            or request.get("name") or (request.get("metadata") or {}).get("name"))
    if not rid:
        match = re.search(r"computeinstance-[a-z0-9]+", json.dumps(ev))
        rid = match.group(0) if match else None
    if not rid:
        return None
    status = ev.get("status")
    subject = (ev.get("authentication") or {}).get("subject") or {}
    labels = (cur_meta.get("labels") or {}) or ((request.get("labels") or {}))
    return {"instance_id": rid, "name": name, "action": ev.get("action"),
            # The event's own type string (`ai.nebius.compute.computeinstance.create`)
            # plus the resource's short kind. A caller filtering to instances needs one
            # of these; inferring it from the id prefix works until a service names ids
            # differently.
            "type": ev.get("type"), "kind": meta.get("type"),
            "at": (ev.get("time") or "")[:19],
            # Ownership on a shared account. Creation time proves nothing — several
            # people launch boxes into the same projects on the same night, and the
            # resource lists do not carry an owner at all.
            "actor": subject.get("name") or subject.get("id"),
            # The account id alongside the name, because this log is the ONLY practical
            # id -> person map: `iam tenant-user-account list` returns members with
            # `email` and `name` both null, so it can report that a tenant has six
            # members while identifying none of them. Ids are per-tenant, so one human
            # legitimately has several — that is one person, not several accounts.
            "actor_id": subject.get("tenant_user_id") or subject.get("id"),
            "outcome": (status.get("code") if isinstance(status, dict) else status) or "",
            "tag": labels.get("mlclaw_tag")}


def _verdict(events):
    """The **last decisive event in time order**, not "does a DELETE appear somewhere":
    one id can be stopped, restarted and stopped again, and any-order matching reports
    whichever it happened to see.

    Only `DONE` counts. A CREATE that ended in ERROR left nothing behind — capacity
    failures produce these constantly — and counting it invents machines that never were.
    """
    done = [e for e in events if (e["outcome"] or "DONE") == "DONE"]
    decisive = [e["action"] for e in done if e["action"] in ("CREATE", "DELETE", "START", "STOP")]
    last = decisive[-1] if decisive else None
    names, seen = [], set()
    for e in reversed(done):
        if e["name"] and e["name"] not in seen:
            seen.add(e["name"])
            names.append(e["name"])
    verdict = {"DELETE": "RELEASED — deleted, nothing left",
               "STOP": "STOPPED — compute halted BUT DISK STILL BILLS"}.get(
                   last, "ALIVE? — created/started, nothing stopped it in this window")
    return {"verdict": verdict if last else "UNKNOWN — no completed lifecycle event",
            "name": names[0] if names else None,
            # A rename is one machine with two names, not two machines. Keeping the
            # aliases is what makes that visible rather than confusing.
            "aka": names[1:], "actor": done[-1]["actor"] if done else None,
            "first": done[0]["at"] if done else None,
            "last": done[-1]["at"] if done else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--resources")
    sub = ap.add_subparsers(dest="verb", required=True)

    c = sub.add_parser("capacity"); c.set_defaults(fn=v_capacity); add_shape_args(c)

    u = sub.add_parser("up"); u.set_defaults(fn=v_up); add_shape_args(u)
    u.add_argument("--machine-type", required=True)
    u.add_argument("--ttl-s", type=int, required=True, help="dead-man expiry; L2 owns the default")
    u.add_argument("--tag", help="owner token from L2; stored as a label, swept by prefix")
    u.add_argument("--run")
    u.add_argument("--project")

    for verb, fn in (("addr", v_addr), ("state", v_state), ("down", v_down)):
        p = sub.add_parser(verb); p.set_defaults(fn=fn)
        p.add_argument("instance_id")

    n = sub.add_parser("renew"); n.set_defaults(fn=v_renew)
    n.add_argument("instance_id")
    n.add_argument("--ttl-s", type=int, required=True)

    s = sub.add_parser("sweep"); s.set_defaults(fn=v_sweep)
    s.add_argument("--tag-prefix", default=TAG_PREFIX)
    s.add_argument("--attribute", action="store_true",
                   help="join each row against the audit log for who created it. Off by "
                        "default because it is the slowest call in the layer; needed "
                        "whenever the sweep is not scoped to this tool's own tag, since "
                        "an untagged box on a shared account may be a colleague's")
    s.add_argument("--attribute-window-s", type=int, default=5 * 86400,
                   help="how far back to look for the CREATE. A box older than this gets "
                        "operator null, which means unknown")

    h = sub.add_parser("history"); h.set_defaults(fn=v_history)
    h.add_argument("--tag-prefix", default=TAG_PREFIX)
    h.add_argument("--instance-id")
    h.add_argument("--window-s", type=int, default=5 * 86400)

    args = ap.parse_args()
    args.res = resources_from_workspace_root(args.resources)
    args.fn(args)


if __name__ == "__main__":
    main()
