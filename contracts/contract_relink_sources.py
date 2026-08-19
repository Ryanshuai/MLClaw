#!/usr/bin/env python3
"""`shared/relink_sources.py` -- the one repair script that can destroy a code tree.

CLAUDE.md -> "Contracts" admits a check for *an irreversible action*, and this is
one: it writes symlinks into `stages/<stage>/code/_source`, the path the code_dir
rule resolves through. Two of its outcomes are the interesting ones and both were
uncovered.

  REFUSED     a real file or directory occupies `_source`. Replacing it with a
              symlink deletes a code tree that may be the only copy, and nothing
              downstream could tell that it happened. `/project-init`'s SKILL.md
              names this refusal as the reason not to hand-roll `ln -sfn`.
  UNRESOLVED  the target does not exist on this host. A symlink to nothing is
              WORSE than no symlink: `code_dir = code/_source if exists else code/`
              silently falls back, and the run then executes a different tree than
              the one its record names. Nothing raises.

The second is the one this file exists for. Every other failure here is loud.
"""
import os
import unittest

from helpers import TempDirCase, load_script, requires_symlinks, run_script

relink = load_script("shared/relink_sources.py")
SCRIPT = "shared/relink_sources.py"


class TheTwoRefusals(TempDirCase):

    def setUp(self):
        super().setUp()
        self.code = self.path("elsewhere", "myrepo")
        os.makedirs(self.code, exist_ok=True)
        requires_symlinks(self.code, self.path("_probe_link"))

    def project(self, path=None, stage="training", enabled=True, make_stage=True):
        if make_stage:
            os.makedirs(self.path("proj", "stages", stage, "code"), exist_ok=True)
        self.write_json("proj/project.json", {
            "name": "p",
            "stages": {stage: {"enabled": enabled,
                               "code_source": {"source": "local",
                                               "path": self.code if path is None else path}}},
        })
        return self.path("proj")

    def only(self, report):
        self.assertEqual(len(report["stages"]), 1, report)
        return report["stages"][0]

    # --- the happy path, so the refusals below mean something ----------------

    def test_a_missing_link_is_created(self):
        e = self.only(relink.relink_all(self.project()))
        self.assertEqual(e["action"], relink.CREATED)
        self.assertEqual(os.path.realpath(e["link"]), os.path.realpath(self.code))

    def test_running_it_twice_is_a_no_op(self):
        root = self.project()
        relink.relink_all(root)
        self.assertEqual(self.only(relink.relink_all(root))["action"], relink.OK)

    def test_a_link_pointing_somewhere_else_is_replaced(self):
        root = self.project()
        other = self.path("elsewhere", "old")
        os.makedirs(other, exist_ok=True)
        os.symlink(other, self.path("proj", "stages", "training", "code", "_source"))
        e = self.only(relink.relink_all(root))
        self.assertEqual(e["action"], relink.RELINKED)
        self.assertEqual(os.path.realpath(e["link"]), os.path.realpath(self.code))

    # --- REFUSED: the irreversible one ---------------------------------------

    def test_a_real_directory_at_source_is_refused_and_left_alone(self):
        root = self.project()
        occupied = self.path("proj", "stages", "training", "code", "_source")
        os.makedirs(occupied)
        self.write("proj/stages/training/code/_source/train.py", "# the only copy\n")
        e = self.only(relink.relink_all(root))
        self.assertEqual(e["action"], relink.REFUSED)
        self.assertFalse(os.path.islink(occupied))
        self.assertEqual(self.read("proj/stages/training/code/_source/train.py"),
                         "# the only copy\n",
                         "the tree that was there must still be there")

    def test_a_real_file_at_source_is_refused_too(self):
        root = self.project()
        self.write("proj/stages/training/code/_source", "not a link\n")
        e = self.only(relink.relink_all(root))
        self.assertEqual(e["action"], relink.REFUSED)
        self.assertIn("destroy", e["message"])

    # --- UNRESOLVED: the silent one ------------------------------------------

    def test_a_target_that_is_not_on_this_host_creates_no_link(self):
        """The whole point. A dangling link makes `code_dir` fall back to `code/`
        and the run executes a tree its own record does not name."""
        root = self.project(path=self.path("elsewhere", "not_here"))
        e = self.only(relink.relink_all(root))
        self.assertEqual(e["action"], relink.UNRESOLVED)
        self.assertFalse(os.path.lexists(e["link"]),
                         "no link at all is better than one pointing at nothing")

    def test_an_existing_dangling_link_is_left_rather_than_deleted(self):
        root = self.project(path=self.path("elsewhere", "gone"))
        link = self.path("proj", "stages", "training", "code", "_source")
        os.symlink(self.path("elsewhere", "gone"), link)
        e = self.only(relink.relink_all(root))
        self.assertEqual(e["action"], relink.UNRESOLVED)
        self.assertTrue(os.path.islink(link),
                        "deleting it destroys the evidence of what was declared")

    def test_a_good_link_is_not_replaced_by_one_pointing_at_nothing(self):
        root = self.project()
        link = self.path("proj", "stages", "training", "code", "_source")
        os.symlink(self.code, link)
        self.write_json("proj/project.json", {
            "name": "p",
            "stages": {"training": {"enabled": True, "code_source": {
                "source": "local", "path": self.path("elsewhere", "not_here")}}},
        })
        e = self.only(relink.relink_all(root))
        self.assertEqual(e["action"], relink.UNRESOLVED)
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.code))

    def test_an_empty_declared_path_is_unresolved_not_a_link_to_cwd(self):
        e = self.only(relink.relink_all(self.project(path="")))
        self.assertEqual(e["action"], relink.UNRESOLVED)


class OneBrokenStageDoesNotStrandTheOthers(TempDirCase):
    """Its docstring states it: one broken stage must not leave the others
    dangling. A repair script that stops at the first problem leaves a project
    half-relinked, which is the state hardest to diagnose later."""

    def setUp(self):
        super().setUp()
        self.code = self.path("elsewhere", "myrepo")
        os.makedirs(self.code, exist_ok=True)
        requires_symlinks(self.code, self.path("_probe_link"))

    def test_a_refused_stage_does_not_stop_a_repairable_one(self):
        for s in ("training", "evaluation"):
            os.makedirs(self.path("proj", "stages", s, "code"), exist_ok=True)
        os.makedirs(self.path("proj", "stages", "evaluation", "code", "_source"))
        self.write_json("proj/project.json", {
            "name": "p",
            "stages": {s: {"enabled": True, "code_source": {"source": "local",
                                                            "path": self.code}}
                       for s in ("training", "evaluation")},
        })
        report = relink.relink_all(self.path("proj"))
        by = {e["stage"]: e["action"] for e in report["stages"]}
        self.assertEqual(by["evaluation"], relink.REFUSED)
        self.assertEqual(by["training"], relink.CREATED)
        self.assertEqual(report["unresolved"], ["evaluation"])

    def test_only_local_source_stages_are_touched(self):
        """Every other mode puts real files under `stages/<stage>/code/` and has
        no `_source` link to repair -- walking them would be the REFUSED case
        arriving for every project."""
        for s in ("training", "inference"):
            os.makedirs(self.path("proj", "stages", s, "code"), exist_ok=True)
        self.write_json("proj/project.json", {
            "name": "p",
            "stages": {
                "training": {"enabled": True,
                             "code_source": {"source": "local", "path": self.code}},
                "inference": {"enabled": True,
                              "code_source": {"source": "github", "path": "x/y"}},
            },
        })
        report = relink.relink_all(self.path("proj"))
        self.assertEqual([e["stage"] for e in report["stages"]], ["training"])


class TheExitCodesSayWhichKindOfNo(TempDirCase):
    """CLAUDE.md -> "Script Integration". 2 means the skill falls back and does
    the work by hand; here that means hand-writing `ln -sfn`, which is exactly
    what `/project-init`'s SKILL.md forbids because it clobbers silently. So an
    unrepairable STAGE must be 1 and never 2."""

    def setUp(self):
        super().setUp()
        requires_symlinks(self.tmp, self.path("_probe_link"))

    def test_no_project_json_is_exit_2(self):
        rc, out, _err = run_script(SCRIPT, self.path("nothing"))
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_an_unrepairable_stage_is_exit_1_not_2(self):
        os.makedirs(self.path("proj", "stages", "training", "code"))
        self.write_json("proj/project.json", {
            "name": "p",
            "stages": {"training": {"enabled": True, "code_source": {
                "source": "local", "path": self.path("nope")}}},
        })
        rc, out, _err = run_script(SCRIPT, self.path("proj"))
        self.assertEqual(rc, 1)
        self.assertEqual(out["unresolved"], ["training"])

    def test_everything_resolved_is_exit_0(self):
        code = self.path("elsewhere", "repo")
        os.makedirs(code)
        os.makedirs(self.path("proj", "stages", "training", "code"))
        self.write_json("proj/project.json", {
            "name": "p",
            "stages": {"training": {"enabled": True,
                                    "code_source": {"source": "local", "path": code}}},
        })
        rc, _out, _err = run_script(SCRIPT, self.path("proj"))
        self.assertEqual(rc, 0)

    def test_dry_run_reports_without_touching_the_filesystem(self):
        code = self.path("elsewhere", "repo")
        os.makedirs(code)
        os.makedirs(self.path("proj", "stages", "training", "code"))
        self.write_json("proj/project.json", {
            "name": "p",
            "stages": {"training": {"enabled": True,
                                    "code_source": {"source": "local", "path": code}}},
        })
        report = relink.relink_all(self.path("proj"), dry_run=True)
        self.assertEqual(report["stages"][0]["action"], relink.CREATED)
        self.assertFalse(os.path.lexists(
            self.path("proj", "stages", "training", "code", "_source")),
            "dry-run said CREATED; nothing may be on disk")


if __name__ == "__main__":
    unittest.main()
