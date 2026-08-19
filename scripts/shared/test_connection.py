"""Test connectivity to remote resources (SSH/S3). Outputs JSON result.

`ok` MEANS THE CONNECTION WORKED, AND NOTHING ELSE. With `remote_path` the
command run is `test -e P && echo exists || echo not_found`, which exits 0
either way -- so a reachable machine that does NOT have the path reported
`ok: true` exactly like one that does, and the only difference was a word
buried in `output`. `/{stage}-run` Step 1 calls this to confirm a declared
source resolves, and a candidate whose path is gone would have passed.

So the two facts are separate fields: `ok` is reachability, `exists` is the
path -- true, false, or None when no path was asked about. CLAUDE.md: a
machine that did not answer, a path that is not there, and a directory that is
genuinely empty are three facts.
"""
import json
import shlex
import shutil
import subprocess
import sys


def test_ssh(host, port, username, ssh_key_path, remote_path=None, timeout=10):
    if not shutil.which("ssh"):
        return {"ok": False, "error": "ssh command not found. Install OpenSSH or Git Bash."}

    cmd = ["ssh", "-o", f"ConnectTimeout={timeout}", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
    if ssh_key_path:
        cmd += ["-i", ssh_key_path]
    if port and port != 22:
        cmd += ["-p", str(port)]
    cmd.append(f"{username}@{host}")

    if remote_path:
        cmd.append(f"test -e {shlex.quote(remote_path)} && echo exists || echo not_found")
    else:
        cmd.append("echo ok")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exists": None,
                "error": f"Connection timed out after {timeout}s"}
    except (OSError, ValueError) as e:
        return {"ok": False, "exists": None, "error": str(e)}

    out = r.stdout.strip()
    result = {
        "ok": r.returncode == 0,
        "output": out,
        "error": r.stderr.strip() if r.returncode != 0 else None,
        # None, not False: with no path asked about there is nothing to report,
        # and with an unreachable machine nothing looked.
        "exists": None,
    }
    if remote_path and r.returncode == 0:
        if out.endswith("exists"):
            result["exists"] = True
        elif out.endswith("not_found"):
            result["exists"] = False
        else:
            result["‼️"] = ("the existence probe printed something unrecognised; "
                            "`exists` stays null rather than being guessed at")
    return result


def test_s3(path, region=None, profile=None, timeout=10):
    if not shutil.which("aws"):
        return {"ok": False, "error": "aws CLI not found. Install AWS CLI first."}

    cmd = ["aws", "s3", "ls", path]
    if region:
        cmd += ["--region", region]
    if profile:
        cmd += ["--profile", profile]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "AWS CLI timed out"}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": r.returncode == 0,
        "output": r.stdout.strip()[:200],
        "error": r.stderr.strip() if r.returncode != 0 else None,
    }


def main():
    # CLAUDE.md "Script Integration": usage is 2 (the caller fixes the call), an
    # unreachable host is 1 (the script worked and the answer is no). Both were
    # 1, so "you typed it wrong" arrived as "that machine is down".
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage", "fix": [
            "python test_connection.py ssh <host> <username> [ssh_key_path] [port] [remote_path]",
            "python test_connection.py s3 <s3_path> [region] [profile]"]}, indent=2))
        sys.exit(2)

    conn_type = sys.argv[1]

    if conn_type == "ssh":
        host = sys.argv[2] if len(sys.argv) > 2 else ""
        username = sys.argv[3] if len(sys.argv) > 3 else ""
        ssh_key = sys.argv[4] if len(sys.argv) > 4 else ""
        port = int(sys.argv[5]) if len(sys.argv) > 5 else 22
        remote_path = sys.argv[6] if len(sys.argv) > 6 else None
        conn_result = test_ssh(host, port, username, ssh_key, remote_path)

    elif conn_type == "s3":
        path = sys.argv[2] if len(sys.argv) > 2 else ""
        region = sys.argv[3] if len(sys.argv) > 3 else None
        profile = sys.argv[4] if len(sys.argv) > 4 else None
        conn_result = test_s3(path, region, profile)

    else:
        print(json.dumps({"error": f"unknown connection type: {conn_type}",
                          "fix": "ssh | s3"}, indent=2))
        sys.exit(2)

    json.dump(conn_result, sys.stdout, indent=2, ensure_ascii=False)
    print()
    # `exists is False` is a real answer about a reachable machine, and the
    # caller must not read it as a pass: the point of asking was the path.
    sys.exit(0 if conn_result["ok"] and conn_result.get("exists") is not False else 1)


if __name__ == "__main__":
    main()
