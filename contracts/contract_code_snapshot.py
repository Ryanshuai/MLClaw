"""Regression tests for the reproduction contract.

The contract: `git checkout <origin_commit> && git apply <run_dir>/code_dirty.patch`
must reconstruct the exact tree that ran. Every test here is a way that contract
was, or could be, silently broken — silently being the point. A snapshot that
loses a file does not raise; it produces a run record that looks complete and
reproduces different code months later.
"""
import os
import subprocess
import sys
import unittest

from helpers import GitRepoCase, load_script

snap = load_script("shared/code_snapshot.py")


class UntrackedFiles(GitRepoCase):
    """run-mechanics.md -> "Code snapshot (Step 2 detail)": untracked files are part
    of the diff. `git diff HEAD` cannot see a file that was never `git add`ed, so
    counting only tracked changes records a tree as clean that is not, and the run
    reproduces different code with nothing raised.
    """

    def test_new_file_counts_as_dirty(self):
        self.make_repo()
        self.write("repo/model_v2.py", "print('never git-added')\n")

        result = snap.capture(self.repo, self.path("run"))

        self.assertEqual(result["dirty_files_count"], 1,
                         "an untracked file must not read as a clean tree")
        self.assertIsNotNone(result["dirty_patch_path"])

    def test_new_file_is_in_the_patch(self):
        self.make_repo()
        self.write("repo/model_v2.py", "print('never git-added')\n")

        snap.capture(self.repo, self.path("run"))
        patch = self.read("run", "code_dirty.patch")

        self.assertIn("model_v2.py", patch)
        self.assertIn("new file", patch)

    def test_patch_actually_reproduces_the_tree(self):
        """The contract end to end, not just the presence of the right strings."""
        self.make_repo()
        self.write("repo/train.py", "print('v2')\n")          # tracked, modified
        self.write("repo/model_v2.py", "print('new')\n")      # untracked

        snap.capture(self.repo, self.path("run"))

        # Throw the working tree away and rebuild it from SHA + patch alone.
        self.git("checkout", "-q", "--", ".")
        os.remove(self.path("repo", "model_v2.py"))
        applied = self.git("apply", self.path("run", "code_dirty.patch"))
        self.assertEqual(applied.returncode, 0, applied.stderr)

        self.assertEqual(self.read("repo", "train.py"), "print('v2')\n")
        self.assertEqual(self.read("repo", "model_v2.py"), "print('new')\n")

    def test_gitignored_files_stay_out(self):
        """Embedding untracked files must not drag in checkpoints and logs."""
        self.make_repo(gitignore="*.log\ncheckpoints/\n")
        self.write("repo/junk.log", "noise\n")
        self.write("repo/checkpoints/epoch_1.txt", "weights\n")
        self.write("repo/real_code.py", "x = 1\n")

        result = snap.capture(self.repo, self.path("run"))
        patch = self.read("run", "code_dirty.patch")

        self.assertEqual(result["dirty_files_count"], 1)
        self.assertIn("real_code.py", patch)
        self.assertNotIn("junk.log", patch)
        self.assertNotIn("epoch_1.txt", patch)

    def test_user_index_is_never_mutated(self):
        """`local` code sources point at the user's live repo, open in their editor.
        Intent-to-add must happen on a throwaway index copy, not on theirs."""
        self.make_repo()
        self.write("repo/model_v2.py", "print('new')\n")
        before = self.git("status", "--porcelain").stdout

        snap.capture(self.repo, self.path("run"))

        self.assertEqual(self.git("status", "--porcelain").stdout, before,
                         "snapshot changed the user's git index")

    def test_oversized_untracked_file_is_reported_not_dropped(self):
        """Skipping is allowed. Skipping quietly is the bug."""
        self.make_repo()
        self.write("repo/big.bin", "x" * 3000)

        result = snap.capture(self.repo, self.path("run"), max_untracked_mb=0.001)

        self.assertEqual(len(result["untracked_skipped"]), 1)
        self.assertEqual(result["untracked_skipped"][0]["path"], "big.bin")
        self.assertFalse(result["reproducible"],
                         "a snapshot missing a file must not claim to be reproducible")
        self.assertTrue(any("big.bin" in w for w in result["warnings"]))


class CleanTree(GitRepoCase):
    """run-mechanics.md -> "Code snapshot (Step 2 detail)": a genuinely clean tree
    reproduces from the SHA alone, with no patch.
    """
    def test_clean_tree_writes_no_patch(self):
        self.make_repo()
        result = snap.capture(self.repo, self.path("run"))

        self.assertTrue(result["reproducible"])
        self.assertIsNone(result["dirty_patch_path"])
        self.assertEqual(result["dirty_files_count"], 0)
        self.assertFalse(os.path.exists(self.path("run", "code_dirty.patch")))

    def test_records_sha_and_branch(self):
        self.make_repo()
        result = snap.capture(self.repo, self.path("run"))

        self.assertEqual(result["origin_commit"], self.base_sha)
        self.assertIsNotNone(result["branch"])


class RefusesWhatItCannotReproduce(GitRepoCase):
    """CLAUDE.md -> "Script Integration": "2 = the script broke, fall back and do it
    manually; 1 = the script worked and the answer is no." A tree with no SHA cannot
    be reproduced, so capturing it would record a contract that does not hold — that
    is a verdict, not a malfunction, and it must exit 1. Exiting anything else sends
    the caller down the fallback branch, where it hand-writes a `code` block for a
    tree that has no reproducible code state. Refusal is also one line with no
    traceback, so the caller can offer `git init` instead of crashing.
    """

    def test_non_git_directory_raises_snapshot_error(self):
        plain = self.path("not_a_repo")
        os.makedirs(plain)
        with self.assertRaises(snap.SnapshotError):
            snap.capture(plain, self.path("run"))

    def test_repo_with_no_commits_raises(self):
        empty = self.path("empty")
        os.makedirs(empty)
        self.git("init", "-q", ".", cwd=empty)
        with self.assertRaises(snap.SnapshotError):
            snap.capture(empty, self.path("run"))

    def test_missing_directory_is_usage_error_not_refusal(self):
        """A path that isn't there is a caller bug, so it stays on the breakage
        branch — the one case here where falling back by hand is right."""
        with self.assertRaises(snap.SnapshotUsageError):
            snap.capture(self.path("nope"), self.path("run"))

    def test_cli_exits_1_on_refusal_without_a_traceback(self):
        from helpers import run_script
        plain = self.path("not_a_repo")
        os.makedirs(plain)

        rc, out, err = run_script("shared/code_snapshot.py", plain, self.path("run"))

        self.assertEqual(rc, 1, "refusal must not read as breakage")
        self.assertNotIn("Traceback", err)
        self.assertIn("error", out)

    def test_cli_exits_2_on_breakage(self):
        from helpers import run_script

        rc, out, err = run_script("shared/code_snapshot.py",
                                  self.path("nope"), self.path("run"))

        self.assertEqual(rc, 2, "a missing code_dir is breakage, not a verdict")
        self.assertIn("error", out)


class ScopedToCodeDir(GitRepoCase):
    """run-mechanics.md -> "Code snapshot (Step 2 detail)": the reproduction contract
    is `git checkout <sha> && git apply <patch>` run from `code_dir`. For the
    `github`, `server` and `null` source modes /project-init puts the code at
    stages/<stage>/code *inside the project's own git repo*, so an unscoped
    `git diff HEAD` writes repo-relative paths that do not exist from `code_dir`
    (apply fails) and drags in project.json (the record counts config as code).
    Neither is signalled: the snapshot succeeds, the run.json looks complete, and
    the failure surfaces months later at reproduction time.
    """

    SUB = "stages/training/code"

    def patched_paths(self, *run):
        """Every path a `diff --git a/X b/X` header names, in the order emitted."""
        return [line.split(" b/", 1)[0][len("diff --git a/"):]
                for line in self.read(*run, "code_dirty.patch").splitlines()
                if line.startswith("diff --git a/")]

    def test_patch_holds_nothing_from_outside_code_dir(self):
        self.make_repo(code_subdir=self.SUB)
        self.write(f"repo/{self.SUB}/train.py", "print('v2')\n")
        self.write("repo/project.json", '{"name": "proj", "env_name": "mlclaw_proj"}\n')

        result = snap.capture(self.repo, self.path("run"))

        self.assertEqual(self.patched_paths("run"), [f"{self.SUB}/train.py"],
                         "a project-level file is not part of the code snapshot")
        self.assertEqual(result["dirty_files_count"], 1)

    def test_patch_applies_from_code_dir(self):
        """The contract end to end, from the directory the run skill uses as cwd."""
        self.make_repo(code_subdir=self.SUB)
        self.write(f"repo/{self.SUB}/train.py", "print('v2')\n")       # tracked
        self.write(f"repo/{self.SUB}/model_v2.py", "print('new')\n")   # untracked
        self.write("repo/project.json", '{"name": "proj", "dirty": true}\n')

        snap.capture(self.repo, self.path("run"))

        self.git("checkout", "-q", "--", ".", cwd=self.repo_root)
        os.remove(self.path("repo", self.SUB, "model_v2.py"))
        # cwd defaults to self.repo, i.e. code_dir — that is the contract.
        applied = self.git("apply", self.path("run", "code_dirty.patch"))
        self.assertEqual(applied.returncode, 0, applied.stderr)

        self.assertEqual(self.read("repo", self.SUB, "train.py"), "print('v2')\n")
        self.assertEqual(self.read("repo", self.SUB, "model_v2.py"), "print('new')\n")

    def test_edits_outside_code_dir_do_not_move_the_counts(self):
        """`dirty_files_count` must describe the code, not the project directory
        MLClaw itself keeps writing to."""
        self.make_repo(code_subdir=self.SUB)
        self.write(f"repo/{self.SUB}/train.py", "print('v2')\n")
        before = snap.capture(self.repo, self.path("run_a"))

        self.write("repo/project.json", '{"name": "proj", "touched": 1}\n')
        self.write("repo/history.json", '{"stack": []}\n')          # untracked, outside
        after = snap.capture(self.repo, self.path("run_b"))

        self.assertEqual(after["dirty_files_count"], before["dirty_files_count"])
        self.assertEqual(after["dirty_files_count"], 1)
        self.assertNotIn("history.json", self.read("run_b", "code_dirty.patch"))

    def test_repo_subdir_records_which_layout_produced_the_record(self):
        self.make_repo(code_subdir=self.SUB)
        self.assertEqual(snap.capture(self.repo, self.path("run"))["repo_subdir"], self.SUB)

    def test_repo_subdir_is_null_when_code_dir_is_the_repo_root(self):
        self.make_repo()
        self.assertIsNone(snap.capture(self.repo, self.path("run"))["repo_subdir"])


class RecordStatesEachFactOnce(GitRepoCase):
    """CLAUDE.md -> "Script Integration": when a script fails the agent does the same
    work by hand and fills run.json itself. Any returned field that merely restates
    another can therefore be hand-written to contradict it — `is_clean: true` beside
    `dirty_files_count: 3` — with nothing anywhere raising. `is_clean`,
    `tracked_dirty_count` and `untracked_files_count` were all derivable and are gone;
    the tracked/untracked split lives on in the warning prose, where it reads as
    narration rather than as an independent fact.
    """

    DERIVABLE = ("is_clean", "tracked_dirty_count", "untracked_files_count")

    def test_derivable_fields_are_not_returned(self):
        self.make_repo()
        self.write("repo/train.py", "print('v2')\n")
        self.write("repo/model_v2.py", "print('new')\n")

        result = snap.capture(self.repo, self.path("run"))

        for field in self.DERIVABLE:
            self.assertNotIn(field, result,
                             f"{field} restates dirty_files_count / untracked_skipped")

    def patch_entry_count(self, *run):
        path = self.path(*run, "code_dirty.patch")
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.startswith("diff --git "))

    def test_count_matches_the_patch_it_describes(self):
        """`dirty_files_count` is read straight off `git status`, which answers a
        different question than `git diff HEAD` in two places: a staged-then-reverted
        file shows as `MM` while differing from HEAD in no way, and whether a rename
        lands in the patch as one entry or as a delete plus an add is decided by
        similarity detection, not by the status code. Both would put a number in
        run.json that the patch beside it contradicts."""
        cases = {
            "staged_then_reverted": ["echo v2 > train.py", "git add train.py",
                                     "echo \"print('v1')\" > train.py"],
            "pure_rename":          ["git mv train.py renamed.py"],
            "rename_plus_edit":     ["git mv train.py renamed.py",
                                     "echo v2 > renamed.py"],
            "staged_add_then_rm":   ["echo n > new.py", "git add new.py", "rm new.py"],
        }
        for name, cmds in cases.items():
            with self.subTest(case=name):
                self.make_repo(name=name)
                for cmd in cmds:
                    subprocess.run(cmd, shell=True, cwd=self.repo, capture_output=True)

                result = snap.capture(self.repo, self.path(name + "_run"))

                self.assertEqual(result["dirty_files_count"],
                                 self.patch_entry_count(name + "_run"))

    def test_warnings_still_carry_the_tracked_untracked_split(self):
        self.make_repo()
        self.write("repo/model_v2.py", "print('new')\n")

        result = snap.capture(self.repo, self.path("run"))

        self.assertTrue(any("1 untracked file(s)" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()


class AStageWithNoTreeHasADifferentContractNotAMissingOne(GitRepoCase):
    """`layout.md` -> "Code Source Resolution", the `framework` mode; and
    run-mechanics.md -> "Code snapshot (Step 2 detail)", the no-tree branch.

    When what you inherit is a built artifact plus a package — a `.deb` holding a
    `.pt`, a released checkpoint, a model somebody handed you — the eval "code" is
    an installed framework's CLI. There is nothing to `git init`: site-packages is
    owned by the package manager. Refusing there is correct about the tree and
    wrong about the world, because the run IS reproducible, under a different
    contract:

        git tree   ->  git checkout <sha> && git apply <patch>
        framework  ->  <env_manager> install <pkg>==<version>

    The danger this class guards is not the missing tree, it is the record. A
    framework record's `origin_commit` is null by construction; a git record whose
    capture failed also has a null. If those two read the same, a reader six months
    on cannot tell "there was nothing to capture" from "we failed to capture", and
    only the first is reproducible. `kind` is the field that separates them, which
    is why it is asserted on BOTH branches here.
    """

    def framework(self, spec):
        return snap.capture_framework(spec, self.path("run"))

    def setUp(self):
        super().setUp()
        os.makedirs(self.path("run"), exist_ok=True)

    def test_a_pinned_framework_is_captured_not_refused(self):
        rec = self.framework("ultralytics==8.4.40")
        self.assertEqual(rec["kind"], "framework")
        self.assertEqual(rec["framework"], "ultralytics")
        self.assertEqual(rec["framework_version"], "8.4.40")
        self.assertTrue(rec["reproducible"],
                        "installing that version does rebuild the code")

    def test_kind_is_on_the_git_branch_too(self):
        """Without this the field is useless: `kind` absent would have to mean
        both "a git record" and "an old record", and the whole point is telling a
        constructed null from a failed one."""
        self.make_repo()
        self.assertEqual(snap.capture(self.repo, self.path("run"))["kind"], "git")

    def test_the_nulls_are_by_construction_and_say_so(self):
        rec = self.framework("ultralytics==8.4.40")
        for field in ("repo", "branch", "origin_commit", "repo_subdir",
                      "dirty_patch_path"):
            self.assertIsNone(rec[field], f"{field} must be null on this branch")
        self.assertEqual(rec["kind"], "framework",
                         "and `kind` is what makes those nulls readable")

    def test_dirty_files_count_is_null_not_zero(self):
        """"There is no tree" and "the tree was clean" are different facts. A 0
        asserts the second. Same rule as a byte count that was never measured —
        null means not measured and never means zero."""
        self.assertIsNone(self.framework("ultralytics==8.4.40")["dirty_files_count"])

    def test_an_unpinned_framework_is_refused(self):
        """A package name is not a reproduction contract. Recording `ultralytics`
        as though it were `ultralytics==8.4.40` produces a run that reads as
        reproducible and is not — the same bar as the SHA a git record refuses to
        invent. Exit 1: the script worked and the answer is no."""
        with self.assertRaises(snap.SnapshotError):
            self.framework("ultralytics")
        with self.assertRaises(snap.SnapshotError):
            self.framework("ultralytics==")

    def test_a_nameless_spec_is_a_caller_bug_not_a_refusal(self):
        with self.assertRaises(snap.SnapshotUsageError):
            self.framework("==8.4.40")

    def test_the_blind_spot_rides_along_as_a_warning(self):
        """The pin cannot see a local edit — a monkeypatched site-packages file or
        a stray sitecustomize.py leaves no trace, where a tree would have produced
        a patch. `reproducible: true` next to no caveat would overstate the
        contract, so the caveat is not optional."""
        rec = self.framework("ultralytics==8.4.40")
        self.assertEqual(len(rec["warnings"]), 1)
        self.assertIn("local edit", rec["warnings"][0])

    def test_the_cli_takes_this_branch_and_still_wants_a_code_dir(self):
        """run-mechanics.md: "The same call applies to all run skills — no
        per-skill variant." Keeping `code_dir` positional is what preserves that,
        so the flag must work alongside it without the directory being a repo."""
        plain = self.path("not_a_repo")
        os.makedirs(plain, exist_ok=True)
        out = subprocess.run(
            # `sys.executable`, not "python" -- a machine can have python3 only,
            # and then this test does not fail, it *errors*: a test that cannot
            # run is not a test that passes.
            [sys.executable, snap.__file__, plain, self.path("run"),
             "--framework", "ultralytics==8.4.40"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"kind": "framework"', out.stdout)

    def test_without_the_flag_that_same_directory_is_still_refused(self):
        """The new branch must not become a way past the old refusal. A stage that
        genuinely has a tree and forgot to `git init` gets the same answer it
        always did."""
        plain = self.path("not_a_repo2")
        os.makedirs(plain, exist_ok=True)
        out = subprocess.run([sys.executable, snap.__file__, plain, self.path("run")],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not a git working tree", out.stdout)
