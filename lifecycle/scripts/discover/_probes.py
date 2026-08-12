#!/usr/bin/env python3
"""probe -- go look. Four transports plus five vendor REST APIs.

Split out of `discover.py` at that file's own section banner, and the numbers say
why it is a seam and not a line-count cut: **21 definitions, 1050 lines, and the
whole thing sits behind nine names** -- `probe_local` / `probe_server` /
`probe_s3` / `probe_tracking` for the four transports, `TRACKING` /
`REST_LISTINGS` / `credential_present` / `aws_env` for what `sources` needs to
answer "could this be swept at all", and `human` for rendering a byte count.
It reaches *up* for nothing at all.

Everything else here is private: the five per-vendor REST probes, the HTTP
plumbing they share, the wandb listing tables, the sh-over-ssh script builder.
None of it is referenced outside this file.

Every probe answers with one of the four statuses (`verified` / `gone` /
`unreachable` / `claim`) and never collapses two of them -- a machine that did
not answer and a path that is not there are different facts, and only the second
one means the data is gone.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import read_json  # noqa: E402

# The sentinel a remote sh script echoes so the caller can tell "the command ran
# and said MISSING" from "the connection died mid-stream". Lives here because the
# probe section is the only thing that speaks that protocol.
OK = "__MLCLAW_DISCOVER_OK__"

# probe — go look
# --------------------------------------------------------------------------- #

def human(n):
    if n is None:
        return None
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def measure_local(path, budget_s):
    """-> (bytes, files, why_not). Bounded, and honest when the bound is hit."""
    deadline = time.time() + budget_s
    total = files = 0
    for root, _dirs, names in os.walk(path, onerror=lambda e: None):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
            files += 1
        if time.time() > deadline:
            # Both values discarded on purpose. Returning the partial count with
            # a caveat invites it to be quoted without one.
            return None, None, (f"the walk exceeded the {budget_s:g}s budget — "
                                f"size and file count are NOT MEASURED, not zero")
    return total, files, None


def probe_local(path, budget_s):
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return "gone", "no such path on this machine", None, {}
    if os.path.isfile(p):
        sz = os.path.getsize(p)
        return "verified", f"file, {human(sz)}", None, {"bytes": sz, "files": 1}
    try:
        names = sorted(os.listdir(p))
    except OSError as exc:
        # Permission denied is "could not look", not "not there". This is the
        # single most common way a handover sweep produces a false `gone`.
        return "unreachable", f"{type(exc).__name__}: {exc}", None, {}
    b, f, why = measure_local(p, budget_s)
    size = {"bytes": b, "files": f}
    if why:
        size["not_measured"] = why
    return ("verified", f"directory, {len(names)} entries"
            + (f", {human(b)} in {f} files" if b is not None else f"; {why}"),
            names[:20], size)


SIZE, FILES = "__MLCLAW_SIZE__", "__MLCLAW_FILES__"


def probe_server(key, path, resources, budget_s):
    srv = ((resources or {}).get("servers") or {}).get(key)
    if not srv:
        return ("unreachable", f"no server {key!r} in resources.json", None,
                {"blocker": f"server:{key}:not_registered"})
    host = srv.get("alias") or srv.get("host")
    if not host:
        return ("unreachable", f"server {key!r} has no host or alias", None,
                {"blocker": f"server:{key}:no_host"})
    q = shlex.quote(path)
    # `du -sk` rather than `-sb`: `-b` is GNU-only and a capture box may be a
    # BSD. KB granularity is irrelevant next to the question being asked.
    script = (
        f'if [ -e {q} ]; then\n'
        f'  ls -1A {q} 2>/dev/null | head -n 20\n'
        f'  echo {SIZE}\n'
        f'  du -sk {q} 2>/dev/null | awk \'{{print $1}}\'\n'
        f'  echo {FILES}\n'
        f'  find {q} -type f 2>/dev/null | wc -l\n'
        f'  echo {OK}\n'
        f'else echo MISSING; echo {OK}; fi\n')
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "sh", "-s"]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True, encoding="utf-8",
                           timeout=max(60.0, budget_s * 2))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "unreachable", f"{type(exc).__name__}: {exc}", None, {}
    # Both checks: a non-zero exit catches ssh refusing, the sentinel catches a
    # shell that died mid-command with a zero exit. Either alone lets a
    # truncated answer pass for a complete one.
    if p.returncode != 0 or OK not in p.stdout:
        err = (p.stderr or "").strip().splitlines()
        return ("unreachable",
                f"exit {p.returncode}: {err[-1] if err else 'no sentinel'}", None, {})
    stdout_lines = p.stdout.splitlines()
    if "MISSING" in stdout_lines:
        return "gone", f"{host} answered; that path is not there", None, {}

    def section(a, b):
        try:
            i = stdout_lines.index(a)
        except ValueError:
            return []
        j = stdout_lines.index(b) if b in stdout_lines else len(stdout_lines)
        return [x.strip() for x in stdout_lines[i + 1:j] if x.strip()]

    names = [x for x in stdout_lines[:stdout_lines.index(SIZE)] if x.strip()] if SIZE in stdout_lines else []
    kb, nf = section(SIZE, FILES), section(FILES, OK)
    size = {}
    try:
        size["bytes"] = int(kb[0]) * 1024
    except (IndexError, ValueError):
        size["bytes"] = None
        size["not_measured"] = "du produced no total on that host"
    try:
        size["files"] = int(nf[0])
    except (IndexError, ValueError):
        size["files"] = None
    detail = f"{len(names)} entries listed (first 20)"
    if size.get("bytes") is not None:
        detail += f", {human(size['bytes'])} in {size.get('files')} files"
    return "verified", detail, names[:20], size


def last_meaningful(text, limit=300):
    """The last non-empty LINE, capped — not `text[-limit:]`, which cuts
    mid-token at an offset that depends on the path length. That is how one
    AccessDenied on three buckets produced three different-looking blockers and
    defeated the access worklist's grouping."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return (lines[-1] if lines else "")[:limit]


def aws_env(resources):
    """-> (env, where, registered). The credential `resources.json` DECLARES.

    This function exists because it was missing, and the failure it caused is the
    kind this whole skill is about. `probe_server` reads `resources.json ->
    servers`; `probe_s3` used to shell out to a bare `aws s3 ls` and inherit
    whatever the CLI resolved ambiently. So the registry could hold a working key
    while the probe used a different one and reported `unreachable`, and
    `cmd_sources` — which DOES read `aws.access_key_id` to decide the s3 row is
    usable — would say "s3: usable" about a credential nothing then used.

    Two different keys, one for the checklist and one for the probe, is how a
    sweep reports "no access" over data it can read. It never becomes a false
    `gone`, which is why it survived: the answer stayed safe while being wrong.

    Registry beats ambient deliberately. `resources.json` is the declaration every
    run skill reads through, so a probe resolving something else is answering
    about a different world than the one a run will execute in.
    """
    aws = (resources or {}).get("aws") or {}
    key, secret = aws.get("access_key_id"), aws.get("secret_access_key")
    if not (key and secret):
        return (None, "no aws credential in resources.json — using whatever the "
                "CLI resolves ambiently (env, ~/.aws, instance role)", False)
    env = dict(os.environ)
    env["AWS_ACCESS_KEY_ID"] = key
    env["AWS_SECRET_ACCESS_KEY"] = secret
    # An ambient profile or a session token left over from a different key would
    # override or invalidate the pair we just set, which reintroduces the exact
    # bug: the probe silently answering about a credential nobody chose.
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_SESSION_TOKEN", None)
    if aws.get("region"):
        env["AWS_DEFAULT_REGION"] = aws["region"]
    # Last four only. `leads.json` is git-tracked, and the rule in searches.md is
    # that a record holds a credential's NAME and LOCATION, never its material —
    # four characters is how IAM itself disambiguates keys in a listing.
    return env, f"resources.json -> aws.access_key_id (…{key[-4:]})", True


def probe_s3(path, resources, budget_s):
    if not path.startswith("s3://"):
        return "unreachable", "not an s3:// path", None, {"blocker": "s3:bad_uri"}
    env, where, registered = aws_env(resources)
    try:
        # `--summarize --recursive` is the only way to get a real total, and it
        # is also what makes this the slow probe. Bounded like the local walk.
        p = subprocess.run(["aws", "s3", "ls", "--recursive", "--summarize", path],
                           capture_output=True, text=True, encoding="utf-8", env=env,
                           timeout=max(60.0, budget_s * 4))
    except FileNotFoundError:
        return ("unreachable", "the aws CLI is not installed", None,
                {"blocker": "s3:no_cli"})
    except subprocess.TimeoutExpired:
        return ("unreachable",
                f"the recursive listing exceeded its budget — size is NOT "
                f"MEASURED; the prefix may still be fine", None, {})
    except OSError as exc:
        return "unreachable", f"{type(exc).__name__}: {exc}", None, {}
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        # An empty prefix exits NON-ZERO. `aws s3 ls` returns 1 when it matched
        # nothing, which is indistinguishable from failure by exit code alone —
        # and that is why `--summarize` is worth its cost: it prints
        # "Total Objects: 0" only when the listing actually RAN. Sentinel present
        # and stderr empty is therefore a real reading, and this is the one place
        # in the S3 probe where `gone` is earned rather than guessed. The same
        # both-signals discipline as the ssh probe, inverted: there a zero exit
        # needs a sentinel to be believed, here a non-zero exit needs one to be
        # forgiven.
        if not err and "Total Objects: 0" in (p.stdout or ""):
            return ("gone",
                    f"the prefix listed successfully and holds no objects "
                    f"[credential: {where}]", None, {"blocker": None})
        # Beyond that, an auth failure and an empty prefix are opposite
        # conclusions and both exit non-zero. Only credential and access wording
        # is allowed to mean "could not look".
        low = err.lower()
        if any(k in low for k in ("credential", "accessdenied", "access denied",
                                  "expired", "unable to locate", "forbidden",
                                  "invalidaccesskey")):
            # Two different asks, so two different blockers. A registered key that
            # is refused needs a POLICY change from whoever owns the bucket; no
            # registered key needs a key. Collapsing them sends somebody to
            # request access they already have — and the worklist groups on the
            # blocker, so this is the line that decides what they go and ask for.
            return ("unreachable", f"{last_meaningful(err)} [credential: {where}]",
                    None, {"blocker": "s3:denied_with_registered_key" if registered
                           else "s3:no_usable_credential"})
        if "nosuchbucket" in low.replace(" ", ""):
            return "gone", last_meaningful(err), None, {}
        return ("unreachable", f"aws exit {p.returncode}: {last_meaningful(err)}",
                None, {"blocker": f"s3:exit_{p.returncode}"})

    lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    object_lines = [x for x in lines if not x.startswith("Total ")]
    size = {"bytes": None, "files": None}
    for ln in lines:
        if ln.startswith("Total Objects:"):
            try:
                size["files"] = int(ln.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif ln.startswith("Total Size:"):
            try:
                size["bytes"] = int(ln.split(":", 1)[1].strip())
            except ValueError:
                pass
    if not object_lines and not size["files"]:
        # Require POSITIVE evidence that the listing completed before calling a
        # prefix empty — the same reason census.py wants a sentinel and not just
        # a zero exit. `--summarize` always prints a Total line on success, so
        # its absence next to anything on stderr means we did not get an answer,
        # and "no answer" must not become "no data".
        if "Total Objects:" not in p.stdout:
            return ("unreachable",
                    f"aws exited 0 but printed no summary line, so the prefix was "
                    f"not confirmed empty: {(p.stderr or '').strip()[-200:]}",
                    None, size)
        # The bucket answered, the summary is there, and it is zero. This is the
        # one place `gone` is a real reading rather than a guess.
        return "gone", "the prefix listed successfully and is empty", None, size
    return ("verified",
            f"{size['files']} objects, {human(size['bytes'])}",
            object_lines[:20], size)


def credential_present(spec):
    """-> (found, where). Env var first, then the config file the CLI writes.

    Checked BEFORE importing anything or touching a network, because "there is no
    credential" is the most common answer on a handover, the cheapest one to
    reach, and the one that belongs on the access worklist. Getting it without a
    package installed also means a bare interpreter can still produce the
    worklist, which is the state a takeover actually starts in.
    """
    for var in spec.get("env", ()):
        if os.environ.get(var):
            return True, f"{var} in env"
    for path, needle in spec.get("files", ()):
        full = os.path.expanduser(path)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                if not needle or needle in fh.read():
                    return True, f"{path}" + (f" (entry for {needle})" if needle else "")
        except OSError:
            continue
    names = ", ".join(spec.get("env", ())) or "—"
    return False, (f"no credential found: none of [{names}] in env, and no "
                   f"config file at "
                   f"{' / '.join(p for p, _ in spec.get('files', ())) or '—'}")


# Every tracking backend this can be pointed at, and the split that matters:
# whether probing it needs a credential at all.
#
#   disk     the runs are FILES. A marker glob finds them, nothing is imported,
#            no network is touched, and it works on day one of a handover before
#            any access has arrived. This is the family to probe first, and
#            train-init Step 0 already knows it — its "Local tracking leftovers"
#            row is a default-yes precisely because `wandb/`, `mlruns/` and
#            `lightning_logs/` need no key.
#   service  a project on somebody's server. Credential, then package, then a
#            listing. The first two stages are real and testable on a bare
#            interpreter; only the listing needs an adapter.
#
# A backend with no listing adapter is still worth probing: "the config exists but
# the package is missing" and "there is no credential at all" are different,
# actionable answers, and both come out of stages 1-2. Only wandb is in that state
# now — every other service backend has a urllib listing (see REST_LISTINGS).
TRACKING = {
    "tensorboard": {"family": "disk", "markers": ("events.out.tfevents.*",),
                    "what": "tfevents files"},
    "lightning":   {"family": "disk", "markers": ("lightning_logs/version_*",
                                                  "version_*/hparams.yaml"),
                    "what": "lightning_logs versions"},
    "mlruns":      {"family": "disk", "markers": ("mlruns/*/meta.yaml",
                                                  "*/meta.yaml"),
                    "what": "local mlflow runs"},
    "wandb_local": {"family": "disk", "markers": ("wandb/run-*", "run-*"),
                    "what": "offline wandb run dirs"},

    "wandb": {"family": "service", "pkg": "wandb",
              "env": ("WANDB_API_KEY",),
              "files": (("~/.netrc", "api.wandb.ai"),),
              "listing": "wandb"},
    # `rest` beats `pkg`: urllib needs nothing installed, so the listing runs on a
    # bare interpreter AND is testable against a stub server. mlflow, clearml,
    # neptune and comet all go that way. wandb publishes no REST surface of the same
    # kind, so it is the one backend still on the package path — which is also why
    # it is the one that needed a real account to exercise.
    "mlflow": {"family": "service", "pkg": "mlflow",
               "env": ("MLFLOW_TRACKING_URI", "MLFLOW_TRACKING_TOKEN"),
               "files": (),
               "listing": "mlflow_rest"},
    "clearml": {"family": "service", "pkg": "clearml",
                "env": ("CLEARML_API_ACCESS_KEY", "CLEARML_API_SECRET_KEY",
                        "CLEARML_API_HOST"),
                "files": (("~/clearml.conf", ""),),
                "listing": "clearml_rest"},
    "neptune": {"family": "service", "pkg": "neptune",
                "env": ("NEPTUNE_API_TOKEN",), "files": (),
                "listing": "neptune_rest"},
    "comet": {"family": "service", "pkg": "comet_ml",
              "env": ("COMET_API_KEY",),
              "files": (("~/.comet.config", ""),), "listing": "comet_rest"},
    "aim": {"family": "disk", "markers": (".aim/meta/*", "meta/*"),
            "what": "aim repo metadata"},
    "dvclive": {"family": "disk", "markers": ("dvclive/*", "*/metrics.json"),
                "what": "dvclive outputs"},
}

WANDB_ENTITY_LISTING = (
    "import json,sys\n"
    "try:\n"
    "    import wandb\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e':'no_pkg','m':str(e)})); sys.exit(0)\n"
    "try:\n"
    "    api = wandb.Api(timeout=25)\n"
    "    ent = %r or getattr(api.viewer,'entity',None) or getattr(api.viewer,'username',None)\n"
    "    ps = [p.name for p in api.projects(ent)]\n"
    "    print(json.dumps({'entity': ent, 'projects': ps}))\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e': type(e).__name__, 'm': str(e)[:300]}))\n")

WANDB_LISTING = (
    "import json,sys\n"
    "try:\n"
    "    import wandb\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e':'no_pkg','m':str(e)})); sys.exit(0)\n"
    "try:\n"
    "    api = wandb.Api(timeout=20)\n"
    "    loc = %r\n"
    "    rs = api.runs(loc, per_page=50)\n"
    "    names = []\n"
    "    for i, r in enumerate(rs):\n"
    "        if i >= 20: break\n"
    "        names.append(f'{r.name} [{r.state}]')\n"
    # An empty run list does not distinguish "this project is empty" from "there
    # is no such project" -- the API returns [] for both. So when it comes back
    # empty, ask a second question whose answer differs between those two cases:
    # is the project among the entity's projects? Same both-signals discipline
    # the ssh sentinel and S3's `Total Objects: 0` already use, and without it
    # `gone` would be guessed rather than earned.
    "    ent = loc.split('/')[0]\n"
    "    exists = None\n"
    "    if len(rs) == 0:\n"
    "        try:\n"
    "            exists = loc.split('/')[1] in [p.name for p in api.projects(ent)]\n"
    "        except Exception:\n"
    "            exists = None\n"
    "    print(json.dumps({'n': len(rs), 'names': names, 'project_exists': exists}))\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e': type(e).__name__, 'm': str(e)[:300]}))\n")


def http_json(url, payload=None, token=None, timeout=20.0, headers=None):
    """-> (status_code, parsed_or_None, error_or_None). urllib only: a listing that
    needs no package runs on the interpreter a handover actually starts with.

    `headers` exists because the four REST backends disagree about how to carry a
    credential and there is no winning that argument: MLflow wants
    `Authorization: Bearer`, ClearML wants Basic for its login call and Bearer
    after, Comet wants the raw key in `Authorization` with no scheme at all, and
    Neptune wants `X-Neptune-Api-Token`. `token` stays as the Bearer shorthand
    since three of the five call sites want exactly that.
    """
    import urllib.error
    import urllib.request
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, header_value in (headers or {}).items():
        req.add_header(key, header_value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = (e.read() or b"")[:300].decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, None, body or str(e)
    except urllib.error.URLError as e:
        return None, None, str(e.reason)
    except (OSError, ValueError) as e:
        return None, None, f"{type(e).__name__}: {e}"


def probe_mlflow_rest(path, where, budget_s):
    """Count experiments and runs on an MLflow tracking server over its REST API.

    `path` is either a full tracking URI or an experiment name, in which case
    MLFLOW_TRACKING_URI supplies the base. Both forms occur in the wild — a config
    names the server, a wiki page names the experiment.
    """
    base = (path if path.startswith(("http://", "https://"))
            else os.environ.get("MLFLOW_TRACKING_URI", ""))
    want_exp = None if path.startswith(("http://", "https://")) else path.strip("/")
    if not base.startswith(("http://", "https://")):
        return ("unreachable",
                f"no HTTP tracking URI: {path!r} is not a URL and "
                f"MLFLOW_TRACKING_URI is {os.environ.get('MLFLOW_TRACKING_URI')!r}. "
                f"A `file:` store is the on-disk family — record it as "
                f"`tracking:mlruns` instead", None,
                {"blocker": "tracking:mlflow:no_http_uri"})
    base = base.rstrip("/")
    token = os.environ.get("MLFLOW_TRACKING_TOKEN")

    code, body, err = http_json(f"{base}/api/2.0/mlflow/experiments/search",
                                {"max_results": 200}, token,
                                timeout=max(20.0, budget_s))
    if code is None:
        return ("unreachable", f"could not reach {base}: {err}", None,
                {"blocker": "tracking:mlflow:unreachable_host"})
    if code in (401, 403):
        return ("unreachable",
                f"{base} answered {code} — authentication is the blocker, not "
                f"absence (credential from {where})", None,
                {"blocker": f"tracking:mlflow:http_{code}"})
    if code == 404 or body is None:
        # Older servers only have the GET /list form.
        code, body, err = http_json(f"{base}/api/2.0/mlflow/experiments/list",
                                    None, token, timeout=max(20.0, budget_s))
        if code != 200 or body is None:
            return ("unreachable",
                    f"{base} did not answer either experiments endpoint "
                    f"(last: {code} {err})", None,
                    {"blocker": f"tracking:mlflow:http_{code}"})

    exps = body.get("experiments") or []
    if want_exp is not None:
        exps = [e for e in exps if e.get("name") == want_exp]
        if not exps:
            return ("gone",
                    f"{base} listed its experiments and none is named "
                    f"{want_exp!r}", None, {"blocker": None})
    if not exps:
        return ("gone", f"{base} answered and holds no experiments", None,
                {"blocker": None})

    ids = [e.get("experiment_id") for e in exps if e.get("experiment_id")][:50]
    code, body, err = http_json(f"{base}/api/2.0/mlflow/runs/search",
                                {"experiment_ids": ids, "max_results": 200},
                                token, timeout=max(20.0, budget_s))
    runs = (body or {}).get("runs") or []
    names = []
    for r in runs[:20]:
        run_info = r.get("info") or {}
        names.append(f"{run_info.get('run_id', '?')[:8]} [{run_info.get('status', '?')}]")
    if code != 200:
        # The experiments answered, so the server is real; the run listing did not.
        return ("verified",
                f"{len(exps)} experiment(s) at {base}; the run listing failed "
                f"({code} {err}) so RUNS ARE NOT COUNTED. Credential from {where}. "
                f"This verifies the RECORD exists, not any number in it",
                [e.get("name") for e in exps[:20]], {})
    return ("verified",
            f"{len(exps)} experiment(s), {len(runs)} run(s) at {base} "
            f"(credential from {where}). This verifies the RECORD exists, not any "
            f"number in it — a logged metric is a claim until /repro reproduces it",
            names or [e.get("name") for e in exps[:20]], {})


def config_value(path, keys):
    """-> {key: value} for whichever of `keys` the CLI-written config file names.

    A regex, not a parser, and that is a decision: the files this reads are HOCON
    (`clearml.conf`), INI-ish (`.comet.config`) and whatever somebody hand-edited,
    and `key = "value"` is the only shape all three share. A miss returns nothing
    and every caller degrades to `unreachable` naming the file it could not read a
    key out of — never to `gone`, because failing to parse a config says nothing
    whatsoever about the server.
    """
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    found = {}
    for key in keys:
        m = re.search(r'^[\s"]*' + re.escape(key) + r'"?\s*[:=]\s*"?([^"\s,]+)"?',
                      text, re.MULTILINE)
        if m:
            found[key] = m.group(1)
    return out


def probe_clearml_rest(path, where, budget_s):
    """Count projects and tasks on a ClearML API server over its REST API.

    Two calls deep, because ClearML does not accept the key pair directly: the
    access/secret pair buys a token from `auth.login` (HTTP Basic), and everything
    after is Bearer. Both stages are named separately in the blocker so "the key is
    wrong" and "the key is fine, the listing endpoint moved" do not arrive as one
    undifferentiated failure.

    `path` is a project name or empty (list everything the key can see) — the
    handover shape, same as wandb's entity listing.

    STATUS: exercised against a stub server only. The auth-then-list shape is
    documented ClearML; if a real server disagrees the probe answers `unreachable`
    carrying the endpoint and code, which is one line to fix and is never a false
    `gone`.
    """
    host = (path if path.startswith(("http://", "https://"))
            else os.environ.get("CLEARML_API_HOST", ""))
    conf = config_value("~/clearml.conf",
                        ("api_server", "access_key", "secret_key"))
    host = host or conf.get("api_server", "")
    access = os.environ.get("CLEARML_API_ACCESS_KEY") or conf.get("access_key", "")
    secret = os.environ.get("CLEARML_API_SECRET_KEY") or conf.get("secret_key", "")
    want = None if path.startswith(("http://", "https://")) else (path.strip("/") or None)

    if not host.startswith(("http://", "https://")):
        return ("unreachable",
                f"no ClearML API host: CLEARML_API_HOST is "
                f"{os.environ.get('CLEARML_API_HOST')!r} and ~/clearml.conf named "
                f"no api_server. Note the host must be the API server "
                f"(api.*), not the web app", None,
                {"blocker": "tracking:clearml:no_api_host"})
    if not (access and secret):
        return ("unreachable",
                f"found {where} but not both halves of the key pair "
                f"(access_key {'yes' if access else 'no'}, secret_key "
                f"{'yes' if secret else 'no'}) — ClearML needs both to log in",
                None, {"blocker": "tracking:clearml:partial_credential"})

    host = host.rstrip("/")
    basic = base64.b64encode(f"{access}:{secret}".encode()).decode()
    code, body, err = http_json(f"{host}/auth.login", None,
                                timeout=max(20.0, budget_s),
                                headers={"Authorization": f"Basic {basic}"})
    if code is None:
        return ("unreachable", f"could not reach {host}: {err}", None,
                {"blocker": "tracking:clearml:unreachable_host"})
    token = ((body or {}).get("data") or {}).get("token")
    if code != 200 or not token:
        return ("unreachable",
                f"{host}/auth.login answered {code} without a token "
                f"({last_meaningful(err or '', 160) or 'no error body'}) — "
                f"authentication is the blocker, not absence", None,
                {"blocker": f"tracking:clearml:login_{code}"})

    code, body, err = http_json(f"{host}/projects.get_all",
                                {"page": 0, "page_size": 200,
                                 "only_fields": ["id", "name"]},
                                token, timeout=max(20.0, budget_s))
    projects = ((body or {}).get("data") or {}).get("projects")
    if code != 200 or projects is None:
        return ("unreachable",
                f"logged in to {host} but projects.get_all answered {code} "
                f"({last_meaningful(err or '', 160) or 'unexpected body shape'}). "
                f"The credential works; the listing did not, so NOTHING WAS "
                f"COUNTED", None, {"blocker": f"tracking:clearml:list_{code}"})

    if want is not None:
        projects = [p for p in projects if p.get("name") == want]
        if not projects:
            return ("gone",
                    f"{host} listed its projects and none is named {want!r}",
                    None, {"blocker": None})
    if not projects:
        return ("gone", f"{host} answered and the key sees no projects", None,
                {"blocker": None})

    ids = [p.get("id") for p in projects if p.get("id")][:50]
    code, body, _e = http_json(f"{host}/tasks.get_all",
                               {"project": ids, "page": 0, "page_size": 200,
                                "only_fields": ["id", "name", "status"]},
                               token, timeout=max(20.0, budget_s))
    tasks = ((body or {}).get("data") or {}).get("tasks")
    if code != 200 or tasks is None:
        return ("verified",
                f"{len(projects)} project(s) at {host}; the task listing failed "
                f"({code}) so TASKS ARE NOT COUNTED. Credential from {where}. "
                f"This verifies the RECORD exists, not any number in it",
                [p.get("name") for p in projects[:20]], {})
    return ("verified",
            f"{len(projects)} project(s), {len(tasks)} task(s) at {host} "
            f"(credential from {where}). This verifies the RECORD exists, not any "
            f"number in it — a logged metric is a claim until /repro reproduces it",
            [f"{t.get('name', '?')} [{t.get('status', '?')}]" for t in tasks[:20]]
            or [p.get("name") for p in projects[:20]], {})


def probe_neptune_rest(path, where, budget_s):
    """List Neptune projects the API token can see.

    The token is not a bearer credential — it is base64 of a JSON object carrying
    the API address, so decoding it is also how the host is discovered. That is
    useful on a handover: a Neptune token alone tells you which deployment it
    belongs to, self-hosted included, without anybody writing the URL down.

    STATUS: the token decode and the oauth exchange are documented Neptune and are
    exercised against a stub. The project-listing endpoint is the half most likely
    to be wrong — Neptune reorganised its API between major versions — so a
    mismatch answers `unreachable` naming the endpoint and code rather than
    guessing at a fallback nobody verified.
    """
    raw = os.environ.get("NEPTUNE_API_TOKEN", "")
    if not raw:
        return ("unreachable",
                f"found {where} but NEPTUNE_API_TOKEN is not in this environment "
                f"— the token is the only thing that names the API host, so "
                f"nothing can be probed without it", None,
                {"blocker": "tracking:neptune:no_token_in_env"})
    try:
        pad = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.b64decode(pad).decode("utf-8", "replace"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return ("unreachable",
                "NEPTUNE_API_TOKEN is not decodable base64-JSON — it may be "
                "truncated by a shell quote or be a different kind of key "
                "entirely. The project is not implicated", None,
                {"blocker": "tracking:neptune:undecodable_token"})
    api = (decoded.get("api_url") or decoded.get("api_address") or "").rstrip("/")
    if not api.startswith(("http://", "https://")):
        return ("unreachable",
                f"the token decoded but names no api_url/api_address "
                f"(keys: {sorted(decoded)}) ", None,
                {"blocker": "tracking:neptune:token_names_no_host"})

    code, body, err = http_json(
        f"{api}/api/backend/v1/authorization/oauth-token", None,
        timeout=max(20.0, budget_s), headers={"X-Neptune-Api-Token": raw})
    if code is None:
        return ("unreachable", f"could not reach {api}: {err}", None,
                {"blocker": "tracking:neptune:unreachable_host"})
    access = (body or {}).get("accessToken")
    if code != 200 or not access:
        return ("unreachable",
                f"{api} answered {code} to the oauth-token exchange "
                f"({last_meaningful(err or '', 160) or 'no accessToken in body'}) "
                f"— authentication is the blocker, not absence", None,
                {"blocker": f"tracking:neptune:oauth_{code}"})

    code, body, err = http_json(f"{api}/api/backend/v1/projects", None, access,
                                timeout=max(20.0, budget_s))
    entries = body if isinstance(body, list) else (body or {}).get("entries")
    if code != 200 or entries is None:
        return ("unreachable",
                f"authenticated to {api} but "
                f"/api/backend/v1/projects answered {code} "
                f"({last_meaningful(err or '', 160) or 'unexpected body shape'}). "
                f"The token works; NOTHING WAS COUNTED", None,
                {"blocker": f"tracking:neptune:list_{code}"})

    want = path.strip("/") or None
    names = [f"{p.get('organizationName', '?')}/{p.get('name', '?')}"
             for p in entries if isinstance(p, dict)]
    if want:
        hits = [n for n in names if n == want or n.endswith("/" + want)]
        if not hits:
            return ("gone",
                    f"{api} listed {len(names)} project(s) for this token and "
                    f"none is {want!r}", None, {"blocker": None})
        names = hits
    if not names:
        return ("gone", f"{api} answered and the token sees no projects", None,
                {"blocker": None})
    return ("verified",
            f"{len(names)} project(s) visible at {api} (credential from {where}). "
            f"Neptune's REST does not give a run count per project here, so RUNS "
            f"ARE NOT COUNTED. This verifies the RECORD exists, not any number "
            f"in it", names[:40], {})


def probe_comet_rest(path, where, budget_s):
    """Count Comet projects and experiments over REST v2.

    `path` is `<workspace>` or `<workspace>/<project>`; empty asks the key what
    workspaces it can see, which is again the handover case.

    Comet is the odd one on auth: the key goes in `Authorization` bare, with no
    `Bearer` scheme. Getting that wrong returns 401, which this reports as an auth
    blocker — correct either way, but worth knowing it is the likely cause.

    STATUS: exercised against a stub server only. REST v2's workspaces/projects/
    experiments endpoints are documented and stable.
    """
    key = (os.environ.get("COMET_API_KEY")
           or config_value("~/.comet.config", ("api_key",)).get("api_key", ""))
    if not key:
        return ("unreachable",
                f"found {where} but could not read an api_key out of it — "
                f"the file exists, the key does not, and the workspace is not "
                f"implicated", None,
                {"blocker": "tracking:comet:no_key_value"})
    base = (os.environ.get("COMET_URL_OVERRIDE", "").rstrip("/")
            or "https://www.comet.com").rstrip("/")
    if base.endswith("/clientlib"):          # the SDK's form of the same setting
        base = base[: -len("/clientlib")]
    rest = f"{base}/api/rest/v2"
    auth = {"Authorization": key}

    parts = [p for p in path.strip("/").split("/") if p]
    workspace = parts[0] if parts else None
    want_project = parts[1] if len(parts) > 1 else None

    if workspace is None:
        code, body, err = http_json(f"{rest}/workspaces", None,
                                    timeout=max(20.0, budget_s), headers=auth)
        if code is None:
            return ("unreachable", f"could not reach {base}: {err}", None,
                    {"blocker": "tracking:comet:unreachable_host"})
        wss = (body or {}).get("workspaceNames")
        if code in (401, 403):
            return ("unreachable",
                    f"{base} answered {code} — the key is the blocker, not "
                    f"absence (credential from {where})", None,
                    {"blocker": f"tracking:comet:http_{code}"})
        if code != 200 or wss is None:
            return ("unreachable",
                    f"{rest}/workspaces answered {code} "
                    f"({last_meaningful(err or '', 160) or 'unexpected body'}) — "
                    f"NOTHING WAS COUNTED", None,
                    {"blocker": f"tracking:comet:list_{code}"})
        if not wss:
            return ("gone", f"{base} answered and the key sees no workspaces",
                    None, {"blocker": None})
        return ("verified",
                f"{len(wss)} workspace(s) visible at {base} (credential from "
                f"{where}). Record each workspace you care about as its own lead "
                f"to get project and experiment counts. This verifies the RECORD "
                f"exists, not any number in it", wss[:40], {})

    code, body, err = http_json(f"{rest}/projects?workspaceName={workspace}", None,
                                timeout=max(20.0, budget_s), headers=auth)
    if code is None:
        return ("unreachable", f"could not reach {base}: {err}", None,
                {"blocker": "tracking:comet:unreachable_host"})
    if code in (401, 403):
        return ("unreachable",
                f"{base} answered {code} — the key is the blocker, not absence "
                f"(credential from {where})", None,
                {"blocker": f"tracking:comet:http_{code}"})
    projects = (body or {}).get("projects")
    if code != 200 or projects is None:
        return ("unreachable",
                f"{rest}/projects answered {code} "
                f"({last_meaningful(err or '', 160) or 'unexpected body shape'}) "
                f"— NOTHING WAS COUNTED", None,
                {"blocker": f"tracking:comet:list_{code}"})
    if want_project is not None:
        projects = [p for p in projects if p.get("projectName") == want_project]
        if not projects:
            return ("gone",
                    f"workspace {workspace!r} listed and holds no project named "
                    f"{want_project!r}", None, {"blocker": None})
    if not projects:
        return ("gone", f"workspace {workspace!r} answered and holds no projects",
                None, {"blocker": None})

    total, counted, names = 0, 0, []
    for proj in projects[:20]:
        pid = proj.get("projectId")
        if not pid:
            continue
        code, body, _e = http_json(f"{rest}/experiments?projectId={pid}", None,
                                   timeout=max(20.0, budget_s), headers=auth)
        exps = (body or {}).get("experiments")
        if code != 200 or exps is None:
            continue
        counted += 1
        total += len(exps)
        names.extend(e.get("experimentKey", "?")[:8] for e in exps[:5])
    if counted == 0:
        return ("verified",
                f"{len(projects)} project(s) in workspace {workspace!r}; every "
                f"experiment listing failed so EXPERIMENTS ARE NOT COUNTED. "
                f"Credential from {where}. This verifies the RECORD exists, not "
                f"any number in it",
                [p.get("projectName") for p in projects[:20]], {})
    partial = "" if counted == len(projects[:20]) else (
        f" (counted {counted} of {len(projects)} project(s) — the rest did not "
        f"answer, so this is a LOWER BOUND)")
    return ("verified",
            f"{len(projects)} project(s), {total} experiment(s) in workspace "
            f"{workspace!r}{partial} (credential from {where}). This verifies the "
            f"RECORD exists, not any number in it — a logged metric is a claim "
            f"until /repro reproduces it", names[:40], {})


def probe_wandb_entity(entity, where, budget_s):
    """What can this credential see? Returns the project list as the sample.

    `verified` when the entity resolves and holds projects — the RECORD exists.

    **An empty project list is never `gone` here**, and the reasoning that said it
    was is a fact error worth keeping written down. It claimed parity with an empty
    S3 prefix — "it answered, and there is nothing in it" — but the two APIs behave
    oppositely: `aws s3 ls` on a bucket that does not exist ERRORS, while
    `wandb.Api().projects(<nonsense>)` returns `[]` with no error. Measured, on a
    live account: a real entity gave 10 projects, `definitely-no-such-entity-9f3a`
    gave 0, and so did a malformed locator.

    So an empty list conflates "this entity is empty" with "there is no such
    entity" and cannot tell them apart. Calling that `gone` is the false `gone`
    this whole engine exists to prevent, produced by the engine — and it was found
    exactly that way: a mistyped locator came back `gone` about a project holding
    25 runs, one of them the deployed model's own training.
    """
    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", WANDB_ENTITY_LISTING % (entity or None)],
            capture_output=True, text=True, encoding="utf-8", timeout=max(60.0, budget_s * 2))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return ("unreachable", f"{type(exc).__name__}: {exc}", None,
                {"blocker": "tracking:wandb:entity_probe_failed"})
    try:
        got = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ("unreachable",
                f"could not read the probe's output: "
                f"{last_meaningful(p.stderr or p.stdout, 200)}", None,
                {"blocker": "tracking:wandb:unreadable_output"})
    if got.get("e") == "no_pkg":
        return ("unreachable",
                "the wandb package is not installed in this interpreter — the "
                "entity may be perfectly fine", None,
                {"blocker": "tracking:wandb:no_package"})
    if got.get("e"):
        return ("unreachable", f"{got['e']}: {got['m']} (credential from {where})",
                None, {"blocker": "tracking:wandb:api_error"})
    ps = got.get("projects") or []
    ent = got.get("entity")
    if not ps:
        return ("unreachable",
                f"entity {ent!r} returned no projects -- and for wandb that does "
                f"NOT mean empty. The API returns [] both for an entity with no "
                f"projects and for an entity that does not exist, so this reading "
                f"cannot tell them apart. Check the entity name (a locator "
                f"carrying a `tracking:wandb:` prefix lands here) before treating "
                f"anything as absent [credential: {where}]", None,
                {"blocker": "tracking:wandb:empty_is_ambiguous"})
    return ("verified",
            f"entity {ent!r} holds {len(ps)} project(s), reachable with the "
            f"credential from {where}. Record each project you care about as its "
            f"own lead to get run counts. This verifies the RECORD exists, not "
            f"any number in it", ps[:40], {})


# The listings that need no package installed. Prefer one of these over `pkg`
# whenever a backend offers both: urllib runs on the bare interpreter a handover
# starts with, and — the reason it matters more — a REST listing is testable
# against a stub, so the probe can be exercised without an account. wandb is the
# only backend left on the package path because it publishes no REST surface of
# this kind.
REST_LISTINGS = {
    "mlflow_rest": lambda *a: probe_mlflow_rest(*a),
    "clearml_rest": lambda *a: probe_clearml_rest(*a),
    "neptune_rest": lambda *a: probe_neptune_rest(*a),
    "comet_rest": lambda *a: probe_comet_rest(*a),
}


def probe_tracking_disk(backend, spec, path, budget_s):
    """Count run-shaped things under a path. No credential, no import, no network.

    `gone` here is a real reading — the directory listed and holds no markers —
    which is the same bar probe_s3 sets for an empty prefix.
    """
    import glob
    root = os.path.expanduser(path)
    if not os.path.exists(root):
        return ("gone", f"no such path on this machine", None,
                {"blocker": None})
    if not os.path.isdir(root):
        return ("unreachable", f"{root} is not a directory", None,
                {"blocker": f"tracking:{backend}:not_a_dir"})
    hits, started = [], time.time()
    for marker in spec["markers"]:
        for m in glob.iglob(os.path.join(root, "**", marker), recursive=True):
            hits.append(m)
            if len(hits) >= 500 or time.time() - started > budget_s:
                break
        if hits:
            break
    if not hits:
        return ("gone",
                f"the directory listed and holds no {spec['what']} — checked "
                f"{', '.join(spec['markers'])}", None, {"blocker": None})
    runs = sorted({os.path.dirname(h) for h in hits})
    return ("verified",
            f"{len(runs)} run dir(s) holding {spec['what']}, no credential "
            f"needed. This verifies the RECORD exists, not any number in it",
            [os.path.relpath(r, root) for r in runs[:20]], {"files": len(hits)})


def probe_tracking(on, path, budget_s):
    """Does this tracking project exist, and how much history does it hold.

    The count is the point. "How much of the previous owner's history is still
    there" is what a handover needs answered, and it is exactly what stops being
    answerable when an account is deactivated — so it is worth a probe that runs
    before access arrives rather than a note to check later.

    `verified` means THE RECORD EXISTS. It says nothing about any number inside:
    a run summary reporting mAP 48.5 is a machine-made assertion, still `claimed`
    in `origin.confidence` terms, and only a closed /repro session moves it. Two
    words spelled the same with opposite bars, so every detail says so out loud.

    WHAT HAS ACTUALLY RUN, because "built" and "ran once" are different facts:
    the whole disk family; the credential staging for every service backend; all
    four REST listings against stub servers (mlflow, clearml, neptune, comet —
    urllib only, which is exactly what makes them stubbable); and wandb's listing,
    both forms, against a real account — 25 runs on one project and a 10-project
    entity listing, both matching the raw API. Against a REAL server: mlflow and
    wandb only. The other three are stub-exercised, so a live endpoint that
    disagrees with the documented shape lands as `unreachable` carrying the code
    and the URL — one line to fix, and never a false `gone`.
    """
    backend = on.split(":", 1)[1] if ":" in on else ""
    spec = TRACKING.get(backend)
    if spec is None:
        return ("unreachable",
                f"unknown tracking backend {backend!r} — the locator is recorded "
                f"and readable by hand. Do NOT read this as absent", None,
                {"blocker": f"tracking:{backend}:unknown"})

    # Tolerate every prefix a locator is copied WITH. `wandb:ent/proj` comes out
    # of configs; `tracking:wandb:ent/proj` is what `report` itself renders, so it
    # is the form a person pastes back — and leaving it unstripped made the whole
    # string the entity name, which the wandb API answers with `[]` rather than an
    # error. That is how a project holding 25 runs was reported `gone`.
    loc = path
    for pfx in (f"tracking:{backend}:", f"{backend}:"):
        if loc.lower().startswith(pfx):
            loc = loc[len(pfx):]
            break

    if spec["family"] == "disk":
        return probe_tracking_disk(backend, spec, loc, budget_s)

    have, where = credential_present(spec)
    if not have:
        return ("unreachable", f"{where} — this is a credential lead, not an "
                f"absence", None, {"blocker": f"tracking:{backend}:no_credential"})

    rest = REST_LISTINGS.get(spec.get("listing"))
    if rest is not None:
        return rest(loc, where, budget_s)

    if spec.get("listing") != "wandb":
        # Credential found, no listing adapter. Both halves stated: what IS known
        # (there is a key) and what is not (nobody counted the runs). Reporting
        # only the second would lose the first, which is the actionable half.
        return ("unreachable",
                f"credential found ({where}) but no listing adapter for "
                f"{backend!r} — the history is reachable BY HAND and was not "
                f"counted. Do NOT read this as absent", None,
                {"blocker": f"tracking:{backend}:no_listing_adapter"})

    loc = loc.strip("/")
    if loc.count("/") > 1:
        return ("unreachable", f"cannot parse {path!r} as <entity>[/<project>]",
                None, {"blocker": f"tracking:{backend}:bad_locator"})
    if loc.count("/") == 0:
        # No project named — ask the credential what it can see. This is the
        # handover shape: you inherit a key and nobody wrote down the project
        # names. A probe that demanded `<entity>/<project>` here would refuse the
        # one case where discovery is most needed.
        return probe_wandb_entity(loc, where, budget_s)
    try:
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", WANDB_LISTING % loc],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=max(60.0, budget_s * 2))
    except subprocess.TimeoutExpired:
        return ("unreachable", f"the {backend} API did not answer within the "
                f"budget", None, {"blocker": f"tracking:{backend}:timeout"})
    except OSError as exc:
        return ("unreachable", f"{type(exc).__name__}: {exc}", None,
                {"blocker": f"tracking:{backend}:spawn_failed"})
    try:
        got = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ("unreachable",
                f"could not read the probe's output: "
                f"{last_meaningful(p.stderr or p.stdout, 200)}", None,
                {"blocker": f"tracking:{backend}:unreadable_output"})

    if got.get("e") == "no_pkg":
        return ("unreachable",
                f"the {spec['pkg']} package is not installed in this interpreter "
                f"— the project may be perfectly fine", None,
                {"blocker": f"tracking:{backend}:no_package"})
    if got.get("e"):
        low = (got.get("m") or "").lower()
        # Same discipline as probe_s3: only not-found wording may mean gone.
        if any(k in low for k in ("could not find", "does not exist", "not found",
                                  "no project", "404")):
            return "gone", f"{got['e']}: {got['m']}", None, {"blocker": None}
        return ("unreachable", f"{got['e']}: {got['m']} (credential from {where})",
                None, {"blocker": f"tracking:{backend}:api_error"})

    n = got.get("n")
    if not n:
        # Earned only by the second signal. `[]` alone is the same ambiguity the
        # entity probe hits; the project's presence in the entity's project list
        # is what separates "empty" from "no such project".
        if got.get("project_exists") is True:
            return ("gone", f"the project exists in {loc.split('/')[0]!r} and holds "
                    f"no runs (credential from {where})", None, {"blocker": None})
        if got.get("project_exists") is False:
            return ("gone", f"no project {loc!r} -- it is absent from the entity's "
                    f"own project list (credential from {where})", None,
                    {"blocker": None})
        return ("unreachable",
                f"{loc!r} returned no runs, and the entity's project list could "
                f"not be read to say whether the project exists at all. An empty "
                f"run list does not distinguish the two [credential: {where}]",
                None, {"blocker": "tracking:wandb:empty_is_ambiguous"})
    return ("verified",
            f"{n} run(s), reachable with the credential from {where}. This "
            f"verifies the RECORD exists, not any number in it — a run summary "
            f"is a claim until /repro reproduces it",
            got.get("names") or None, {})
