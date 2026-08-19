#!/usr/bin/env python3
"""PreToolUse(Bash): refuse a delete that CLAUDE.md reserves for a plan -> apply script.

WHY A HOOK AND NOT `allowed-tools`. A skill's `allowed-tools` binds only while
that skill runs. Every rule this guard enforces exists for the opposite moment --
CLAUDE.md, "Never silently": *"the moment they matter most is when no skill is
running -- a user says 'clean up the old checkpoints' ... nothing loads, and an
obliging agent does the wrong thing without anything raising."* A session-wide
PreToolUse hook is the only mechanism that is present in exactly that moment.

IT BLOCKS, and that is deliberate. `rig.py`'s tripwire only warns, correctly, for
a capture operator who cannot re-shoot the frames. `identity.md` build step 8 draws
the line: non-blocking is "wrong for a script whose next statement is `rm -rf`
over ssh." A delete is not recoverable by re-running it.

FALSE POSITIVES ARE THE CHEAP DIRECTION. A wrongly-blocked `rm` costs one message
saying which script to use. A wrongly-allowed one costs a checkpoint nothing
ranked -- and `retention.py` refuses to rank a file with no metric, which is the
whole reason the route exists.

Stdlib only, like everything under contracts/. Exit 0 with no output = no opinion.
"""
import json
import re
import sys

# The sanctioned routes. A command that IS one of these is never blocked -- they
# are the plan -> apply pairs the rules point at.
SANCTIONED = re.compile(r"""
    lifecycle/scripts/(
        train-run/retention\.py      # checkpoints
      | data-retire/retire\.py       # data, against a census listing
      | evacuate/evacuate\.py        # a box about to be destroyed
      | data-check/census\.py        # reads only
    )
""", re.X)

# A delete, in the forms that actually appear.
DELETE = re.compile(r"""
    (^|[;&|]\s*|\$\(\s*)(
        rm\s                                  # rm, rm -rf, sudo rm
      | sudo\s+rm\s
      | find\s.*-delete\b
      | find\s.*-exec\s+rm\b
      | aws\s+s3\s+(rm|rb)\b
      | aws\s+s3\s+sync\b.*--delete\b         # the quiet one: sync deletes too
      | (gsutil|rclone)\s+(rm|delete|purge)\b
      | shutil\.rmtree\b
      | (os|pathlib)\S*\.(unlink|remove|rmdir)\b
      | truncate\s+-s\s*0\b                   # zeroing a file is a delete of its bytes
      | :\s*>\s*\S                            # `: > file`
      | mv\s+\S+\s+/dev/null\b
    )
""", re.X)

# What must not be deleted outside a route. Ordered: first match names the rule.
PROTECTED = [
    (re.compile(r"\.(pth|pt|ckpt|safetensors|bin|onnx)\b|\bckpts?\b|\bcheckpoints?\b|\bbest\.|\blast\."),
     "a checkpoint",
     "`retention.py plan` -> `apply`. CLAUDE.md: *never delete a checkpoint outside* it, and "
     "*showing the user a list of filenames is not confirmation* -- the list carries no evidence "
     "the ranking behind it was right. Never delete a file you cannot rank."),
    (re.compile(r"\bdatasets?\b|\bsnapshots?\b|\bmanifest\.json\b"),
     "data a frozen snapshot may still name",
     "`retire.py plan` -> `apply`. It is the only thing that reads the manifests and the census "
     "together, so a delete outside it cannot know whether a snapshot still cites the unit. The "
     "bytes go; the citation stays, and every run that cited it goes on reading as reproducible."),
    (re.compile(r"\brun\.json\b|\bruns?/run_|\bstages?/"),
     "a run record",
     "nothing. A run record is the evidence a conclusion rests on -- `conclude.py status` "
     "re-derives belief from it, and deleting it turns a `supported` conclusion into an "
     "`unverifiable` one with nothing anywhere raising."),
    (re.compile(r"\bknowledge/|conclusions\.json|graph\.json|leads\.json|census/"),
     "a record nothing can reconstruct",
     "nothing. These are written once and read later by someone who can no longer verify them."),
]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # malformed input is not a licence to block
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd or not DELETE.search(cmd):
        return 0
    if SANCTIONED.search(cmd):
        return 0
    for pattern, what, route in PROTECTED:
        if pattern.search(cmd):
            json.dump({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"MLClaw guard: this deletes {what}, which belongs to {route}\n\n"
                    f"blocked: {cmd[:300]}\n\n"
                    "If the delete is genuinely right, run it through that script -- it is the "
                    "thing that can prove it. If the script refuses, that refusal IS the answer "
                    "(exit 1 = it worked and said no; only exit 2 means fall back and do it by hand)."
                ),
            }}, sys.stdout)
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
