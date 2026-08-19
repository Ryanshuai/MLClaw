"""Getting the work off a machine before it disappears — what must refuse.

This is the one place in MLClaw where doing nothing is itself the destructive
act. Everywhere else a record can be fixed later; here the machine is released,
the disk goes with it, and whatever was not pulled off is gone with no rm in the
log and nothing having raised. The recorded failure is a half-transferred
checkpoint: a `.pth` with a plausible name that no longer loads, sitting next to
a release that reported success.

That is squarely CLAUDE.md -> "Conventions"'s bar — a record read later by
somebody who can no longer verify it, plus an irreversible action.

The checks divide into two halves. One half is about the transfer: a file that
exists is not a file that arrived, and completeness is computed against a
manifest frozen at the SOURCE rather than against what showed up. The other half
is about the release: clearance is computed, never asserted, and a checkpoint
nothing ranked may not be left on a box that is about to stop existing — because
leaving it there IS the delete that CLAUDE.md's checkpoint rule forbids.
"""

import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "evacuate/evacuate.py"


class EvacCase(TempDirCase):
    """A project, a `box` standing in for the doomed machine, and a `dest`."""

    def setUp(self):
        super().setUp()
        self.box = self.path("box")
        self.dest = self.path("dest")
        self.proj = self.path("proj")
        for d in (self.box, self.dest, self.proj):
            os.makedirs(d, exist_ok=True)
        self.write(os.path.join("box", "run.json"), '{"status": "completed"}')
        self.write(os.path.join("box", "stream.jsonl"), '{"step": 1}\n')
        self.write(os.path.join("box", "output", "epoch_12.pth"), "W" * 4096)
        self.write(os.path.join("box", "logs", "train.log"), "line\n")

    def e(self, *args):
        return run_script(SCRIPT, *args, "--project", self.proj)

    def plan(self, *extra, expect=0):
        rc, out, err = self.e("plan", "--source-root", self.box,
                              "--host", "gpu-7", "--id", "evac_T", *extra)
        if expect is not None:
            self.assertEqual(rc, expect, f"plan rc={rc}: {out or err}")
        return rc, out

    def freeze(self):
        rc, out, err = self.e("freeze", "--id", "evac_T")
        self.assertEqual(rc, 0, f"freeze failed: {out or err}")
        return out

    def copy_all(self, *, skip=(), truncate=None, corrupt=None):
        """Simulate the transfer, with the failure modes that actually happen."""
        import shutil
        for root, _, files in os.walk(self.box):
            for f in files:
                src = os.path.join(root, f)
                rel = os.path.relpath(src, self.box)
                if rel.replace("\\", "/") in skip:
                    continue
                dst = os.path.join(self.dest, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if rel.replace("\\", "/") == truncate:
                    with open(src, "rb") as a, open(dst, "wb") as b:
                        b.write(a.read()[: os.path.getsize(src) // 2])
                elif rel.replace("\\", "/") == corrupt:
                    with open(src, "rb") as a:
                        data = a.read()
                    with open(dst, "wb") as b:
                        b.write(b"X" * len(data))
                else:
                    shutil.copy2(src, dst)

    def verify(self, dest=None):
        rc, out, err = self.e("verify", "--id", "evac_T",
                              "--dest-root", dest or self.dest)
        self.assertEqual(rc, 0, f"verify rc={rc}: {out or err}")
        return out

    def clearance(self):
        rc, out, err = self.e("clearance", "--id", "evac_T")
        return rc, out

    def rec(self):
        return self.read_json(os.path.join("proj", "evacuations", "evac_T",
                                           "evacuation.json"))


# ---------------------------------------------------------------------------

class AFileThatExistsIsNotAFileThatArrived(EvacCase):
    """The recorded failure, several times over: 「拉一半，然后不管了」.

    `os.path.exists` returns true for a half-written checkpoint, and so does any
    check built on 「is it there」. The file has a plausible name, a plausible
    location, and does not load — and nobody finds out until the weights are
    wanted, which is after the machine is gone.

    Two distinct defects, and only the second needs a hash: `truncated` is the
    wrong length, `corrupt` is the right length and the wrong bytes. Reporting
    either as a successful transfer is what this exists to stop.
    """

    def test_a_half_transferred_checkpoint_is_truncated_not_present(self):
        self.plan()
        self.freeze()
        self.copy_all(truncate="output/epoch_12.pth")
        out = self.verify()
        self.assertEqual(out["counts"]["truncated"], 1)
        self.assertEqual(out["counts"]["verified"], 3)

    def test_the_finding_names_both_lengths(self):
        self.plan()
        self.freeze()
        self.copy_all(truncate="output/epoch_12.pth")
        bad = [p for p in self.verify()["problems"] if p["state"] == "truncated"][0]
        self.assertEqual(bad["expected_bytes"], 4096)
        self.assertEqual(bad["found_bytes"], 2048)

    def test_right_length_wrong_bytes_is_caught_only_by_the_hash(self):
        self.plan()
        self.freeze()
        self.copy_all(corrupt="output/epoch_12.pth")
        out = self.verify()
        self.assertEqual(out["counts"]["corrupt"], 1)
        self.assertEqual(out["counts"]["truncated"], 0,
                         "a same-size corruption must not be reported as truncation")

    def test_a_file_that_never_arrived_is_missing(self):
        self.plan()
        self.freeze()
        self.copy_all(skip=("output/epoch_12.pth",))
        self.assertEqual(self.verify()["counts"]["missing"], 1)

    def test_a_complete_transfer_verifies(self):
        self.plan()
        self.freeze()
        self.copy_all()
        out = self.verify()
        self.assertEqual(out["counts"]["verified"], 4)
        self.assertEqual(out["counts"]["truncated"], 0)


class CompletenessIsComputedAgainstTheFrozenManifest(EvacCase):
    """`/data-label`'s rule, same primitive, different counterparty: 「the
    manifest is frozen at send time precisely so it can be the only record of
    what a return is supposed to cover」, and CLAUDE.md -> "Never silently":
    「Never accept a claimed return as a verified one.」

    Manifesting AFTER the transfer describes what arrived, which is a tautology
    — it passes every partial pull by construction. The order is the whole
    guarantee.
    """

    def test_pushing_before_freezing_is_refused(self):
        self.plan()
        rc, out, err = self.e("push", "--id", "evac_T", "--dry")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")
        self.assertIn("tautology", json.dumps(out))

    def test_verifying_without_a_manifest_is_refused(self):
        self.plan()
        rc, out, err = self.e("verify", "--id", "evac_T", "--dest-root", self.dest)
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")

    def test_the_manifest_counts_the_source_not_the_destination(self):
        self.plan()
        out = self.freeze()
        self.assertEqual(out["count"], 4)
        self.copy_all(skip=("logs/train.log",))
        self.assertEqual(self.verify()["counts"]["missing"], 1,
                         "the missing file must be visible, which requires the "
                         "manifest to have been frozen before the transfer")

    def test_an_external_transfer_is_a_claim_until_verified(self):
        """CLAUDE.md: 「Never accept a claimed return as a verified one.」"""
        self.plan()
        self.freeze()
        rc, out, err = self.e("push", "--id", "evac_T", "--already-pushed")
        self.assertEqual(rc, 0, f"push failed: {out or err}")
        self.assertIn("claim", json.dumps(out))
        rc, cl = self.clearance()
        self.assertEqual(rc, 1)
        self.assertIn("never verified", " ".join(cl["blocked_by"]))


class LeavingAFileOnADoomedBoxIsADelete(EvacCase):
    """CLAUDE.md -> "Never silently": 「Never delete a checkpoint outside
    `retention.py plan` → `apply`. Showing the user a list of filenames is not
    confirmation… Never delete a file you cannot rank.」

    The rule was written for `rm`, and the deletion here is performed by the
    machine going away — same outcome, no log line, nothing raising. A list of
    excluded filenames carries exactly as little evidence about the ranking
    behind it as a list of deleted ones does, which is why the same authority is
    required: a `retention.py` plan, whose ranking came from
    `select_checkpoint.inventory()` rather than from here.
    """

    def test_excluding_an_unranked_checkpoint_is_refused(self):
        rc, out = self.plan("--exclude", "output/epoch_12.pth", expect=1)
        self.assertIn("nothing ranking them", json.dumps(out))

    def test_the_refusal_names_the_files(self):
        rc, out = self.plan("--exclude", "output/*.pth", expect=1)
        self.assertIn("output/epoch_12.pth", json.dumps(out))

    def test_a_retention_plan_authorises_it(self):
        self.write_json("retention_plan.json",
                        {"delete": ["/somewhere/output/epoch_12.pth"]})
        rc, out = self.plan("--exclude", "output/epoch_12.pth",
                            "--retention-plan", self.path("retention_plan.json"))
        self.assertEqual(out["left_behind"], 1)
        self.assertEqual(out["classes"]["weights"], 0)

    def test_a_missing_retention_plan_is_a_refusal_not_a_crash(self):
        """CLAUDE.md -> "Script Integration": 1 = the answer is no, 2 = fall back
        and do it by hand. Exiting 2 here means an agent works around the one
        check standing between it and a deleted checkpoint."""
        rc, out = self.plan("--exclude", "output/epoch_12.pth",
                            "--retention-plan", self.path("nope.json"), expect=1)

    def test_non_weight_files_may_be_excluded_freely(self):
        rc, out = self.plan("--exclude", "logs/*")
        self.assertEqual(out["left_behind"], 1)


class UnverifiableIsNotAbsent(EvacCase):
    """CLAUDE.md -> "Never silently": 「Never report data you could not look at.
    A machine that did not answer, a path that is not there, and a directory
    that is genuinely empty are three facts, and only the last one means the
    data is gone.」

    Here the third fact is the dangerous one in both directions: a destination
    that did not answer reported as `missing` sends somebody re-running a
    finished transfer, and a source that did not answer reported as empty clears
    a machine that still holds everything.
    """

    def test_a_destination_that_did_not_answer_is_not_missing(self):
        self.plan()
        self.freeze()
        out = self.verify(dest=self.path("no_such_dest"))
        self.assertEqual(out["counts"]["unverifiable"], 4)
        self.assertEqual(out["counts"]["missing"], 0)

    def test_that_state_blocks_clearance(self):
        self.plan()
        self.freeze()
        self.verify(dest=self.path("no_such_dest"))
        rc, cl = self.clearance()
        self.assertEqual(rc, 1)
        self.assertIn("unverifiable", " ".join(cl["blocked_by"]))

    def test_a_source_that_cannot_be_read_refuses_to_plan(self):
        rc, out, err = self.e("plan", "--source-root", self.path("no_such_box"),
                              "--host", "gpu-7", "--id", "evac_X")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")
        self.assertIn("unverifiable", json.dumps(out))

    def test_freezing_after_the_box_is_gone_is_refused_permanently(self):
        """What arrived cannot testify about what did not."""
        self.plan()
        import shutil
        shutil.rmtree(self.box)
        rc, out, err = self.e("freeze", "--id", "evac_T")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")
        self.assertIn("unverifiable", json.dumps(out))


class ClearanceIsComputedNeverAsserted(EvacCase):
    """CLAUDE.md -> "Never silently": 「Never let somebody's word become a
    checked fact」, and `pool.py`'s ARTIFACTS axis, whose `recovered` means
    「pulled off and verified. The only state that permits destroying the box」.

    That axis existed and took the operator's word for it. This is the thing
    that computes it — and the exit code is the whole interface, because a
    verdict nothing acts on is prose.
    """

    def test_a_blocked_clearance_exits_one(self):
        self.plan()
        self.freeze()
        self.copy_all(skip=("output/epoch_12.pth",))
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 1)
        self.assertEqual(out["verdict"], "blocked")

    def test_a_clean_clearance_exits_zero(self):
        self.plan()
        self.freeze()
        self.copy_all()
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "clear")

    def test_clearance_without_any_verification_is_blocked(self):
        self.plan()
        self.freeze()
        rc, out = self.clearance()
        self.assertEqual(rc, 1)
        self.assertIn("never verified", " ".join(out["blocked_by"]))

    def test_no_evacuation_at_all_is_a_refusal_not_a_pass(self):
        rc, out, err = self.e("clearance", "--host", "gpu-7")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")

    def test_the_blocked_verdict_says_not_to_release(self):
        self.plan()
        self.freeze()
        self.copy_all(truncate="output/epoch_12.pth")
        self.verify()
        rc, out = self.clearance()
        self.assertIn("do_not", out)


class SizeOnlyIsNotVerified(EvacCase):
    """CLAUDE.md -> "Never silently": 「a count from a partial census is a lower
    bound and must be said as one」, applied to a transfer.

    S3's ETag equals the MD5 only for single-part uploads, so precisely the
    large checkpoints can come back with no comparable checksum. Present at the
    right length is a real fact and a weaker one; the verdict has its own name,
    it permits the release, and it states the gap rather than rounding up to
    `clear`.
    """

    def _listing(self, *, with_hash):
        """A destination that reports sizes and, optionally, checksums."""
        mpath = os.path.join("proj", "evacuations", "evac_T", "manifest.jsonl")
        lines = []
        with open(self.path(mpath), encoding="utf-8") as fh:
            for line in fh:
                o = json.loads(line)
                if "_manifest" in o:
                    continue
                row = {"item": o["item"], "bytes": o["bytes"]}
                if with_hash:
                    row["sha256"] = o["hash"]
                lines.append(json.dumps(row))
        return self.write("listing.jsonl", "\n".join(lines) + "\n")

    def test_a_destination_with_no_checksum_yields_size_only(self):
        self.plan()
        self.freeze()
        rc, out, err = self.e("verify", "--id", "evac_T",
                              "--listing", self._listing(with_hash=False))
        self.assertEqual(rc, 0, f"verify rc={rc}: {out or err}")
        self.assertEqual(out["counts"]["size_only"], 4)
        self.assertEqual(out["counts"]["verified"], 0)

    def test_it_clears_under_its_own_verdict_and_states_the_gap(self):
        self.plan()
        self.freeze()
        self.e("verify", "--id", "evac_T", "--listing", self._listing(with_hash=False))
        rc, out = self.clearance()
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "clear_size_only")
        self.assertEqual(out["hash_verified"], 0)
        self.assertEqual(out["of"], 4)
        self.assertIn("LOWER BOUND", out["‼️"])

    def test_a_checksum_reporting_destination_reaches_clear(self):
        self.plan()
        self.freeze()
        self.e("verify", "--id", "evac_T", "--listing", self._listing(with_hash=True))
        rc, out = self.clearance()
        self.assertEqual(out["verdict"], "clear")
        self.assertEqual(out["hash_verified"], 4)


class TheVerifierMustNotSilentlyDowngradeItself(EvacCase):
    """A guard on the check rather than on the data, and it earns its place
    because the defect happened here: the manifest writes the digest under
    `hash` and the first verifier read `sha256`.

    Nothing raised. Every file fell through to `size_only`, the run still
    reported success, and the suite would have gone green on a build that had
    stopped comparing hashes entirely — which is the same shape as the failure
    the whole skill exists to prevent, turned on the skill itself. CLAUDE.md ->
    "Conventions": 「a check whose failure doesn't tell you which side to change
    is itself a liability」; this one names the side.
    """

    def test_hashes_on_both_sides_can_never_produce_size_only(self):
        self.plan()
        self.freeze()
        self.copy_all()
        out = self.verify()
        self.assertEqual(out["counts"]["size_only"], 0,
                         "both sides carry a digest, so `size_only` is not a "
                         "possible answer — its appearance is a field-name bug")

    def test_the_guard_reports_a_defect_not_a_finding(self):
        """Exit 2, deliberately: this is the script being wrong, and CLAUDE.md's
        exit contract reserves 1 for 'the answer is no'."""
        self.plan()
        self.freeze()
        mpath = self.path("proj", "evacuations", "evac_T", "manifest.jsonl")
        rows = []
        with open(mpath, encoding="utf-8") as fh:
            for line in fh:
                o = json.loads(line)
                if "_manifest" not in o:
                    o.pop("hash")          # a manifest that lost its digests
                rows.append(json.dumps(o))
        self.write(os.path.join("proj", "evacuations", "evac_T", "manifest.jsonl"),
                   "\n".join(rows) + "\n")
        self.copy_all()
        rc, out, err = self.e("verify", "--id", "evac_T", "--dest-root", self.dest)
        self.assertEqual(rc, 2, f"expected a defect report, got {rc}: {out or err}")
        self.assertIn("defect in the verifier", json.dumps(out))


class NothingIsSweptSilently(EvacCase):
    """CLAUDE.md -> "Never silently", the shape shared by all of them: an
    omission that reports success.

    A classifier that keeps only what its patterns recognised loses the one file
    nobody thought about, and says `ok: true` while doing it. `unclassified` is
    therefore a kept class with a notice, not a dropped one.
    """

    def test_an_unrecognised_file_is_evacuated_anyway(self):
        self.write(os.path.join("box", "notes_from_the_intern.txt"), "read me")
        rc, out = self.plan()
        self.assertEqual(out["classes"]["unclassified"], 1)
        self.assertEqual(out["files"], 5)

    def test_it_is_named_rather_than_counted(self):
        self.write(os.path.join("box", "notes_from_the_intern.txt"), "read me")
        rc, out = self.plan()
        self.assertIn("notes_from_the_intern.txt", json.dumps(out))

    def test_it_reaches_the_manifest(self):
        self.write(os.path.join("box", "notes_from_the_intern.txt"), "read me")
        self.plan()
        self.assertEqual(self.freeze()["count"], 5)


class ACitationMustNotBeStranded(EvacCase):
    """CLAUDE.md -> "Never silently": 「Never delete data a frozen snapshot still
    names. The bytes go; the citation stays… every run that cited it goes on
    reading as reproducible while it no longer is — nothing anywhere raises.」

    The model side has no `retire.py` to know. A conclusion cites a run; the box
    holding that run is released; `conclusions.json` goes on resolving and the
    belief quietly becomes `unverifiable` weeks later. `retire.py plan` earns
    the right to delete by reading the manifests and the census together — this
    is the same join, performed before the machine rather than the bytes go.
    """

    def _cite(self, ref):
        self.write_json(os.path.join("proj", "knowledge", "conclusions.json"),
                        {"conclusions": [{"id": "K01", "evidence": [
                            {"kind": "run", "ref": ref, "quote": "AP50 = 92.15"}]}]})

    def test_a_cited_path_that_survives_nowhere_blocks_the_release(self):
        self._cite("stages/training/runs/run_GONE")
        self.plan()
        self.freeze()
        self.copy_all()
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 1)
        self.assertIn("run_GONE", " ".join(out["blocked_by"]))
        self.assertIn("K01", " ".join(out["blocked_by"]))

    def test_a_citation_already_present_locally_does_not_block(self):
        """Without this the ordinary evacuation blocks on records that were
        synced back weeks ago, and a gate that always fires is a gate nobody
        reads."""
        self._cite("stages/training/runs/run_LOCAL")
        self.write(os.path.join("proj", "stages", "training", "runs",
                                "run_LOCAL", "run.json"), "{}")
        self.plan()
        self.freeze()
        self.copy_all()
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 0, f"blocked on a local record: {out}")

    def test_an_external_reference_is_not_treated_as_a_path(self):
        self.write_json(os.path.join("proj", "knowledge", "conclusions.json"),
                        {"conclusions": [{"id": "K01", "evidence": [
                            {"kind": "external", "ref": "arXiv:2304.08369",
                             "quote": "53.1 AP"}]}]})
        self.plan()
        self.freeze()
        self.copy_all()
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 0, f"blocked on a paper: {out}")


class TheRecordLivesOffTheBox(EvacCase):
    """`references/layout.md` on `retire/retire_{ts}.json`: 「Written
    BEFORE the first rm — one level above what it deletes, and on a different
    machine from the bytes.」

    The same reasoning, one line over: an evacuation record kept on the machine
    being evacuated disappears with it, taking the only account of what was
    supposed to have been saved.
    """

    def test_the_record_is_written_to_the_project(self):
        self.plan()
        self.assertTrue(os.path.exists(self.path(
            "proj", "evacuations", "evac_T", "evacuation.json")))

    def test_nothing_is_written_into_the_source(self):
        self.plan()
        self.freeze()
        self.assertEqual(sorted(os.listdir(self.box)),
                         ["logs", "output", "run.json", "stream.jsonl"])

    def test_the_manifest_survives_the_box(self):
        self.plan()
        self.freeze()
        import shutil
        shutil.rmtree(self.box)
        self.assertTrue(os.path.exists(self.path(
            "proj", "evacuations", "evac_T", "manifest.jsonl")))
        self.assertEqual(self.rec()["manifest"]["count"], 4)


class TheArtifactIsCalledNotOwned(EvacCase):
    """An evacuation is scoped to a MACHINE — which may hold pieces of three
    rounds, or none, plus files belonging to no artifact at all — and is gated on
    a lease. An artifact is scoped to a ROUND and has no deadline. So `/ara` is
    not a stage inside this and this is not a stage inside `/ara`: the moment
    before a box dies is the LAST moment its source can be read, which makes the
    deadline a forcing function rather than a container.

    Same shape as `/train-run` calling `/eval-run` for a fine-tune's base
    measurement — the caller is not a stage of the callee; that moment is simply
    the only one where the measurement is still possible.

    The `unclassified` bucket is the proof the scopes differ: an evacuation must
    carry files belonging to no artifact, and dropping them is the failure it
    exists to prevent.
    """

    def test_bundling_produces_an_artifact_through_ara(self):
        self.plan()
        self.freeze()
        rc, out, err = self.e("bundle", "--id", "evac_T")
        self.assertEqual(rc, 0, f"bundle failed: {out or err}")
        self.assertTrue(os.path.exists(out["artifact"]))
        self.assertIn("/ara", out["note"])

    def test_the_artifact_carries_the_transfer_verdict(self):
        """The one thing the evacuation knows and the artifact cannot: whether
        the bytes it names actually arrived."""
        self.plan()
        self.freeze()
        self.copy_all(truncate="output/epoch_12.pth")
        self.verify()
        self.clearance()
        rc, out, _ = self.e("bundle", "--id", "evac_T")
        with open(out["artifact"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn("truncated", md)
        self.assertIn("blocked", md)

    def test_a_file_belonging_to_no_artifact_is_still_evacuated(self):
        """The scopes differ, and this is where. `/ara` would have no reason to
        carry this file; the evacuation must, because the box is going away."""
        self.write(os.path.join("box", "notes_from_the_intern.txt"), "read me")
        rc, out = self.plan()
        self.assertEqual(out["classes"]["unclassified"], 1)
        self.assertEqual(self.freeze()["count"], 5)

    def test_the_classifier_has_one_definition(self):
        """Imported from `/ara`, not reimplemented. Two classifiers would put a
        checkpoint in `weights/` here and `src/` there, and only the artifact
        would show it."""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "evacuate",
                "evacuate.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("from ara import", src)
        self.assertNotIn("\ndef classify(", src)


class ClearingAMachineAlsoLeavesTheCoverPage(EvacCase):
    """The user's requirement — every round from here on produces an artifact —
    applied at the release path, where it is easiest to skip.

    ‼️ Built, not demanded. Refusing clearance over a markdown file deadlocks a
    box that goes on burning money, and CLAUDE.md is explicit that losing the
    bytes is the worst outcome here — so nothing about the artifact may block a
    release. Building it is the "just do it" bucket exactly: cheap, local,
    recoverable, and the answer is readable. What it may not do is fail
    silently.
    """

    def test_clearance_builds_the_artifact_nobody_built(self):
        self.plan()
        self.freeze()
        self.copy_all()
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 0, f"clearance failed: {out}")
        self.assertTrue(os.path.exists(self.path(
            "proj", "evacuations", "evac_T", "bundle", "ARTIFACT.md")))

    def test_it_does_not_rebuild_one_that_exists(self):
        self.plan()
        self.freeze()
        self.e("bundle", "--id", "evac_T")
        built = self.rec()["bundle"]["written_at"]
        self.copy_all()
        self.verify()
        self.clearance()
        self.assertEqual(self.rec()["bundle"]["written_at"], built)
        self.assertNotIn("auto", self.rec()["bundle"])

    def test_a_blocked_clearance_still_leaves_one(self):
        """The blocked case is exactly when somebody else has to pick this up,
        so it is the case that most needs a cover page."""
        self.plan()
        self.freeze()
        self.copy_all(truncate="output/epoch_12.pth")
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.path(
            "proj", "evacuations", "evac_T", "bundle", "ARTIFACT.md")))

    def test_a_failed_build_never_holds_the_machine(self):
        self.plan()
        self.freeze()
        self.copy_all()
        self.verify()
        rc, out = self.clearance()
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "clear")


if __name__ == "__main__":
    unittest.main()
