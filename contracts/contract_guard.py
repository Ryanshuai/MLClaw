"""The PreToolUse guard: it must refuse what CLAUDE.md reserves for a script.

Enforces CLAUDE.md -> "Never silently", whose opening paragraph declares that
`hooks/guard_destructive.py` blocks a delete aimed at a checkpoint, at frozen
data, at a run record or at a `knowledge/` file, and that it BLOCKS rather than
warns. This earns a check on the bar in "Contracts": an irreversible action.

The direction that matters is the false NEGATIVE. A wrongly-blocked `rm` costs one
message; a wrongly-allowed one costs a file nothing ranked. So the deny cases are
the contract and the allow cases only keep the guard usable.
"""
import json
import os
import subprocess
import unittest

from helpers import REPO_ROOT

GUARD = os.path.join(REPO_ROOT, "hooks", "guard_destructive.py")


def run(command, tool="Bash"):
    """-> the hook's decision, or None when it declined to have an opinion."""
    p = subprocess.run(["python3", GUARD], input=json.dumps(
        {"tool_name": tool, "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, f"a guard that crashes fails open: {p.stderr}"
    if not p.stdout.strip():
        return None
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


class GuardRefusesReservedDeletes(unittest.TestCase):
    """CLAUDE.md -> "Never silently", the four rules naming a plan -> apply route."""

    def test_checkpoint_deletes_are_denied(self):
        for cmd in (
            "rm -rf runs/run_007/checkpoints/best.pth",
            "rm stages/training/runs/run_003/checkpoints/last.ckpt",
            "find . -name '*.safetensors' -delete",
            "aws s3 rm s3://b/checkpoints/epoch_9.pt",
            "python -c \"import shutil; shutil.rmtree('checkpoints')\"",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(run(cmd), "deny")

    def test_frozen_data_deletes_are_denied(self):
        """`retire.py plan` is the only thing that reads manifests and census together."""
        for cmd in (
            "rm -rf datasets/boxes",
            "aws s3 sync ./local s3://b/datasets/boxes --delete",
            "rm datasets/boxes/manifest.json",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(run(cmd), "deny")

    def test_record_deletes_are_denied(self):
        """A run record is what a conclusion rests on; deleting it makes a
        `supported` belief `unverifiable` with nothing raising."""
        for cmd in (
            "rm stages/training/runs/run_003/run.json",
            "rm -rf knowledge/",
            "rm knowledge/conclusions.json",
            "rm -f stages/exploration/graph.json",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(run(cmd), "deny")

    def test_a_delete_hidden_after_a_separator_is_still_seen(self):
        """The guard reads the whole command line, not its first word."""
        for cmd in (
            "echo ok && rm -rf runs/run_1/checkpoints",
            "cd /x; rm datasets/boxes/manifest.json",
            "ls | xargs echo && sudo rm -rf checkpoints/best.pth",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(run(cmd), "deny")


class GuardStaysOutOfTheWay(unittest.TestCase):
    """A guard that blocks ordinary work gets removed, and then guards nothing."""

    def test_the_sanctioned_routes_pass(self):
        for cmd in (
            "python /x/scripts/train-run/retention.py apply --project p",
            "python /x/scripts/data-retire/retire.py apply --project p",
            "python /x/scripts/evacuate/evacuate.py verify --project p",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(run(cmd))

    def test_reads_and_unrelated_deletes_pass(self):
        for cmd in (
            "ls runs/run_007/checkpoints/",
            "cat stages/training/runs/run_003/run.json",
            "rm /tmp/scratch.log",
            "rm -rf node_modules",
            "git status",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(run(cmd))

    def test_a_non_bash_tool_is_not_the_guards_business(self):
        self.assertIsNone(run("rm -rf checkpoints/best.pth", tool="Read"))


class GuardIsWiredAndFailsOpenOnJunk(unittest.TestCase):
    """A hook declared with the wrong path is a guard nobody notices is absent."""

    def test_hooks_json_points_at_the_guard_that_exists(self):
        d = json.load(open(os.path.join(REPO_ROOT, "hooks", "hooks.json"), encoding="utf-8"))
        entries = d["hooks"]["PreToolUse"]
        cmds = [h["command"] for e in entries for h in e["hooks"]]
        self.assertTrue(any("guard_destructive.py" in c for c in cmds), cmds)
        self.assertTrue(all("${CLAUDE_PLUGIN_ROOT}" in c for c in cmds),
                        "a plugin hook must not depend on the working directory")
        self.assertEqual([e["matcher"] for e in entries], ["Bash"])
        self.assertTrue(os.path.isfile(GUARD))

    def test_malformed_input_is_not_a_licence_to_block(self):
        p = subprocess.run(["python3", GUARD], input="not json",
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
