"""The research artifact — what a round leaves behind when it is over.

ARA (arXiv:2604.24658) argues the artifact IS the research object rather than a
byproduct. MLClaw produced all four of its layers already and had nowhere to put
them: a finished round left a directory of runs, which is a different thing from
something a person can read a year later.

Every check here covers a way the artifact can read STRONGER or MORE COMPLETE
than what it holds — a `logic/` layer frozen at `supported` while the live
record has gone `unverifiable`, an index that contradicts the directory beside
it, weights and numbers with no `src/` presented as an artifact rather than a
backup. That is CLAUDE.md -> "Conventions"'s bar exactly: a record written now
and read later by somebody who can no longer verify it.
"""

import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "ara/ara.py"


class AraCase(TempDirCase):
    """A project with one run, one conclusion, and an exploration graph."""

    RUN = os.path.join("proj", "stages", "training", "runs", "run_A")

    def setUp(self):
        super().setUp()
        self.proj = self.path("proj")
        self.write_json(os.path.join(self.RUN, "run.json"),
                        {"run_id": "run_A", "code": {"reproducible": True}})
        self.write_json(os.path.join(self.RUN, "config_snapshot.json"), {"lr": 1e-3})
        self.write(os.path.join(self.RUN, "stream.jsonl"), '{"step": 1}\n')
        self.write(os.path.join(self.RUN, "best.pth"), "W" * 512)
        self.write(os.path.join(self.RUN, "logs", "train.log"), "line\n")
        self.conclude([{"id": "K01", "statement": "one-to-one helps",
                        "scope": {"corpus": "datasets/b@1"},
                        "status": "supported", "tier": "T2"}])
        self.write_json(os.path.join("proj", "stages", "exploration", "graph.json"),
                        {"nodes": [{"id": "N07", "verdict": "won"}]})

    def conclude(self, rows):
        self.write_json(os.path.join("proj", "knowledge", "conclusions.json"),
                        {"conclusions": rows})

    def a(self, *args):
        return run_script(SCRIPT, *args, "--project", self.proj)

    def build(self, *extra, expect=0):
        rc, out, err = self.a("build", *extra)
        if expect is not None:
            self.assertEqual(rc, expect, f"build rc={rc}: {out or err}")
        return out

    def check(self, expect=None):
        rc, out, err = self.a("check", "--no-fail")
        self.assertEqual(rc, 0, f"check broke: {out or err}")
        return out

    def ids(self):
        base = self.path("proj", "ara")
        return sorted(d for d in (os.listdir(base) if os.path.isdir(base) else [])
                      if os.path.isdir(os.path.join(base, d)))

    def md(self, aid=None):
        with open(self.path("proj", "ara", aid or self.ids()[-1], "ARTIFACT.md"),
                  encoding="utf-8") as f:
            return f.read()

    def findings(self, severity=None):
        return [f for f in self.check()["findings"]
                if severity is None or f["severity"] == severity]


# ---------------------------------------------------------------------------

class TheLayersAreARAsPlusTheOneItDoesNotHave(AraCase):
    """ARA (arXiv:2604.24658) — `logic/` `src/` `trace/` `evidence/` — and the
    reason MLClaw needs a fifth.

    A paper's artifact is its KNOWLEDGE, and knowledge regenerates from src +
    evidence. A checkpoint does not. `weights/` is the only layer the other
    four cannot rebuild, which is also why its partial transfer costs the most
    (`/evacuate`).
    """

    def test_all_five_layers_are_separated(self):
        out = self.build()
        c = out["layers"]
        self.assertEqual(c["src"], 1)          # config_snapshot.json
        self.assertEqual(c["weights"], 1)
        self.assertGreaterEqual(c["evidence"], 2)
        self.assertEqual(c["logic"], 1)
        self.assertEqual(c["trace"], 1)

    def test_a_checkpoint_under_a_code_directory_is_still_weights(self):
        self.write(os.path.join(self.RUN, "src", "pytorch_model.bin"), "W" * 16)
        self.assertEqual(self.build()["layers"]["weights"], 2)

    def test_a_log_is_evidence_because_that_is_where_a_number_was_read(self):
        """MLClaw's grounding rule makes the transcribed log line the evidence a
        metric was read rather than recalled — same word, one layer down."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ara_mod", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "ara", "ara.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.classify("logs/train.log"), "evidence")

    def test_the_records_are_copied_in_physically(self):
        """They must stay readable without pulling the weights back down, and
        they are what survives when the weights do not.

        Each keeps its own path under the layer. Flattening to a basename was
        survivable while only two directories were read and stops being so the
        moment a second stage has a `config.json` -- one would silently
        overwrite the other inside the artifact.
        """
        out = self.build()
        self.assertIn("logic/knowledge/conclusions.json", out["copied"])
        self.assertIn("trace/stages/exploration/graph.json", out["copied"])
        self.assertTrue(os.path.exists(self.path(
            "proj", "ara", out["id"], "logic", "knowledge", "conclusions.json")))

    def test_the_index_never_contradicts_the_directory_beside_it(self):
        """The counts must include what was copied in. Reporting "no logic
        layer" on a bundle holding `logic/conclusions.json` is worse than either
        answer alone — it teaches the reader that the index is decoration."""
        out = self.build()
        self.assertEqual(out["layers"]["logic"], 1)
        self.assertNotIn("no `logic` layer", json.dumps(out))
        self.assertNotIn("No `logic/` layer", self.md())

    def test_an_unrecognised_file_is_kept_and_named(self):
        self.write(os.path.join(self.RUN, "notes_from_the_intern.txt"), "read me")
        out = self.build()
        self.assertEqual(out["layers"]["unclassified"], 1)
        self.assertIn("notes_from_the_intern.txt", json.dumps(out))


class AnArtifactWithoutItsInputIsABackup(AraCase):
    """The user's framing, and it is the right one: "code + config is effectively
    what reproducibility means". In an architecture search the CODE IS THE VARIABLE, so `src/` is not
    context around the result — it is the reproducibility claim.

    Weights and numbers with no way to regenerate them is a backup. An ablation
    read off one cannot say what differed between two arms, which is the only
    question anybody asks it.
    """

    def test_no_src_layer_is_named_as_a_backup(self):
        os.remove(self.path(self.RUN, "config_snapshot.json"))
        os.remove(self.path(self.RUN, "run.json"))
        out = self.build()
        self.assertEqual(out["layers"]["src"], 0)
        self.assertIn("BACKUP", out["‼️src"])

    def test_no_logic_layer_routes_to_the_skill_that_writes_one(self):
        os.remove(self.path("proj", "knowledge", "conclusions.json"))
        out = self.build()
        self.assertIn("/conclude", out["‼️logic"])

    def test_a_missing_src_layer_is_critical_at_check(self):
        os.remove(self.path(self.RUN, "config_snapshot.json"))
        os.remove(self.path(self.RUN, "run.json"))
        self.build()
        rc, out, err = self.a("check")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")


class ReproducibilityIsReadNotClaimed(AraCase):
    """`references/run-mechanics.md` -> "Record integrity", the code
    snapshot contract: `reproducible: false` means a differing file was not
    embedded, so `git checkout && git apply` rebuilds a DIFFERENT tree.

    Read from the run's own snapshot rather than re-derived — `code_snapshot.py`
    already refused what it could refuse, and a second opinion here could only
    disagree with it.

    ‼️ It never blocks a build. Losing the bytes is strictly worse than saving
    them under an honest label, which is why a census that could not reach a
    machine is stamped `complete: false` rather than withheld.
    """

    def _code(self, **kw):
        self.write_json(os.path.join(self.RUN, "run.json"),
                        {"run_id": "run_A", "code": kw})

    def test_a_reproducible_snapshot_reads_yes(self):
        self.assertEqual(self.build()["reproducible"], "yes")

    def test_a_non_reproducible_snapshot_reads_no_and_says_why(self):
        self._code(reproducible=False)
        out = self.build()
        self.assertEqual(out["reproducible"], "no")
        self.assertIn("different tree", out["reproducibility"])

    def test_a_missing_verdict_is_unknown_not_no(self):
        """Not the same fact, and collapsing them is the extraction-failure-
        versus-absence bug one domain over."""
        self._code()
        self.assertEqual(self.build()["reproducible"], "unknown")

    def test_it_never_blocks_the_build(self):
        self._code(reproducible=False)
        rc, out, err = self.a("build")
        self.assertEqual(rc, 0, f"reproducibility must not gate a build: {out or err}")

    def test_the_index_carries_the_label(self):
        self._code(reproducible=False)
        self.build()
        self.assertIn("Reproducible?", self.md())
        self.assertIn("**no**", self.md())


class AFrozenBeliefDoesNotUpdateItself(AraCase):
    """CLAUDE.md -> "Never silently": 「Never repeat a conclusion without
    re-reading its status」 — and this is that failure one level up, in the copy
    people actually read.

    `/conclude` computes `status` and `tier` from what the evidence currently
    resolves to. The artifact FREEZES them. Nothing about the frozen copy changes
    when its evidence rots, and the frozen copy is the one handed to whoever
    takes over. `check` reports the drift; it never repairs it, for the same
    reason `graph.py check` does not — a record that silently corrects itself
    cannot be audited.
    """

    def test_a_status_that_moved_since_the_build_is_critical(self):
        self.build()
        self.conclude([{"id": "K01", "statement": "one-to-one helps",
                        "status": "unverifiable", "tier": "T2"}])
        blob = " ".join(f["detail"] for f in self.findings("critical"))
        self.assertIn("froze `supported`", blob)
        self.assertIn("unverifiable", blob)

    def test_a_tier_that_moved_is_critical(self):
        """CLAUDE.md: 「The tier travels with the number forever, in every file
        and every sentence.」 An artifact is a file."""
        self.build()
        self.conclude([{"id": "K01", "statement": "one-to-one helps",
                        "status": "supported", "tier": "T1"}])
        blob = " ".join(f["detail"] for f in self.findings("critical"))
        self.assertIn("froze tier T2", blob)

    def test_a_conclusion_that_vanished_is_critical(self):
        self.build()
        self.conclude([])
        blob = " ".join(f["detail"] for f in self.findings("critical"))
        self.assertIn("no longer in", blob)

    def test_a_conclusion_added_afterwards_is_reported(self):
        self.build()
        self.conclude([{"id": "K01", "statement": "one-to-one helps",
                        "status": "supported", "tier": "T2"},
                       {"id": "K02", "statement": "later", "status": "supported",
                        "tier": "T1"}])
        blob = " ".join(f["detail"] for f in self.findings())
        self.assertIn("K02", blob)
        self.assertIn("rebuild", blob)

    def test_an_unchanged_record_is_clean(self):
        self.build()
        self.assertEqual(self.findings("critical"), [])

    def test_check_repairs_nothing(self):
        self.build()
        aid = self.ids()[-1]
        before = self.read(os.path.join("proj", "ara", aid, "ara.json"))
        self.conclude([{"id": "K01", "statement": "s", "status": "refuted",
                        "tier": "T2"}])
        self.a("check", "--no-fail")
        self.assertEqual(self.read(os.path.join("proj", "ara", aid, "ara.json")),
                         before)
        self.assertIn("reports", self.check()["repaired"])

    def test_the_index_says_the_statuses_are_a_snapshot(self):
        self.build()
        self.assertIn("ara.py check", self.md())

    def test_checking_before_building_is_a_refusal(self):
        rc, out, err = self.a("check")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")


class AnArtifactNeedsNoMachine(AraCase):
    """`/evacuate` is scoped to a MACHINE and gated on a lease; this is scoped to
    a ROUND and has no deadline. The relationship is caller, not container — the
    moment before a box dies is the last moment its source can be read, so the
    deadline FORCES the artifact to be finished rather than owning it.

    The check is that the artifact can be built with no machine anywhere in
    sight, because until it was split out it could not be: an exploration that
    ended on hardware nobody was releasing produced no artifact at all.
    """

    def test_it_builds_straight_from_the_project(self):
        out = self.build()
        self.assertTrue(os.path.exists(out["artifact"]))
        self.assertEqual(out["layers"]["logic"], 1)

    def test_an_alternate_root_is_classified_instead(self):
        """What `/evacuate` passes: the doomed machine's path."""
        out = self.build("--root", self.path(self.RUN))
        self.assertEqual(out["layers"]["weights"], 1)

    def test_an_unreadable_root_is_a_refusal_naming_the_third_fact(self):
        rc, out, err = self.a("build", "--root", self.path("no_such_box"))
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")
        self.assertIn("unverifiable", json.dumps(out))


class AnArtifactIsADatedReadingNotAFile(AraCase):
    """`references/layout.md` — `census/census_{ts}.json`,
    `evacuations/evac_{ts}/`, `retire/retire_{ts}.json`: everything in MLClaw
    that GOES AND LOOKS is dated and kept. And CLAUDE.md's reason, from
    `/conclude`: 「what was believed at the time is what explains the runs
    launched at the time.」

    Round two building on top of round one destroys the only record of what was
    believed during round one — and that record is what makes round one's runs
    legible. So `build` dates a new one by default; rebuilding in place stays
    available, because `check` reporting drift is a legitimate reason to refresh
    one, but it has to be asked for.
    """

    def test_two_builds_produce_two_artifacts(self):
        a = self.build()["id"]
        b = self.build()["id"]
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.ids()), 2)

    def test_the_earlier_one_still_says_what_it_said(self):
        first = self.build()["id"]
        self.conclude([{"id": "K01", "statement": "one-to-one helps",
                        "status": "refuted", "tier": "T2"}])
        self.build()
        self.assertIn("supported", self.md(first))

    def test_check_reads_the_newest_by_default(self):
        self.build()
        self.conclude([{"id": "K01", "statement": "one-to-one helps",
                        "status": "unverifiable", "tier": "T2"}])
        second = self.build()["id"]
        self.assertEqual(self.check()["id"], second)
        self.assertEqual(self.findings("critical"), [],
                         "the newest artifact was built from the current record")

    def test_an_older_artifact_can_be_checked_by_id(self):
        first = self.build()["id"]
        self.conclude([{"id": "K01", "statement": "one-to-one helps",
                        "status": "unverifiable", "tier": "T2"}])
        self.build()
        rc, out, err = self.a("check", "--no-fail", "--id", first)
        self.assertEqual(rc, 0, f"check broke: {out or err}")
        blob = " ".join(f["detail"] for f in out["findings"])
        self.assertIn("froze `supported`", blob)

    def test_rebuilding_in_place_must_be_asked_for(self):
        first = self.build()["id"]
        self.build("--id", first)
        self.assertEqual(self.ids(), [first])


if __name__ == "__main__":
    unittest.main()


class TheArtifactDoesNotKnowWhatAStageIsCalled(AraCase):
    """CLAUDE.md -> "Never silently": 「Never let a value have two authors.」

    `classify()` says which layer a file belongs to. The copy loop used to say
    it a second time, by naming `knowledge/` -> `logic/` and
    `stages/exploration/` -> `trace/` as literal source directories -- and the
    two authors disagreed in BOTH directions at once, which is the signature of
    this failure rather than an unlucky instance of it:

      · `stages/exploration/config.json` was COUNTED `unclassified` and COPIED
        into `trace/`;
      · a tune session's `chain.md` -- the same kind of record one stage over --
        was counted `trace` and copied nowhere at all.

    An index naming a file the directory beside it does not hold is precisely
    what `AnArtifactWithoutItsInputIsABackup` and
    `test_the_index_never_contradicts_the_directory_beside_it` exist to report,
    and the script was doing it to itself. Every check here is the one-author
    rule applied to the artifact's own layering.
    """

    def setUp(self):
        super().setUp()
        S = os.path.join("proj", "stages")
        self.write_json(os.path.join(S, "training", "config.json"),
                        {"entry_command": "python train.py"})
        self.write_json(os.path.join(S, "training", "provenance.json"),
                        {"source_mode": "swept"})
        self.write(os.path.join(S, "training", "recipe.md"), "# recipe\n")
        self.write_json(os.path.join(S, "exploration", "config.json"),
                        {"corpus": "datasets/b@1"})
        self.write_json(os.path.join(S, "training", "tune_sessions", "s1",
                                     "state.json"), {"session_id": "s1"})
        self.write(os.path.join(S, "training", "tune_sessions", "s1", "chain.md"),
                   "# chain\n")

    def test_a_tune_session_is_in_the_bundle_the_way_an_exploration_is(self):
        """The user's framing, and it is the right one: a tune round's artifact
        should BE a training round's artifact, with exploration recording more.
        Both records are `trace`; only one of them used to arrive."""
        out = self.build()
        for rel in ("trace/stages/training/tune_sessions/s1/chain.md",
                    "trace/stages/training/tune_sessions/s1/state.json"):
            self.assertIn(rel, out["copied"])
            self.assertTrue(os.path.exists(
                self.path("proj", "ara", out["id"], *rel.split("/"))))

    def test_a_stages_declared_config_is_src_not_unclassified(self):
        """`src` is ARA's INPUT layer -- what the numbers were produced FROM.
        A stage's `-init` output is exactly that, and while it was unclassified
        an artifact could be named a BACKUP for want of a file it held."""
        out = self.build()
        for rel in ("src/stages/training/config.json",
                    "src/stages/training/provenance.json",
                    "src/stages/training/recipe.md",
                    "src/stages/exploration/config.json"):
            self.assertIn(rel, out["copied"])

    def test_two_stages_same_named_config_do_not_overwrite_each_other(self):
        base = os.path.join("proj", "ara", self.build()["id"], "src", "stages")
        self.assertEqual(json.loads(self.read(base, "training", "config.json"))
                         ["entry_command"], "python train.py")
        self.assertEqual(json.loads(self.read(base, "exploration", "config.json"))
                         ["corpus"], "datasets/b@1")

    def test_every_path_the_index_names_is_on_disk(self):
        """The whole point: `copied` is the index, the bundle is the directory,
        and they may never disagree."""
        out = self.build()
        aid = self.path("proj", "ara", out["id"])
        for rel in out["copied"]:
            self.assertTrue(os.path.exists(os.path.join(aid, *rel.split("/"))),
                            f"the index names {rel} and the directory has no such file")

    def test_a_record_it_does_not_recognise_is_still_copied_and_named(self):
        """CLAUDE.md: a sweep that keeps only what it recognises loses the file
        nobody thought about, and reports success while doing it."""
        self.write_json(os.path.join("proj", "nobody_thought_about_this.json"), {})
        out = self.build()
        self.assertIn("unclassified/nobody_thought_about_this.json", out["copied"])

    def test_bulk_directories_are_in_by_reference_not_by_copy(self):
        """A run's records are what `--root` already walks. Copying them would
        duplicate every one of them into every dated artifact, forever."""
        out = self.build()
        self.assertFalse([r for r in out["copied"] if "/runs/" in r],
                         "runs/ must be in the artifact by reference")

    def test_an_artifact_is_not_counted_into_the_next_one(self):
        """Two builds of an unchanged project must report the same layers. They
        did not: `ARTIFACT.md` and `ara.json` were counted as two more
        `unclassified` files each time, so the index grew while the round did
        not -- a record describing itself describing itself."""
        first = self.build()
        second = self.build()
        self.assertEqual(first["layers"], second["layers"])
        self.assertFalse([r for r in second["copied"] if r.split("/")[1] == "ara"],
                         "the artifact directory must not be copied into an artifact")
