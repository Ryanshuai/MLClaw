"""Parse ~/.ssh/config and output server entries as JSON."""
import json
import os
import sys


def parse_ssh_config(path=None):
    if path is None:
        # Works on both Windows and Unix
        path = os.path.join(os.path.expanduser("~"), ".ssh", "config")

    if not os.path.isfile(path):
        return []

    servers = []
    current = None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if line.lower().startswith("host "):
                if current:
                    servers.append(current)
                aliases = line.split()[1:]
                # Skip wildcard entries
                if any("*" in a for a in aliases):
                    current = None
                    continue
                current = {
                    "alias": aliases[0] if aliases[0] != aliases[-1] else "",
                    "host": aliases[-1],  # last value is usually the IP/hostname
                    "username": "",
                    "ssh_key_path": "",
                    "port": 22,
                    "description": "from SSH config",
                    "gpu": "",
                    "gpu_count": 0,
                }
            elif current:
                key = line.split()[0].lower()
                val = " ".join(line.split()[1:])
                if key == "hostname":
                    current["host"] = val
                elif key == "user":
                    current["username"] = val
                elif key == "identityfile":
                    current["ssh_key_path"] = val
                elif key == "port":
                    current["port"] = int(val)

    if current:
        servers.append(current)

    return servers


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    servers = parse_ssh_config(path)
    json.dump(servers, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
