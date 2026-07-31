"""Test connectivity to remote resources (SSH/S3). Outputs JSON result."""
import json
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
        cmd.append(f"test -e {remote_path} && echo exists || echo not_found")
    else:
        cmd.append("echo ok")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return {
            "ok": r.returncode == 0,
            "output": r.stdout.strip(),
            "error": r.stderr.strip() if r.returncode != 0 else None,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Connection timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def test_s3(path, region=None, profile=None, timeout=10):
    if not shutil.which("aws"):
        return {"ok": False, "error": "aws CLI not found. Install AWS CLI first."}

    cmd = ["aws", "s3", "ls", path]
    if region:
        cmd += ["--region", region]
    if profile:
        cmd += ["--profile", profile]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": r.returncode == 0,
            "output": r.stdout.strip()[:200],
            "error": r.stderr.strip() if r.returncode != 0 else None,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "AWS CLI timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python test_connection.py ssh <host> <username> [ssh_key_path] [port] [remote_path]")
        print("  python test_connection.py s3 <s3_path> [region] [profile]")
        sys.exit(1)

    conn_type = sys.argv[1]

    if conn_type == "ssh":
        host = sys.argv[2] if len(sys.argv) > 2 else ""
        username = sys.argv[3] if len(sys.argv) > 3 else ""
        ssh_key = sys.argv[4] if len(sys.argv) > 4 else ""
        port = int(sys.argv[5]) if len(sys.argv) > 5 else 22
        remote_path = sys.argv[6] if len(sys.argv) > 6 else None
        result = test_ssh(host, port, username, ssh_key, remote_path)

    elif conn_type == "s3":
        path = sys.argv[2] if len(sys.argv) > 2 else ""
        region = sys.argv[3] if len(sys.argv) > 3 else None
        profile = sys.argv[4] if len(sys.argv) > 4 else None
        result = test_s3(path, region, profile)

    else:
        result = {"ok": False, "error": f"Unknown connection type: {conn_type}"}

    json.dump(result, sys.stdout, indent=2)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
