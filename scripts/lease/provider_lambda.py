#!/usr/bin/env python3
"""provider_lambda — Lambda Cloud adapter for /lease.

Contract: `skills/lease/references/contract.md`. Shape table: `machines_lambda.json`.
Fleet-level rules this serves: `references/fleet.md`.

  machine_type   `<region>:<instance_type>`, e.g. `us-west-1:gpu_1x_a10`.
                 Everything `up` needs is in the string, because L2 passes nothing else.
  tag            carried in the instance **name** — this API has no label field at all.
  dead-man       a systemd timer on the box that calls the terminate endpoint. There is
                 no other option here; see "The three places this provider is not Nebius".

THE THREE PLACES THIS PROVIDER IS NOT NEBIUS, and each one changes what an honest
answer looks like rather than just how a call is spelled:

1. **Price is read live, so it is a measurement, not a claim.** `instance-types` returns
   `price_cents_per_hour`. The table therefore carries only `arch` and a GPU name — what
   no API returns — which is the contract's "Put in the table only what the API will not
   tell you" arriving at the opposite layout from `machines_nebius.json` for the same
   reason. A hand-copied price here would be a second author for a value that has one.

2. **There is no lifecycle log**, so `history` is `supported: false` and "did I release
   that box?" is **unanswerable on this provider**. Not "no", not "probably" — the
   record does not exist. `sweep`'s silence must never be read as the answer, which is
   the whole reason the contract made `history` a verb instead of an inference.

3. **`shutdown -h` is not a dead-man switch here, it is the opposite of one.** Lambda
   instances have no stopped state: an instance exists or it is terminated, and it bills
   for existing. Halting the guest OS therefore stops nothing, and it removes the ssh
   access that was the only remaining way to release the box. So the guest-side switch
   the contract offers as the fallback is worse than absent on this provider, and the
   only real switch is one that calls the terminate API from the box. That needs an API
   key on a rented machine, which is a decision the user makes, not this adapter --
   `up` REFUSES rather than renting something that can never expire. Money rule 3.

‼️ **ONE CONSUMER LIVES OUTSIDE THIS REPO.** The global `lambda_server` skill
(`skill-hub/skills/lambda_server/lambda.sh`) is a shell over this file — it renders these
verbs and keeps the flags it has always had. Renaming a verb or changing an emitted key
therefore breaks a script this repo's checks do not see, on three machines, and it breaks
it the quiet way: the shell's `up` has no launch path of its own to fall back to, so the
failure is "cannot rent" rather than a traceback. Same arrangement as
`nebius_scan.py` / `nebius_audit.py`, and the same reason: one implementation means the
next trap found on either side is fixed for both.

  provider_lambda.py capacity [--gpu-count N] [--gpu-memory-gb G] [--arch-min sm_80]
  provider_lambda.py up --machine-type T --ttl-s N [--tag ...] [--run ...] [--project ...]
  provider_lambda.py addr|state|down <instance_id>
  provider_lambda.py renew <instance_id> --ttl-s N
  provider_lambda.py sweep [--tag-prefix mlclaw-]
  provider_lambda.py history [--tag-prefix P] [--instance-id ID] [--window-s N]
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (TAG_PREFIX, add_shape_args, die, emit, load_resources,  # noqa: E402
                     parse_arch, resources_from_workspace_root, sweep_result)

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "machines_lambda.json")

DEFAULT_API = "https://cloud.lambda.ai/api/v1"
CALL_TIMEOUT = 60
UP_POLL_TIMEOUT = 900   # a create not reachable by then is not slow; see `await_reachable`
UP_POLL_EVERY = 15
SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes"]

MT_RE = re.compile(r"^(?P<region>[a-z0-9-]+):(?P<itype>[A-Za-z0-9_.-]+)$")

# Provider state -> the contract's five.
#
# `stopped` is absent from this table and its absence is the fact worth recording: the
# Lambda API has no such state. That removes the trap the contract warns about on AWS and
# Nebius (compute halted, disk still billing, reported as gone) and replaces it with a
# blunter one -- there is no way to pause the meter at all, so a box you are done with is
# either terminated or costing full rate.
STATE_MAP = {
    "booting": "pending",
    "active": "running",
    "unhealthy": "failed",      # created, never became usable. Never `running`.
    "terminating": "stopping",
    "terminated": "gone",
}


def table():
    with open(TABLE, encoding="utf-8") as fh:
        return json.load(fh)


def conf(res_path):
    """`resources.json -> compute.lambda`, plus the credential it names.

    Nothing here is an infrastructure id and nothing is pinned in code -- regions and
    instance types are discovered from the API on every call, for the same reason
    `provider_nebius` discovers tenants: an id written down drifts, and a drifted one is
    a wrong answer that reads like a configured one.
    """
    res = load_resources(res_path)
    cfg = dict(((res.get("compute") or {}).get("lambda")) or {})
    if not cfg:
        die("permission", "no `compute.lambda` block in resources.json",
            hint="run /resources to register the provider before leasing from it")
    cfg["api_base"] = (cfg.get("api_base") or os.environ.get("LAMBDA_API_BASE")
                       or DEFAULT_API).rstrip("/")
    cfg["api_key"] = _read_key(cfg.get("api_key_path"), cfg.get("api_key_env"),
                               "LAMBDA_API_KEY", "the Lambda API key")
    return cfg


def _read_key(path, env_name, fallback_env, what):
    for name in (env_name, fallback_env):
        if name and os.environ.get(name):
            return os.environ[name].strip()
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            die("permission", f"cannot read {what} at {path}: {exc}")
    return None


def classify(status, body):
    """HTTP status + body -> a normalized error class.

    `no_capacity` and `quota` are the pair that matters, because their remedies are
    opposite and a support ticket buys nothing against an empty rack. Lambda names the
    first one in the body (`insufficient-capacity`); anything else that reads like a
    limit falls to `quota`, per the contract's tie-break rule.
    """
    low = (body or "").lower()
    if status in (401, 403):
        return "credential_expired" if "expired" in low else "permission"
    if "insufficient-capacity" in low or "capacity" in low:
        return "no_capacity"
    if "quota" in low or "limit" in low or status == 429:
        return "quota"
    if status and status >= 500:
        return "transient"
    return "transient"


def api(cfg, path, method="GET", body=None, timeout=CALL_TIMEOUT):
    """(payload, error_string). The Lambda API authenticates with the key as the Basic
    username and an empty password."""
    url = f"{cfg['api_base']}{path}"
    if not cfg.get("api_key"):
        die("permission", "no Lambda API key",
            hint="set compute.lambda.api_key_path in resources.json, or $LAMBDA_API_KEY")
    token = base64.b64encode(f"{cfg['api_key']}:".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}"), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        return None, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def api_or_die(cfg, path, **kw):
    data, err = api(cfg, path, **kw)
    if err:
        status = int(re.match(r"HTTP (\d+)", err).group(1)) if err.startswith("HTTP ") else 0
        die(classify(status, err), f"{path}: {err}")
    return data


def instances(cfg):
    """Every instance on the account.

    One flat list, no scope tree: a Lambda API key reaches one account and the endpoint
    is not paginated or region-scoped. That is why this adapter's `sweep` can report
    `complete: true` where `provider_nebius` has to walk tenants and projects -- and
    it is a property of the API, not a shortcut. If Lambda ever paginates this, the
    scope envelope is where that has to show up.
    """
    return (api_or_die(cfg, "/instances").get("data") or [])


# --- capacity -----------------------------------------------------------------

def _specs(entry):
    it = entry.get("instance_type") or entry
    return it, (it.get("specs") or {})


def v_capacity(args):
    cfg = conf(args.res)
    tab = table()["instance_types"]
    want_gpus = args.gpu_count or 1
    want_mem = args.gpu_memory_gb or 0
    want_arch = parse_arch(args.arch_min)
    want_ram = args.host_ram_gb or 0
    allow = cfg.get("allow_regions")

    data = api_or_die(cfg, "/instance-types").get("data") or {}
    rows, unknown = [], []
    for name, entry in sorted(data.items()):
        it, specs = _specs(entry)
        known = tab.get(name)
        if not known:
            # A type this table has never seen. Named, not silently dropped: `arch` is a
            # hard admission check and an unlisted type cannot pass one, but a caller
            # deserves to know a machine exists that this file has not been taught.
            unknown.append(name)
            continue
        gpus = specs.get("gpus") or known.get("gpu_count") or 0
        gpu_mem = known.get("gpu_memory_gb") or 0
        arch = parse_arch(known.get("arch"))
        ram = specs.get("memory_gib") or 0
        if gpus < want_gpus or (want_mem and gpu_mem < want_mem):
            continue
        if want_arch is not None and (arch is None or arch < want_arch):
            continue
        if want_ram and ram < want_ram:
            continue
        cents = it.get("price_cents_per_hour")
        for region in (entry.get("regions_with_capacity_available") or []):
            rname = region.get("name")
            if allow and rname not in allow:
                continue
            rows.append({
                "region": rname,
                "machine_type": f"{rname}:{name}",
                # Lambda publishes WHICH regions have capacity, never HOW MUCH. `1` is a
                # true lower bound and is flagged as one; reporting a made-up count here
                # would let a fleet be sized off a number nobody measured, which
                # fleet.md "Placement" names as the way a sweep plans slots it cannot get.
                "avail": 1, "avail_is_lower_bound": True,
                "binding_limit": "stock (per-region count not published by this API)",
                # Read live from the API on this call, so unlike the Nebius table this is
                # a measurement. The status field travels with it either way.
                "price_hr": round(cents / 100, 2) if cents is not None else None,
                "price_status": "verified" if cents is not None else "claim",
                "gpu": known.get("gpu"), "gpu_count": gpus,
                "gpu_memory_gb": gpu_mem or None, "arch": known.get("arch"),
                "arch_status": known.get("arch_status"),
                "host_ram_gb": ram or None, "vcpu": specs.get("vcpus"),
                # One pool. Lambda sells no interruptible tier, so there is no second row
                # to look for -- stated rather than left as an absence a caller has to
                # interpret. Contract, "Interruptible capacity".
                "pool": "on_demand",
                "label": f"{known.get('gpu')} ×{gpus} · {rname} · "
                         f"{it.get('description') or name}",
            })
    # Types with no stock report no row at all from this API, which would render as "this
    # machine does not exist". Emit an explicit zero row so the caller can tell the two apart.
    for name, entry in sorted(data.items()):
        it, specs = _specs(entry)
        if tab.get(name) and not (entry.get("regions_with_capacity_available") or []):
            cents = it.get("price_cents_per_hour")
            rows.append({"region": None, "machine_type": None, "avail": 0,
                         "binding_limit": "stock — no region has capacity right now",
                         "price_hr": round(cents / 100, 2) if cents is not None else None,
                         "price_status": "verified" if cents is not None else "claim",
                         "gpu": tab[name].get("gpu"), "arch": tab[name].get("arch"),
                         "pool": "on_demand",
                         "label": f"{name} — out of stock everywhere"})

    # A type the provider sells and this table has never heard of. It rides as an
    # `avail: 0` row with a null `machine_type`, the same shape `provider_nebius` uses for
    # a scope it could not read, and for the same reason: something the caller cannot use
    # but must be told about. Dropping it silently would read as "Lambda does not sell
    # that card", which is how the L40S conclusion was reached wrongly on the other
    # provider. `capacity` returns a BARE LIST per the contract, so there is nowhere else
    # for this to go.
    for name in unknown:
        rows.append({"region": None, "machine_type": None, "avail": 0,
                     "price_hr": None, "price_status": "claim", "pool": "on_demand",
                     "binding_limit": "NOT IN machines_lambda.json — no `arch`, so it "
                                      "cannot pass the compatibility check",
                     "label": f"{name} — this table is behind the provider; add the row"})
    emit(rows)


# --- up ---------------------------------------------------------------------

def parse_mt(machine_type):
    m = MT_RE.match(machine_type or "")
    if not m:
        die("permission", f"unparseable machine_type {machine_type!r}",
            hint="expected <region>:<instance_type>, exactly as a `capacity` row reports it")
    return m.group("region"), m.group("itype")


def name_safe(value):
    """The tag rides in the instance NAME on this provider, so it has to survive whatever
    the name field accepts. Truncation is the risk worth naming: a clipped tag no longer
    matches the prefix `sweep` filters on, and the box becomes invisible to `reap` while
    still billing."""
    out = re.sub(r"[^A-Za-z0-9_-]", "-", str(value or ""))[:60]
    return out


def dead_man_key(cfg):
    """The credential the BOX uses to terminate itself, and the reason `up` can refuse.

    Deliberately a separate config key from `api_key_path` rather than reusing it. The
    two have different blast radii: one sits on this laptop, the other sits on a rented
    machine that strangers get next. Making the user name it separately is what turns
    "we put your account key on a box" into a decision instead of a side effect.
    """
    return _read_key(cfg.get("dead_man_key_path"), cfg.get("dead_man_key_env"),
                     None, "the dead-man API key")


DEADMAN_INSTALL = r"""set -eu
sudo install -d -m 700 /root/.mlclaw
printf '%s' "$MLCLAW_KEY" | sudo tee /root/.mlclaw/lambda.key >/dev/null
printf '%s' "$MLCLAW_ID"  | sudo tee /root/.mlclaw/instance_id >/dev/null
printf '%s' "$MLCLAW_API" | sudo tee /root/.mlclaw/api_base >/dev/null
sudo chmod 600 /root/.mlclaw/lambda.key /root/.mlclaw/instance_id /root/.mlclaw/api_base
sudo tee /usr/local/bin/mlclaw-deadman >/dev/null <<'SCRIPT'
#!/bin/sh
# MLClaw dead-man switch. Terminates THIS instance through the Lambda API.
# `shutdown -h` would not do it: a halted Lambda instance still exists and still bills.
K=$(cat /root/.mlclaw/lambda.key); I=$(cat /root/.mlclaw/instance_id)
A=$(cat /root/.mlclaw/api_base)
exec curl -fsS -u "$K:" -X POST "$A/instance-operations/terminate" \
  -H 'Content-Type: application/json' -d "{\"instance_ids\":[\"$I\"]}"
SCRIPT
sudo chmod 700 /usr/local/bin/mlclaw-deadman
"""

DEADMAN_ARM = r"""set -eu
sudo systemctl stop mlclaw-deadman.timer 2>/dev/null || true
sudo systemd-run --on-active="$MLCLAW_TTL"s --unit=mlclaw-deadman --collect \
  /usr/local/bin/mlclaw-deadman >/dev/null
printf 'ARMED=%s\n' "$(systemctl is-active mlclaw-deadman.timer)"
"""


def ssh_run(cfg, ip, script, env, timeout=90):
    """(rc, stdout, stderr). Values reach the box as `export` lines PREPENDED TO THE
    SCRIPT ON STDIN, never as command-line arguments — an argument lands in the remote
    process table, where any other user on the box reads the API key with `ps`.

    Not `SendEnv` either: sshd forwards only what its `AcceptEnv` allows and the default
    allows almost nothing, so a switch relying on it would arm with an empty key and
    report success."""
    user = cfg.get("ssh_user") or "ubuntu"
    argv = ["ssh", *SSH_OPTS]
    if cfg.get("ssh_identity"):
        argv += ["-i", os.path.expanduser(cfg["ssh_identity"])]
    prelude = "".join(f"export {k}={_sh_quote(v)}\n" for k, v in env.items())
    argv += [f"{user}@{ip}", "bash -s"]
    proc = subprocess.run(argv, input=prelude + script, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _sh_quote(value):
    """Single-quote for the remote shell. The key goes through here and nowhere else:
    an API key spliced unquoted into a command line lands in the remote process table,
    where every other user on the box can read it with `ps`."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def require_dead_man(cfg):
    key = dead_man_key(cfg)
    if not key:
        die("permission",
            "no dead-man credential configured, so this box could never expire on its own",
            hint="Lambda has no provider-side TTL and a guest `shutdown -h` does not stop "
                 "its meter (the instance keeps existing and keeps billing), so the only "
                 "switch is one that calls the terminate API from the box. Set "
                 "compute.lambda.dead_man_key_path in resources.json — use a key you are "
                 "willing to place on rented hardware. Contract, Money rule 3.")
    return key


def arm_dead_man(cfg, ip, instance_id, ttl_s, install=True):
    key = require_dead_man(cfg)
    env = {"MLCLAW_KEY": key, "MLCLAW_ID": instance_id,
           "MLCLAW_API": cfg["api_base"], "MLCLAW_TTL": str(int(ttl_s))}
    if install:
        rc, _, err = ssh_run(cfg, ip, DEADMAN_INSTALL, env)
        if rc != 0:
            return False, f"could not install the dead-man switch: {err.strip()[:200]}"
    rc, out, err = ssh_run(cfg, ip, DEADMAN_ARM, env)
    # `ARMED=active` exactly. A substring test would pass on `inactive`, which is the
    # literal word systemd prints for a timer that did not start — a dead-man switch
    # reporting itself armed when it is not is worse than none, because nobody re-checks
    # a safety they believe in.
    if rc != 0 or "ARMED=active" not in out:
        return False, f"could not arm the dead-man switch: {(err or out).strip()[:200]}"
    return True, None


def v_up(args):
    cfg = conf(args.res)
    region, itype = parse_mt(args.machine_type)

    # Refuse BEFORE the create call, not after. A box that exists with no switch is
    # already the failure; discovering the missing credential afterwards means paying
    # for it. Money rule 3 read in the only order that helps.
    require_dead_man(cfg)

    name = name_safe(args.tag or "")
    body = {"region_name": region, "instance_type_name": itype, "quantity": 1,
            "name": name,
            "ssh_key_names": cfg.get("ssh_key_names") or []}
    if not body["ssh_key_names"]:
        die("permission", "no compute.lambda.ssh_key_names in resources.json",
            hint="Lambda injects authorized_keys only at create; a box launched without "
                 "one can never be reached, released by hand, or read")
    if cfg.get("file_system_names"):
        body["file_system_names"] = cfg["file_system_names"]

    data, err = api(cfg, "/instance-operations/launch", method="POST", body=body)
    if err:
        status = int(re.match(r"HTTP (\d+)", err).group(1)) if err.startswith("HTTP ") else 0
        die(classify(status, err), f"launch failed: {err}")
    ids = ((data or {}).get("data") or {}).get("instance_ids") or []
    if not ids:
        # The response was accepted and named nothing. Per fleet.md, this means GO AND
        # LOOK, never retry — the instance may exist and bill while nothing local knows it.
        die("transient", "launch returned no instance id — the box may exist and be "
                         "billing; run `sweep` before retrying, never retry blind")
    await_reachable(cfg, ids[0], args)
    emit(ids)


def await_reachable(cfg, instance_id, args):
    """`up` is not complete until the box answers. Contract, "Created is not usable".

    The failure this exists for: the create succeeds, the instance appears in the list
    with a plausible status, and it never becomes reachable. Everything reads as a
    working box until ssh times out several minutes later — by which time a search may
    have "acquired" a dozen of them.
    """
    deadline = time.time() + UP_POLL_TIMEOUT
    while time.time() < deadline:
        inst = get_instance(cfg, instance_id)
        if inst is None:
            die("no_capacity", f"{instance_id} disappeared before it became reachable")
        status = (inst.get("status") or "").lower()
        mapped = STATE_MAP.get(status, "pending")
        if mapped == "failed":
            teardown(cfg, instance_id)   # the remains still hold a machine
            die("no_capacity", f"{instance_id} became {status} without becoming usable; "
                               "torn down")
        ip = inst.get("ip")
        if mapped == "running" and ip and _ssh_ready(cfg, ip):
            ok, why = arm_dead_man(cfg, ip, instance_id, args.ttl_s)
            if not ok:
                # A box that is up with no switch is exactly what Money rule 3 forbids.
                # Tearing it down loses nothing (nothing has run on it yet) and is the
                # only outcome that cannot leave a machine billing forever.
                teardown(cfg, instance_id)
                die("transient", f"{why} — instance terminated rather than left without "
                                 "an expiry")
            return
        time.sleep(UP_POLL_EVERY)
    die("transient", f"{instance_id} was still not reachable after {UP_POLL_TIMEOUT}s — "
                     "the lease row stays open on purpose so it remains visible")


def _ssh_ready(cfg, ip):
    user = cfg.get("ssh_user") or "ubuntu"
    argv = ["ssh", *SSH_OPTS]
    if cfg.get("ssh_identity"):
        argv += ["-i", os.path.expanduser(cfg["ssh_identity"])]
    try:
        return subprocess.run(argv + [f"{user}@{ip}", "true"], capture_output=True,
                              timeout=20).returncode == 0
    except subprocess.TimeoutExpired:
        return False


# --- addr / state / down / renew ----------------------------------------------

def get_instance(cfg, instance_id):
    for inst in instances(cfg):
        if inst.get("id") == instance_id:
            return inst
    return None


def v_addr(args):
    cfg = conf(args.res)
    inst = get_instance(cfg, args.instance_id)
    if inst is None:
        die("permission", f"{args.instance_id} no longer exists")
    ip = inst.get("ip")
    if not ip:
        die("transient", f"{args.instance_id} has no address yet "
                         f"(status {inst.get('status')})")
    emit(f"ssh://{cfg.get('ssh_user') or 'ubuntu'}@{ip}:22")


def v_state(args):
    cfg = conf(args.res)
    inst = get_instance(cfg, args.instance_id)
    if inst is None:
        emit("gone")
        return
    emit(STATE_MAP.get((inst.get("status") or "").lower(), "pending"))


def teardown(cfg, instance_id):
    api(cfg, "/instance-operations/terminate", method="POST",
        body={"instance_ids": [instance_id]}, timeout=120)


def v_down(args):
    """Money rule 2: not done until nothing bills.

    Terminating an instance releases its local storage with it — Lambda's per-instance
    disk is not a separate resource. What survives is a **filesystem**, which is created
    and deleted independently and goes on billing with no instance anywhere. `up` never
    creates one, so the check below is normally empty; it runs anyway, because rule 2 is
    a verification and an assumption is what it exists to replace.
    """
    cfg = conf(args.res)
    inst = get_instance(cfg, args.instance_id)
    tag = (inst or {}).get("name") or None
    if inst is not None:
        _, err = api(cfg, "/instance-operations/terminate", method="POST",
                     body={"instance_ids": [args.instance_id]}, timeout=120)
        if err and "not found" not in err.lower():
            status = (int(re.match(r"HTTP (\d+)", err).group(1))
                      if err.startswith("HTTP ") else 0)
            die(classify(status, err), f"terminate failed for {args.instance_id}: {err}")

    residual = [{"filesystem": fs.get("id"), "name": fs.get("name"),
                 "why": "filesystem carrying this lease's tag survives the instance"}
                for fs in (api_or_die(cfg, "/file-systems").get("data") or [])
                if tag and fs.get("name") == tag]
    if residual:
        die("transient", "instance terminated but storage survives — STILL BILLING",
            residual=residual)
    emit({"ok": True, "instance_id": args.instance_id, "residual_billing": False})


def v_renew(args):
    """Re-arm the switch on the box. Must fail loudly when the hold is already gone — a
    renew that quietly succeeds against a dead box is how a fleet believes it holds
    machines it does not."""
    cfg = conf(args.res)
    inst = get_instance(cfg, args.instance_id)
    if inst is None:
        die("permission", f"{args.instance_id} no longer exists — acquire a fresh lease")
    ip = inst.get("ip")
    if not ip:
        die("transient", f"{args.instance_id} is unreachable; cannot re-arm its expiry")
    ok, why = arm_dead_man(cfg, ip, args.instance_id, args.ttl_s, install=False)
    if not ok:
        die("transient", why)
    emit({"expires_at": int(time.time()) + args.ttl_s, "instance_id": args.instance_id})


# --- sweep / history ----------------------------------------------------------

def v_sweep(args):
    """Both meters. Instances bill while they exist; filesystems bill while they exist
    and outlive every instance, which is the one the account forgets."""
    cfg = conf(args.res)
    tab = table()["instance_types"]
    now = int(time.time())

    units = []
    for inst in instances(cfg):
        # No label field on this API, so the tag lives in the name. It is a weaker
        # channel than a label and the weakness is worth stating: a name is editable in
        # the console, so a renamed box drops out of every tag-filtered sweep while it
        # goes on billing. `history` cannot rescue that here either (see below), which
        # is why `leases.json` carries more weight on this provider than on Nebius.
        tag = inst.get("name") or ""
        if args.tag_prefix and not tag.startswith(args.tag_prefix):
            continue
        itype = ((inst.get("instance_type") or {}).get("name")) or ""
        cents = (inst.get("instance_type") or {}).get("price_cents_per_hour")
        units.append({
            "instance_id": inst.get("id"), "tag": tag,
            # This API returns no creation timestamp, so age is genuinely unknown rather
            # than zero. `null` keeps "started a minute ago" and "has run for nine days"
            # from rendering identically.
            "age_s": None,
            "price_hr": round(cents / 100, 2) if cents is not None else None,
            "price_status": "verified" if cents is not None else "claim",
            # Expiry lives on the box, not in the API, so this adapter cannot read it
            # back. `false` here would assert a hold is fresh; null says nobody asked.
            "expired": None,
            "state": (inst.get("status") or "").upper(),
            "name": inst.get("name"), "gpu": (tab.get(itype) or {}).get("gpu"),
            "machine_type": f"{(inst.get('region') or {}).get('name')}:{itype}",
            "region": (inst.get("region") or {}).get("name"),
        })

    storage = []
    for fs in (api_or_die(cfg, "/file-systems").get("data") or []):
        name = fs.get("name") or ""
        if args.tag_prefix and not name.startswith(args.tag_prefix):
            continue
        used = fs.get("bytes_used")
        storage.append({
            "storage_id": fs.get("id"), "kind": "filesystem", "tag": name,
            "name": name,
            "size_gib": round(used / (1024 ** 3)) if used else None,
            # Lambda bills filesystems per GB-month and publishes no per-hour rate
            # through this API. null, never 0 — see machines_lambda.json -> storage.
            "price_hr": None,
            "attached_to": "in use" if fs.get("is_in_use") else None,
            "age_s": None, "region": (fs.get("region") or {}).get("name"),
        })

    # `complete: true` with no `unreached` is a real claim here, not a shortcut: one API
    # key reaches one account, and neither endpoint is paginated or region-scoped. There
    # is no scope tree to walk and therefore no corner to miss.
    emit(sweep_result(units, checked=["account"], unreached=[], storage=storage))


def v_history(args):
    """`supported: false`, and it is the honest whole answer.

    Lambda exposes no audit or lifecycle log. So on this provider "did that box get
    released?" cannot be answered from the provider at all — and the failure the contract
    guards against is answering it anyway from `sweep`'s silence, which is equally
    consistent with a terminated box and a renamed one that is still billing.

    What is left is `leases.json`, which L2 merges alongside these events and which is a
    record of what MLClaw did rather than of what happened. That gap is the reason
    `reap` matters more here than on a provider with a log.
    """
    emit({"events": [], "supported": False,
          "why": "the Lambda API exposes no audit or lifecycle log; a terminated "
                 "instance leaves no readable record on the provider side",
          "scope": {"complete": True, "checked": ["account"], "unreached": []}})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--resources")
    sub = ap.add_subparsers(dest="verb", required=True)

    c = sub.add_parser("capacity"); c.set_defaults(fn=v_capacity); add_shape_args(c)

    u = sub.add_parser("up"); u.set_defaults(fn=v_up); add_shape_args(u)
    u.add_argument("--machine-type", required=True)
    u.add_argument("--ttl-s", type=int, required=True)
    u.add_argument("--tag", required=True)
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
