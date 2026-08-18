#!/usr/bin/env python3
"""provider_ssh — static-machine adapter for /lease.

Implements the seven verbs of `.claude/skills/lease/references/contract.md` against
machines in `resources.json -> servers`. Rationale for treating an owned box as a
provider, and for putting the claim marker on the target host, is in the contract
("Where this sits", static-box paragraph) — not repeated here.

Adapter-specific facts:
  instance_id    `ssh:<server_key>:<i,j>` — opaque to callers, this file owns it
  claim marker   `~/.mlclaw/claims/gpu<i>.claim` on the target, one per GPU, O_EXCL
  machine_type   the server key, so a capacity row round-trips into `up`; `label` displays
  no table       attributes are probed live with nvidia-smi, not looked up

Usage
  provider_ssh.py capacity [--gpu-count N] [--gpu-memory-gb G] [--arch-min sm_89]
                           [--host-ram-gb R] [--server KEY]
  provider_ssh.py up --machine-type SERVER_KEY --ttl-s N --tag TAG [shape flags]
                     [--run RUN_ID] [--project NAME]
  provider_ssh.py addr|state|down INSTANCE_ID
  provider_ssh.py renew INSTANCE_ID --ttl-s N
  provider_ssh.py sweep [--tag-prefix mlclaw-]
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (SSH_UNREACHABLE, TAG_PREFIX, add_shape_args, die, emit,  # noqa: E402
                     fan_out, load_resources, parse_arch, resources_from_workspace_root,
                     sweep_result)

PROBE_TIMEOUT = 12   # nvidia-smi + a few small reads; 60s only ever hid a wedged host
CLAIM_TIMEOUT = 20

# Every remote script starts here: the claim dir, and one timestamp for the whole call.
PRELUDE = 'set -u; D="$HOME/.mlclaw/claims"; now=$(date +%s)\n'

# Fallback only, for drivers too old for --query-gpu=compute_cap. Such a driver predates
# Ampere, so there is no point listing cards newer than that.
NAME_ARCH = {"V100": 70, "T4": 75, "RTX 2": 75, "P100": 60, "GTX 10": 61}


# --- servers ------------------------------------------------------------------

def load_servers(path):
    data = load_resources(path)
    return {k: v for k, v in (data.get("servers") or {}).items() if not k.startswith("_")}


def pick(servers, key):
    if key not in servers:
        die("permission", f"no server '{key}' in resources.json -> servers",
            known=sorted(servers))
    return servers[key]


def endpoint(entry):
    """Alias-vs-host/user/port precedence, resolved once for both ssh and addr."""
    if entry.get("alias"):
        return entry["alias"], None
    host = entry.get("host")
    if not host:
        die("permission", "server entry has neither 'alias' nor 'host'")
    user, port = entry.get("username"), int(entry.get("port") or 22)
    return f"{user}@{host}" if user else host, port


# --- ssh ----------------------------------------------------------------------

def ssh_exec(entry, script, timeout=PROBE_TIMEOUT):
    """(rc, stdout). rc == SSH_UNREACHABLE means ssh itself could not connect.

    ControlMaster is what keeps the per-verb subprocess model affordable: the socket
    outlives this process, so a following `state` or `down` skips TCP+KEX+auth."""
    target, port = endpoint(entry)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/mlclaw-%C",
            "-o", "ControlPersist=60s"]
    if entry.get("ssh_key_path"):
        argv += ["-i", os.path.expanduser(entry["ssh_key_path"])]
    if port and port != 22:
        argv += ["-p", str(port)]
    try:
        proc = subprocess.run(argv + [target, "bash -s"], input=script,
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return SSH_UNREACHABLE, ""
    except FileNotFoundError:
        die("transient", "ssh binary not found on this machine")
    if proc.returncode == SSH_UNREACHABLE:
        low = proc.stderr.lower()
        if "denied" in low or "publickey" in low:
            die("permission", f"ssh auth failed: {proc.stderr.strip()}")
    return proc.returncode, proc.stdout


def ssh_or_die(entry, script, timeout=PROBE_TIMEOUT):
    rc, out = ssh_exec(entry, script, timeout)
    if rc == SSH_UNREACHABLE:
        die("transient", "ssh could not connect")
    return out


# --- one round trip per host --------------------------------------------------

INFO_SCRIPT = PRELUDE + (
    'printf "NOW=%s\\n" "$now"\n'
    'echo "==GPUS"\n'
    'nvidia-smi --query-gpu=index,name,memory.total,compute_cap '
    '--format=csv,noheader,nounits 2>/dev/null || '
    'nvidia-smi --query-gpu=index,name,memory.total '
    '--format=csv,noheader,nounits 2>/dev/null || true\n'
    'echo "==CLAIMS"\n'
    'for f in "$D"/gpu*.claim; do [ -f "$f" ] || continue\n'
    '  printf "@%s\\n" "$(basename "$f")"; cat "$f"\n'
    'done\n')

RAM_SCRIPT = 'echo "==RAM"; awk \'/MemTotal/{print $2}\' /proc/meminfo 2>/dev/null || true\n'


def arch_num(name, cc):
    if cc:
        try:
            major, minor = cc.strip().split(".")
            return int(major) * 10 + int(minor)
        except ValueError:
            pass
    return next((num for frag, num in NAME_ARCH.items() if frag in name), None)


def remote_info(entry, want_ram=False):
    """{now, gpus, claims, ram_gb} in ONE ssh connection.

    Returns None when unreachable — multi-server verbs report that host and carry on
    instead of aborting the whole sweep."""
    script = INFO_SCRIPT + (RAM_SCRIPT if want_ram else "")
    rc, out = ssh_exec(entry, script)
    if rc == SSH_UNREACHABLE:
        return None
    info = {"now": int(time.time()), "gpus": [], "claims": {}, "ram_gb": None}
    section, cur = None, None
    for line in out.splitlines():
        if line.startswith("NOW="):
            info["now"] = int(line[4:])
        elif line.startswith("=="):
            section, cur = line[2:], None
        elif section == "GPUS":
            cols = [c.strip() for c in line.split(",")]
            if len(cols) >= 3 and cols[0].isdigit():
                info["gpus"].append({
                    "index": int(cols[0]), "name": cols[1],
                    "memory_total_gb": round(float(cols[2]) / 1024, 1),
                    "arch": arch_num(cols[1], cols[3] if len(cols) > 3 else None)})
        elif section == "CLAIMS":
            if line.startswith("@"):
                idx = line[1:].removeprefix("gpu").removesuffix(".claim")
                cur = info["claims"].setdefault(int(idx), {}) if idx.isdigit() else None
            elif cur is not None and "=" in line:
                k, _, v = line.partition("=")
                cur[k] = v
        elif section == "RAM" and line.strip().isdigit():
            info["ram_gb"] = round(int(line.strip()) / 1024 / 1024, 1)
    for claim in info["claims"].values():
        exp = int(claim.get("expires_at") or 0)
        claim["expired"] = bool(exp) and info["now"] >= exp
    return info


# --- shape matching -----------------------------------------------------------

def binding_reason(gpu, claims, gpu_memory_gb, want_arch):
    """Why this GPU cannot serve the request, or None if it can. One predicate, so
    `capacity` and `up` cannot disagree about what is eligible."""
    claim = claims.get(gpu["index"])
    if claim and not claim["expired"]:
        return f"gpu{gpu['index']} claimed by {claim.get('run') or claim.get('tag') or 'unknown'}"
    if gpu["memory_total_gb"] + 0.5 < (gpu_memory_gb or 0):
        return (f"gpu{gpu['index']} has {gpu['memory_total_gb']}GB "
                f"< {gpu_memory_gb}GB required")
    if want_arch and gpu["arch"] and gpu["arch"] < want_arch:
        return f"gpu{gpu['index']} is sm_{gpu['arch']} < sm_{want_arch} required"
    return None


# --- claims -------------------------------------------------------------------

def gpu_loop(indices, body):
    return (f'for i in {" ".join(str(i) for i in indices)}; do\n'
            '  f="$D/gpu$i.claim"\n' + body + 'done\n')


def acquire(entry, indices, need, meta, ttl_s):
    """O_EXCL per GPU in one remote shell; releases partial holdings on shortfall."""
    script = PRELUDE + f'need={need}; got=""; exp=$((now + {ttl_s}))\n' + gpu_loop(indices, (
        '  [ "$need" -eq 0 ] && break\n'
        '  if [ -f "$f" ]; then\n'
        '    e=$(sed -n "s/^expires_at=//p" "$f" 2>/dev/null | head -1)\n'
        '    case "$e" in ""|0) printf "HELD=%s:no-expiry\\n" "$i"; continue;; esac\n'
        '    if [ "$now" -ge "$e" ]; then rm -f "$f"\n'
        '    else printf "HELD=%s:%s\\n" "$i" "$(sed -n "s/^tag=//p" "$f" | head -1)"; continue; fi\n'
        '  fi\n'
        '  if ( set -C; printf "tag=%s\\nholder=%s\\nrun=%s\\nproject=%s\\n'
        'acquired_at=%s\\nexpires_at=%s\\n" '
        f'"{meta["tag"]}" "{meta["holder"]}" "{meta["run"]}" "{meta["project"]}" '
        '"$now" "$exp" > "$f" ) 2>/dev/null; then\n'
        '    got="$got $i"; need=$((need-1))\n'
        '  else printf "HELD=%s:race\\n" "$i"; fi\n'
    )) + (
        'if [ "$need" -gt 0 ]; then for i in $got; do rm -f "$D/gpu$i.claim"; done; got=""; fi\n'
        'printf "GOT=%s\\n" "$got"\n')
    kv = parse_kv(ssh_or_die(entry, script, CLAIM_TIMEOUT))
    got = [int(x) for x in kv.get("GOT", [""])[0].split()]
    return got, [f"gpu{v.split(':', 1)[0]} held by {v.split(':', 1)[1] or 'unknown'}"
                 for v in kv.get("HELD", [])]


def renew(entry, indices, ttl_s):
    """Rewrite expires_at in place. A missing marker is reported, never recreated —
    resurrecting a released hold would hand out a GPU someone else now owns."""
    script = PRELUDE + f'exp=$((now + {ttl_s}))\n' + gpu_loop(indices, (
        '  if [ ! -f "$f" ]; then printf "MISSING=%s\\n" "$i"; continue; fi\n'
        '  sed -i "s/^expires_at=.*/expires_at=$exp/" "$f"\n'
    )) + 'printf "EXP=%s\\n" "$exp"\n'
    kv = parse_kv(ssh_or_die(entry, script, CLAIM_TIMEOUT))
    return int(kv["EXP"][0]), kv.get("MISSING", [])


def release(entry, indices):
    files = " ".join(f'"$D/gpu{i}.claim"' for i in indices)
    ssh_or_die(entry, PRELUDE + f"rm -f {files}\n", CLAIM_TIMEOUT)


def parse_kv(out):
    """One line protocol for every claim script: KEY=value, repeats collected."""
    kv = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv.setdefault(k, []).append(v)
    return kv


# --- instance ids -------------------------------------------------------------

def make_id(key, indices):
    return f"ssh:{key}:{','.join(str(i) for i in indices)}"


def parse_id(instance_id):
    parts = instance_id.split(":")
    if len(parts) != 3 or parts[0] != "ssh":
        die("permission", f"not a provider_ssh instance id: {instance_id}")
    return parts[1], [int(x) for x in parts[2].split(",") if x]


# --- verbs --------------------------------------------------------------------

def v_capacity(args):
    servers = load_servers(args.res)
    if args.server:
        servers = {args.server: pick(servers, args.server)}
    want_arch = parse_arch(args.arch_min)
    need = args.gpu_count or 1

    def one(item):
        key, entry = item
        info = remote_info(entry, want_ram=bool(args.host_ram_gb))
        if info is None:
            return {"region": key, "machine_type": key, "avail": 0, "price_hr": 0,
                    "binding_limit": "unreachable over ssh"}
        if not info["gpus"]:
            return {"region": key, "machine_type": key, "label": "cpu-only", "avail": 0,
                    "price_hr": 0, "binding_limit": "no GPU reported by nvidia-smi"}
        limits, ok = [], []
        for gpu in info["gpus"]:
            reason = binding_reason(gpu, info["claims"], args.gpu_memory_gb, want_arch)
            (limits if reason else ok).append(reason or gpu)
        if args.host_ram_gb and info["ram_gb"] and info["ram_gb"] + 1 < args.host_ram_gb:
            limits.append(f"host RAM {info['ram_gb']}GB < {args.host_ram_gb}GB required")
            ok = []
        first = info["gpus"][0]
        return {
            # machine_type is what `up --machine-type` accepts, so a capacity row round-trips; label is display
            "region": key, "machine_type": key, "label": first["name"],
            "avail": len(ok) // need if need else 0, "price_hr": 0,
            "binding_limit": "; ".join(limits) or None,
            "free_gpus": [g["index"] for g in ok],
            "arch": f"sm_{first['arch']}" if first["arch"] else None,
        }

    rows = fan_out(list(servers.items()), one)
    emit(sorted(rows, key=lambda r: -r["avail"]))


def v_up(args):
    entry = pick(load_servers(args.res), args.machine_type)
    need = args.gpu_count or 1
    want_arch = parse_arch(args.arch_min)
    info = remote_info(entry)
    if info is None:
        die("transient", f"{args.machine_type} is unreachable over ssh")
    if not info["gpus"]:
        die("no_capacity", f"{args.machine_type} reports no GPU", binding_limit="no GPU")
    cand = [g["index"] for g in info["gpus"]
            if binding_reason(g, {}, args.gpu_memory_gb, want_arch) is None]
    if len(cand) < need:
        die("no_capacity", f"{args.machine_type} has {len(cand)} GPU(s) meeting the shape, need {need}",
            binding_limit="shape requirements exclude the rest")
    meta = {"tag": args.tag or "", "holder": os.uname().nodename,
            "run": args.run or "", "project": args.project or ""}
    got, held = acquire(entry, cand, need, meta, args.ttl_s)
    if len(got) < need:
        die("no_capacity", f"could not claim {need} GPU(s) on {args.machine_type}",
            binding_limit="; ".join(held) or "all GPUs claimed")
    emit([make_id(args.machine_type, got)])


def v_addr(args):
    key, _ = parse_id(args.instance_id)
    target, port = endpoint(pick(load_servers(args.res), key))
    emit(f"ssh://{target}" + (f":{port}" if port and port != 22 else ""))


def v_state(args):
    key, indices = parse_id(args.instance_id)
    info = remote_info(pick(load_servers(args.res), key))
    if info is None:
        emit("failed")  # cannot be reached, so it has failed as a compute target
        return
    live = [i for i in indices if i in info["claims"] and not info["claims"][i]["expired"]]
    emit("running" if indices and len(live) == len(indices) else "gone")


def v_down(args):
    key, indices = parse_id(args.instance_id)
    release(pick(load_servers(args.res), key), indices)  # rm -f: already-gone is success
    emit({"ok": True, "released": indices, "server": key})


def v_renew(args):
    key, indices = parse_id(args.instance_id)
    exp, missing = renew(pick(load_servers(args.res), key), indices, args.ttl_s)
    if missing:
        die("no_capacity", f"claim already gone for gpu {','.join(missing)} on {key} — "
            "not recreating it; acquire a fresh lease", missing=missing)
    emit({"expires_at": exp, "instance_id": args.instance_id})


def v_sweep(args):
    """Rows carry `tag` so L2 reconciles on its own owner token rather than on an
    instance id this adapter owns the format of. `provider` is L2's to inject.

    A host that does not answer is reported as **unreached**, not as zero claims. It
    used to return `[]` for that case, which is the failure the scope envelope exists
    to stop: a powered-off box holds no claims and an unreachable one holds unknown
    claims, and both used to render as "no orphans here". Contract: "Scope completeness".
    """
    servers = load_servers(args.res)

    def one(item):
        key, entry = item
        info = remote_info(entry)
        if info is None:
            return key, None
        rows = []
        for idx, claim in sorted(info["claims"].items()):
            tag = claim.get("tag") or ""
            if args.tag_prefix and not tag.startswith(args.tag_prefix):
                continue
            rows.append({
                "instance_id": make_id(key, [idx]), "tag": tag,
                "age_s": max(0, info["now"] - int(claim.get("acquired_at") or info["now"])),
                "price_hr": 0, "expired": claim["expired"],
                "holder": claim.get("holder"), "run": claim.get("run") or None,
                "project": claim.get("project") or None,
            })
        return key, rows

    units, checked, unreached = [], [], []
    for key, rows in fan_out(list(servers.items()), one):
        if rows is None:
            unreached.append({"scope": key, "why": "unreachable over ssh"})
        else:
            checked.append(key)
            units += rows
    # `storage=[]`, explicitly, and it is not the same statement as omitting the key.
    # Owned hardware has no second meter -- the disk was bought, so releasing a claim
    # leaves nothing accruing. Saying so is what stops L2 marking this adapter's scope
    # incomplete, which is the correct treatment for an adapter that simply never looked.
    emit(sweep_result(units, checked, unreached, storage=[]))


def v_history(args):
    """`supported: false`, and that is the whole honest answer for a static box.

    The claim marker is the only lifecycle record here and `down` deletes it, so a
    released GPU leaves nothing behind to read. Saying so is the point: the caller
    then knows "was it released" is unanswerable on this provider, rather than
    inferring release from a sweep that shows no claim — which is the same absence
    the contract's `history` row exists to stop being read as evidence.
    """
    emit({"events": [], "supported": False,
          "why": "a claim marker is removed on release; nothing records the past",
          "scope": {"complete": True, "checked": sorted(load_servers(args.res)),
                    "unreached": []}})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--resources")
    sub = ap.add_subparsers(dest="verb", required=True)

    c = sub.add_parser("capacity"); c.set_defaults(fn=v_capacity); add_shape_args(c)
    c.add_argument("--server", help="probe only this server key")

    u = sub.add_parser("up"); u.set_defaults(fn=v_up); add_shape_args(u)
    u.add_argument("--machine-type", required=True, help="server key from resources.json -> servers")
    u.add_argument("--ttl-s", type=int, required=True, help="claim expiry; L2 owns the default")
    u.add_argument("--tag", help="owner token from L2; stored verbatim, swept by prefix")
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

    h = sub.add_parser("history"); h.set_defaults(fn=v_history)
    h.add_argument("--tag-prefix", default=TAG_PREFIX)
    h.add_argument("--instance-id")
    h.add_argument("--window-s", type=int, default=5 * 86400)

    args = ap.parse_args()
    args.res = resources_from_workspace_root(args.resources)
    args.fn(args)


if __name__ == "__main__":
    main()
