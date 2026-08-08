#!/usr/bin/env python3
"""Go to a named resource and bring data back. Record what actually arrived.

Collect is where data enters MLClaw's world. The operation is deliberately
small: name a resource, name a path on it, pull. What makes it worth a script
rather than a remembered rsync line is the record — which resource, which
session, how much arrived, and whether the transfer finished.

**Ingest only.** One direction, always. This never pushes, never deletes at the
source, and never overwrites an existing file unless told to. That is not the
same as `/data-check`'s "never moves a byte", and the distinction matters:
that rule is about bidirectional SYNC (whose rules — refuse-if-exists for
source, newest-wins for derived — are every project's own) and about DELETION
(irreversible, needs `plan -> apply` against evidence). A one-directional copy
into a directory MLClaw controls is neither.

**The transfer is not ours.** `rsync` and `aws s3` do it; this only decides what
to invoke and writes down what came of it. Reimplementing copy semantics would
put a second, worse copy of them in play.

**People are not handled here.** When the data is not there yet because somebody
has to go and capture it, or a site has to be visited, that is an exchange with
a party MLClaw does not control — `/data-label`, which already owns the ledger, the
counterparty registry and the "what is still outstanding" report. This script
never waits for a human.

Exit codes per CLAUDE.md "Script Integration": 1 = worked, the answer is no;
2 = broke, do it by hand.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402

# What a transfer can be. Each maps to somebody else's tool; nothing here
# implements copying.
KINDS = ("server", "s3", "local")


def resources_beside_project(project, explicit):
    """--resources, else $MLCLAW_RESOURCES, else the workspace beside the project.

    The third fallback is **not the same one** `lease/_common.py ->
    resources_from_workspace_root` uses: that one reads `workspace_root` out of
    CLAUDE.md, because `reap` and `release` must run with zero upstream state.
    Both were called `resources_path`, and two functions with one name that can
    resolve to *different registry files* is worse than either fallback being
    wrong -- a disagreement there is silent and nothing downstream can see it.
    `resources.json` is workspace-level and never committed; a project sits under
    the workspace root, so its parent is the default place to look."""
    for c in (explicit, os.environ.get("MLCLAW_RESOURCES"),
              os.path.join(os.path.dirname(os.path.expanduser(project).rstrip("/")),
                           "resources.json")):
        if c and os.path.isfile(os.path.expanduser(c)):
            return os.path.expanduser(c)
    return None


def resolve(project, frm, at, resources_arg):
    """-> (kind, remote_spec, described). `frm` is a key in resources.json ->
    servers, or the literal 's3' / 'local'.

    A server is named, never spelled out: the host, user and key live in
    resources.json, which is the never-committed file. A collect record carries
    the KEY, so the record can be committed and the address cannot leak through
    it — the same split /data-collect's rig.json and /data-label already use."""
    if frm == "local":
        src = os.path.expanduser(at)
        if not os.path.exists(src):
            broke(f"local source does not exist: {src}")
        return "local", src.rstrip("/") + "/", {"kind": "local", "path": src}

    if frm == "s3":
        if not at.startswith("s3://"):
            broke("--at must be an s3:// URI when --from is s3", got=at)
        return "s3", at.rstrip("/") + "/", {"kind": "s3", "uri": at}

    rpath = resources_beside_project(project, resources_arg)
    if not rpath:
        broke("cannot locate resources.json",
              hint="pass --resources, set $MLCLAW_RESOURCES, or put it at the workspace root")
    servers = (read_json(rpath).get("servers") or {})
    entry = servers.get(frm)
    if not entry or frm.startswith("_"):
        broke(f"no server {frm!r} in {rpath}",
              known=[k for k in servers if not k.startswith("_")],
              hint="run /resources to register it")
    host = entry.get("host") or entry.get("alias")
    if not host:
        broke(f"server {frm!r} has neither host nor alias")
    user = entry.get("username")
    target = f"{user}@{host}" if user else host
    return "server", f"{target}:{shlex.quote(at).rstrip('/')}/", {
        "kind": "server", "resource": frm, "path": at,
        # host/user/key deliberately NOT recorded — see the docstring above.
    }


def build_cmd(kind, remote, dest, args, *, dry):
    if kind == "s3":
        cmd = ["aws", "s3", "sync", remote, dest]
        if dry:
            cmd.append("--dryrun")
        for pat in args.exclude or []:
            cmd += ["--exclude", pat]
        for pat in args.include or []:
            cmd += ["--include", pat]
        return cmd

    cmd = ["rsync", "-a", "--info=stats2"]
    # Never clobber by default. A capture tree's source layers are irreplaceable,
    # and an ingest that quietly overwrites one has destroyed the thing it was
    # supposed to be rescuing.
    if not args.overwrite:
        cmd.append("--ignore-existing")
    if dry:
        cmd.append("--dry-run")
    for pat in args.exclude or []:
        cmd += ["--exclude", pat]
    for pat in args.include or []:
        cmd += ["--include", pat]
    if kind == "server":
        port = args.port
        cmd += ["-e", f"ssh -p {port}" if port else "ssh"]
    cmd += [remote, dest]
    return cmd


def run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except FileNotFoundError:
        broke(f"{cmd[0]} is not installed", cmd=" ".join(cmd))
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def count_dest(dest):
    n = b = 0
    for dp, _, fs in os.walk(dest):
        for f in fs:
            try:
                b += os.path.getsize(os.path.join(dp, f))
                n += 1
            except OSError:
                pass
    return n, b


def cmd_plan(a):
    project = os.path.expanduser(a.project)
    kind, remote, described = resolve(project, a.frm, a.at, a.resources)
    dest = os.path.expanduser(a.into)
    os.makedirs(dest, exist_ok=True)
    cmd = build_cmd(kind, remote, dest.rstrip("/") + "/", a, dry=True)
    rc, out, err = run(cmd, a.timeout)
    if rc is None or rc != 0:
        # Unreachable is a finding, not a crash — and it is the finding that
        # most often means a person has to go and do something.
        refuse(f"could not reach the source ({kind})",
               source=described, tool_said=(err or out).strip()[:400],
               hint="if somebody has to go capture or connect it, that is an "
                    "exchange with a party MLClaw does not control — use /data-label "
                    "(kind: data_request) so it is tracked instead of remembered")
    emit({"would_pull_from": described, "into": dest, "dry_run": True,
          "tool": cmd[0], "output": out.strip()[-1500:],
          "overwrite_existing": bool(a.overwrite)})


def cmd_pull(a):
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    kind, remote, described = resolve(project, a.frm, a.at, a.resources)
    dest = os.path.expanduser(a.into)
    os.makedirs(dest, exist_ok=True)

    before_n, before_b = count_dest(dest)
    started = now_utc()
    cmd = build_cmd(kind, remote, dest.rstrip("/") + "/", a, dry=False)
    rc, out, err = run(cmd, a.timeout)
    finished = now_utc()
    after_n, after_b = count_dest(dest)

    # A transfer that did not finish must never be recorded as one that did.
    # rsync's partial-transfer exits (23/24) mean SOME files did not come; a
    # session marked complete on that basis is a lower bound wearing a total.
    complete = rc == 0
    session = {
        "session": a.session,
        "collected_at": finished,
        "started_at": started,
        "source": described,
        "into": dest,
        "tool": cmd[0],
        "exit_code": rc,
        "complete": complete,
        "files_before": before_n, "files_after": after_n,
        "files_added": after_n - before_n,
        "bytes_added": after_b - before_b,
        "overwrite_existing": bool(a.overwrite),
        "stderr": (err or "").strip()[-1500:] or None,
        "rig_stamp": None,
        "cited_window": None,
    }

    # The denominator, when this pull is a biased sample of production. A biased
    # pull is the right thing to do — you want the hard frames, not a random
    # five hundred — but the bias is invisible afterwards: once the frames are on
    # disk they look exactly like data somebody captured. `/data-online-sample`'s
    # uniform reading is the only thing that can say what they were drawn FROM,
    # so the citation is what keeps the selection computable rather than a memory.
    if a.cite_window:
        ds, _, wid = a.cite_window.partition("/")
        if not ds or not wid:
            broke("--cite-window must be <dataset>/<window_id>",
                  got=a.cite_window)
        wpath = os.path.join(project, "datasets", ds, "online", f"{wid}.json")
        w = read_json(wpath) if os.path.isfile(wpath) else None
        if not w:
            refuse(f"no online reading {a.cite_window}",
                   why="a denominator that is not on record is not a "
                       "denominator; recording the citation without it would "
                       "make an unmeasured bias read as a measured one",
                   looked_at=wpath,
                   fix="/data-online-sample sample, over the window this pull "
                       "drew from")
        session["cited_window"] = {
            "window": f"{ds}/{wid}", "record": wpath,
            "interval": w.get("window"),
            "population": w.get("population"),
            "population_basis": w.get("population_basis"),
            "enumerated": w.get("enumerated"),
            # Carried through rather than recomputed, so a rate quoted off this
            # pull inherits the reading's own honesty about its denominator.
            "rates_are": w.get("rates_are"),
            "window_complete": w.get("complete"),
        }

    # Optional provenance. A rig is one KIND of source and most are not: an S3
    # prefix has no serial number. When the resource is a declared rig, stamp it
    # into the tree that was just pulled, so what produced the data travels with
    # the data. Failure to stamp never fails the pull — the bytes are already in.
    if a.rig:
        rig_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rig.py")
        p = subprocess.run([sys.executable, "-X", "utf8", rig_py, "stamp", "--project", a.project,
                            "--rig", a.rig, "--into", dest] +
                           (["--session", a.session] if a.session else []) +
                           (["--overwrite"] if a.overwrite else []),
                           capture_output=True, text=True, encoding="utf-8")
        try:
            session["rig_stamp"] = json.loads(p.stdout) if p.stdout.strip() else None
        except json.JSONDecodeError:
            session["rig_stamp"] = {"error": (p.stderr or p.stdout).strip()[:300]}
        if p.returncode != 0:
            session["rig_stamp"] = {"failed": True,
                                    "detail": (p.stderr or p.stdout).strip()[:300]}

    stamp = os.path.join(dest, "_collect",
                         f"collect_{(a.session or finished).replace(':', '').replace('-', '')}.json")
    atomic_write_json(stamp, session)

    result = {k: session[k] for k in
              ("session", "source", "into", "complete", "exit_code",
               "files_added", "bytes_added")}
    result["record"] = stamp
    result["rig_stamped"] = bool(session["rig_stamp"])
    if session["cited_window"]:
        cw = session["cited_window"]
        result["denominator"] = {k: cw[k] for k in
                                 ("window", "population", "population_basis",
                                  "rates_are")}
        if cw["population_basis"] != "declared":
            result["note_denominator"] = (
                "the cited reading has no known population, so what fraction of "
                "production this pull represents is a LOWER BOUND — say it as one")
    if not complete:
        result["warning"] = ("the transfer did not finish cleanly, so files_added "
                             "is a LOWER BOUND — re-run to continue, and do not "
                             "treat this session as a complete ingest")
        emit(result)
        sys.exit(1)
    result["next"] = "census.py scan — confirms it landed and clears UNARCHIVED"
    emit(result)


def cmd_status(a):
    """Every collect session under a tree, newest last. No network.

    Records live beside the data they describe, and `--into` is often outside
    the project (a mounted disk, a landing zone), so the tree to scan is an
    argument. Defaulting it to the project and stopping there would report "no
    sessions" for data that was in fact collected."""
    root = os.path.expanduser(a.root or a.project)
    rows, errors = [], []
    for dp, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        if os.path.basename(dp) != "_collect":
            continue
        for f in sorted(fs):
            if not (f.startswith("collect_") and f.endswith(".json")):
                continue
            path = os.path.join(dp, f)
            try:
                with open(path, encoding="utf-8") as fh:
                    r = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                errors.append({"path": path, "error": str(exc)})
                continue
            rows.append({"session": r.get("session"), "at": r.get("collected_at"),
                         "source": r.get("source"), "into": r.get("into"),
                         "complete": r.get("complete"),
                         "files_added": r.get("files_added"),
                         "rig_stamped": bool(r.get("rig_stamp"))})
    rows.sort(key=lambda r: r.get("at") or "")
    emit({"sessions": rows, "count": len(rows),
          "incomplete": [r["session"] for r in rows if r.get("complete") is False],
          "errors": errors})


def add_common(p):
    p.add_argument("--project", required=True)
    p.add_argument("--from", dest="frm", required=True,
                   help="a key in resources.json -> servers, or 's3' / 'local'")
    p.add_argument("--at", required=True, help="path (or s3:// URI) on that resource")
    p.add_argument("--into", required=True, help="destination directory")
    p.add_argument("--include", action="append")
    p.add_argument("--exclude", action="append")
    p.add_argument("--overwrite", action="store_true",
                   help="allow clobbering existing files; off by default")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--resources", default=None)
    p.add_argument("--timeout", type=int, default=86400)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="dry run: what would come across")
    add_common(pl)
    pl.set_defaults(fn=cmd_plan)

    pu = sub.add_parser("pull", help="bring it back and record the session")
    add_common(pu)
    pu.add_argument("--session", default=None, help="label, e.g. the capture date")
    pu.add_argument("--rig", default=None,
                    help="optional: stamp this rig's reading into what was pulled")
    pu.add_argument("--cite-window", default=None,
                    metavar="<dataset>/<window_id>",
                    help="a /data-online-sample reading, when this pull is a "
                         "BIASED sample of production. Records its denominator: "
                         "without one, N interesting frames came out of an "
                         "unknown number and the bias is not computable")
    pu.set_defaults(fn=cmd_pull)

    st = sub.add_parser("status", help="collect sessions on record; no network")
    st.add_argument("--project", required=True)
    st.add_argument("--root", default=None,
                    help="tree to scan; defaults to the project. Point it at the "
                         "data root when --into landed outside the project")
    st.set_defaults(fn=cmd_status)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
