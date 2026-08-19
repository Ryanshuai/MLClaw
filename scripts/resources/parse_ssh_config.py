"""Parse ~/.ssh/config and output server entries as JSON.

`/resources` reads this to seed `resources.json`, which every run skill then
resolves `${}` through. Three things it now keeps straight:

  the alias is not thrown away   `Host gpu` + `HostName 10.0.0.4` used to yield
                                 `alias: ""`, because the alias and the sole
                                 name were the same string before HostName
                                 overwrote the host. `ssh gpu` is the only
                                 spelling that carries the user's ProxyJump and
                                 IdentityFile, so it is the one worth recording
  one bad line is one bad line   `Port` used to be `int(val)` with no guard, so
                                 a single malformed line lost the WHOLE server
                                 list to a traceback
  Include is followed            a config split across `~/.ssh/config.d/*` used
                                 to yield a short list presented as the complete
                                 one, which is the shape CLAUDE.md calls out:
                                 never report data you could not look at

Output: JSON on stdout -- `{"servers": [...], "sources": [...], "warnings": [...]}`.
The bare list is still accepted by callers that index it; `sources` is what makes
the list's completeness checkable.
"""
import glob as _glob
import json
import os
import sys


def _entry(aliases):
    """A Host stanza's initial record. `aliases[0]` is what you type at `ssh`."""
    return {
        "alias": aliases[0],
        # Overwritten by a HostName line if there is one; until then the alias IS
        # the host, which is the correct reading of a bare `Host gpu`.
        "host": aliases[-1],
        "username": "",
        "ssh_key_path": "",
        "port": 22,
        "description": "from SSH config",
        "gpu": "",
        "gpu_count": 0,
    }


def _parse_one(path, servers, warnings, sources, seen):
    real = os.path.abspath(os.path.expanduser(path))
    if real in seen:                      # Include cycles are legal to write
        return
    seen.add(real)
    if not os.path.isfile(real):
        return
    sources.append(real)
    current = None
    try:
        text = open(real, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as exc:
        warnings.append("%s could not be read (%s) -- the server list is a lower "
                        "bound, not an inventory" % (real, type(exc).__name__))
        return

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        key = parts[0].lower()

        if key == "include":
            if current:
                servers.append(current)
                current = None
            for pattern in parts[1:]:
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = os.path.join(os.path.dirname(real), pattern)
                for inc in sorted(_glob.glob(pattern)):
                    _parse_one(inc, servers, warnings, sources, seen)
            continue

        if key == "host":
            if current:
                servers.append(current)
            aliases = parts[1:]
            if not aliases or any("*" in a or "?" in a for a in aliases):
                current = None            # a pattern stanza names no one machine
                continue
            current = _entry(aliases)
        elif key == "match":
            # Its body applies conditionally; attributing it to the preceding
            # Host would record settings that may not apply.
            if current:
                servers.append(current)
            current = None
        elif current:
            val = " ".join(parts[1:])
            if key == "hostname":
                current["host"] = val
            elif key == "user":
                current["username"] = val
            elif key == "identityfile":
                current["ssh_key_path"] = val
            elif key == "port":
                try:
                    current["port"] = int(val)
                except ValueError:
                    warnings.append("%s:%d: Port %r is not a number -- kept the "
                                    "default 22 for %s" % (real, lineno, val,
                                                           current["alias"]))
    if current:
        servers.append(current)


def parse_ssh_config(path=None):
    """-> [server dicts]. Kept as the list for callers that already index it."""
    return parse_ssh_config_full(path)["servers"]


def parse_ssh_config_full(path=None):
    if path is None:
        # Works on both Windows and Unix
        path = os.path.join(os.path.expanduser("~"), ".ssh", "config")
    servers, warnings, sources = [], [], []
    _parse_one(path, servers, warnings, sources, set())
    return {"servers": servers, "sources": sources, "warnings": warnings,
            "complete": not warnings}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    json.dump(parse_ssh_config_full(path), sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
