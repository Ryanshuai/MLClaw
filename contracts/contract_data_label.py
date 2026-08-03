"""A claim from outside must not become a fact inside.

`/data-label` is the one place in MLClaw where the loop is closed by somebody else,
so it is the one place where "done" arrives as a sentence rather than as
evidence. Every check here defends the same seam: the point where a vendor's
"it's done" is converted — or is not — into a number MLClaw computed itself.

These are the failure modes worth code, by the bar in CLAUDE.md "Contracts": a
record written now and read later by someone who can no longer verify it. Nobody
can re-derive what was in a batch after it has been through an annotation vendor
and the source directory has moved on. If the manifest, the coverage, or the
drift flag is wrong, there is no second source to catch it — which is exactly why
`receive` computing them correctly is not something to leave to prose.
"""
import json
import os
import re
import unittest

from helpers import REPO_ROOT, TempDirCase, run_script

SCRIPT = "data-label/handoff.py"
OFFSET = re.compile(r"[+-]\d{2}:\d{2}$|Z$")


class HandoffCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        os.makedirs(self.project, exist_ok=True)
        self.src = self.path("src")
        for i in range(1, 6):
            self.write(f"src/images/0000{i}.jpg", f"img{i}\n")
        self.spec = self.write("spec.md", "label every visible box\n")

    def send(self, *extra, expect=0):
        rc, out, err = run_script(
            SCRIPT, "send", "--project", self.project, "--source", self.src,
            "--kind", "annotation", "--to", "vendorA", "--spec", self.spec, *extra)
        self.assertEqual(rc, expect, f"send rc={rc} out={out} err={err}")
        return out

    def ret_dir(self, stems, name="back"):
        for s in stems:
            self.write(f"{name}/{s}.json", "{}\n")
        return self.path(name)

    def receive(self, hid, returned, *extra, expect=0):
        rc, out, err = run_script(SCRIPT, "receive", "--project", self.project,
                                  "--id", hid, "--returned", returned, *extra)
        self.assertEqual(rc, expect, f"receive rc={rc} out={out} err={err}")
        return out

    def close(self, hid, *extra):
        return run_script(SCRIPT, "close", "--project", self.project, "--id", hid, *extra)

    def record(self, hid):
        with open(os.path.join(self.project, "handoffs", hid, "handoff.json")) as f:
            return json.load(f)


class CompletenessIsComputed(HandoffCase):
    """CLAUDE.md -> "Never silently": never accept a claimed return as a verified
    one. The whole skill reduces to this — if `receive` can report a short batch
    as complete, or if `close` will accept one on a claim, nothing else in the
    record layer means anything.
    """

    def test_a_short_return_is_not_complete(self):
        hid = self.send()["handoff_id"]
        out = self.receive(hid, self.ret_dir(["00001", "00002", "00003", "00004"]))
        self.assertFalse(out["complete"])
        self.assertEqual(out["coverage"], 0.8)
        self.assertEqual(out["counts"], {"sent": 5, "matched": 4, "missing": 1,
                                         "unexpected": 0, "ambiguous_keys": 0})

    def test_receive_does_not_accept(self):
        """Computing is not accepting — the plan/apply split from retention.py."""
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["00001", "00002", "00003", "00004", "00005"]))
        self.assertEqual(self.record(hid)["status"], "returned")

    def test_missing_items_are_named_not_only_counted(self):
        """A count tells you a batch is short; only the names let anyone act on
        it, or send a rework round that carries the right deficit."""
        hid = self.send()["handoff_id"]
        out = self.receive(hid, self.ret_dir(["00001", "00002", "00003", "00004"]))
        with open(out["reconciliation"]) as f:
            recon = json.load(f)
        self.assertEqual([m["item"] for m in recon["missing"]], ["images/00005.jpg"])

    def test_work_returned_for_something_never_sent_is_surfaced(self):
        hid = self.send()["handoff_id"]
        out = self.receive(hid, self.ret_dir(["00001", "00002", "00003", "00004",
                                              "00005", "99999"]))
        self.assertEqual(out["counts"]["unexpected"], 1)

    def test_an_ambiguous_match_is_never_resolved_by_picking_one(self):
        """Two sent items collapsing to one match key makes the pairing unknown.
        Silently taking the first would report full coverage for a batch whose
        labels are attached to the wrong images."""
        self.write("src/other/00001.jpg", "dup\n")
        hid = self.send()["handoff_id"]
        out = self.receive(hid, self.ret_dir(["00001", "00002", "00003", "00004", "00005"]))
        self.assertEqual(out["counts"]["ambiguous_keys"], 1)
        self.assertFalse(out["complete"])


class AcceptingPartialIsDeliberate(HandoffCase):
    """CLAUDE.md -> "Never silently": a partial return that closes as plain
    "accepted" becomes a full-coverage artifact in every downstream record. The
    guard is not a confirmation prompt — a prompt carries no evidence that the
    thing being confirmed is right — but a restatement of the measured number.
    """

    def partial(self):
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["00001", "00002", "00003", "00004"]))
        return hid

    def test_bare_accept_is_refused(self):
        hid = self.partial()
        rc, out, _ = self.close(hid, "--accept")
        self.assertEqual(rc, 1)
        self.assertIn("0.8", json.dumps(out), "the refusal must name the measured coverage")
        self.assertEqual(self.record(hid)["status"], "returned")

    def test_accept_is_refused_when_the_restated_coverage_is_wrong(self):
        hid = self.partial()
        rc, _, _ = self.close(hid, "--accept", "--accept-partial", "0.95")
        self.assertEqual(rc, 1)
        self.assertEqual(self.record(hid)["status"], "returned")

    def test_accepting_partial_records_that_it_was_partial(self):
        """`partial: true` is the field that has to survive into every downstream
        description of this data."""
        hid = self.partial()
        rc, _, err = self.close(hid, "--accept", "--accept-partial", "0.8")
        self.assertEqual(rc, 0, err)
        rec = self.record(hid)
        self.assertEqual(rec["status"], "accepted")
        self.assertTrue(rec["accepted"]["partial"])
        self.assertEqual(rec["accepted"]["coverage"], 0.8)

    def test_a_closed_handoff_cannot_be_reconciled_again(self):
        hid = self.partial()
        self.close(hid, "--reject")
        self.receive(hid, self.ret_dir(["00001"], name="back2"), expect=1)


class NotCheckedIsNotClean(HandoffCase):
    """run-mechanics.md -> "Record integrity": extraction failure and absence are
    different facts and must not both become the same value. Source drift is that
    rule one domain over — "we did not look" and "we looked and found nothing"
    cannot both read as a clean pairing, because accepting on the first records
    an unverified pairing as a verified one.
    """

    def test_skipping_the_drift_check_is_recorded_as_unchecked(self):
        hid = self.send()["handoff_id"]
        out = self.receive(hid, self.ret_dir(["0000%d" % i for i in range(1, 6)]),
                           "--skip-drift-check")
        self.assertEqual(out["source_drift"], "not_checked")
        self.assertFalse(self.record(hid)["latest"]["source_drift_checked"])

    def test_unchecked_drift_blocks_accept(self):
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["0000%d" % i for i in range(1, 6)]),
                     "--skip-drift-check")
        rc, _, _ = self.close(hid, "--accept")
        self.assertEqual(rc, 1)
        self.assertEqual(self.record(hid)["status"], "returned")

    def test_a_source_that_changed_after_send_blocks_accept(self):
        """The returned labels describe bytes that are no longer on disk here,
        and the filenames still match, so nothing downstream can detect it."""
        hid = self.send()["handoff_id"]
        self.write("src/images/00002.jpg", "re-exported\n")
        out = self.receive(hid, self.ret_dir(["0000%d" % i for i in range(1, 6)]))
        self.assertEqual(out["source_drift"], 1)
        rc, _, _ = self.close(hid, "--accept")
        self.assertEqual(rc, 1)

    def test_a_clean_full_return_accepts(self):
        """The guards must not be so eager that the normal path needs an override —
        one that always fires trains everybody to pass the flag."""
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["0000%d" % i for i in range(1, 6)]))
        rc, _, err = self.close(hid, "--accept")
        self.assertEqual(rc, 0, err)
        rec = self.record(hid)
        self.assertEqual(rec["status"], "accepted")
        self.assertFalse(rec["accepted"]["partial"])


class TheManifestIsTheOnlyAuthority(HandoffCase):
    """run-mechanics.md -> "Record integrity": a record written now and read later
    by someone who can no longer verify it. There is no external truth source for
    a handoff — `lease.py reap` can ask the cloud API what is running, nothing
    here can ask the vendor what they did. So the manifest frozen at send is the
    single point of failure for everything downstream of it.
    """

    def test_a_handoff_id_identifies_exactly_one_handoff(self):
        """The run_id uniqueness rule, same mechanism: one-second resolution plus
        exist_ok=True lets two sends share a directory and the second write
        destroys the first record."""
        ids = {self.send()["handoff_id"] for _ in range(3)}
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(os.listdir(os.path.join(self.project, "handoffs"))), 3)

    def test_an_explicit_id_collision_is_refused_not_overwritten(self):
        self.send("--id", "handoff_fixed")
        self.send("--id", "handoff_fixed", expect=1)
        rec = self.record("handoff_fixed")
        self.assertEqual(rec["sent"]["count"], 5)

    def test_timestamps_carry_an_explicit_offset(self):
        """Naive local strings from machines in different zones sort wrong and
        look fine — and a handoff's age is the whole basis of the staleness
        report at conversation start."""
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["0000%d" % i for i in range(1, 6)]))
        self.close(hid, "--accept")
        rec = self.record(hid)
        for field in ("created_at", "returned_at", "closed_at"):
            self.assertRegex(rec[field], OFFSET, f"{field} has no UTC offset")

    def test_the_manifest_pins_every_item(self):
        hid = self.send()["handoff_id"]
        path = os.path.join(self.project, "handoffs", hid, "manifest.jsonl")
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        header, items = lines[0]["_manifest"], lines[1:]
        self.assertEqual(header["count"], 5)
        self.assertEqual(len(items), 5)
        self.assertTrue(all(len(i["hash"]) == 64 for i in items), "not sha256")
        self.assertRegex(header["frozen_at"], OFFSET)

    def test_the_spec_is_snapshotted_not_referenced(self):
        """A path to the vendor's guideline is not a record of it — the file gets
        edited for the next batch and the old batch's identity changes with it."""
        hid = self.send("--spec-version", "v3")["handoff_id"]
        rec = self.record(hid)
        self.assertEqual(rec["spec"]["version"], "v3")
        snap = os.path.join(self.project, "handoffs", hid, rec["spec"]["path"])
        self.assertTrue(os.path.isfile(snap))
        self.write("spec.md", "COMPLETELY DIFFERENT RULES\n")
        with open(snap) as f:
            self.assertIn("label every visible box", f.read())

    def test_a_missing_spec_must_be_deliberate(self):
        rc, _, _ = run_script(SCRIPT, "send", "--project", self.project,
                              "--source", self.src, "--kind", "annotation", "--to", "v")
        self.assertEqual(rc, 1)
        rc, _, err = run_script(SCRIPT, "send", "--project", self.project,
                                "--source", self.src, "--kind", "annotation",
                                "--to", "v", "--no-spec")
        self.assertEqual(rc, 0, err)

    def test_a_rework_round_carries_exactly_the_deficit(self):
        """Not the whole batch again, and not a hand-typed subset — the items the
        stored reconciliation said never came back."""
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["00001", "00002", "00003"]))
        self.close(hid, "--reject")
        out = self.send("--rework", hid)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["round"], 2)
        path = os.path.join(self.project, "handoffs", out["handoff_id"], "manifest.jsonl")
        with open(path) as f:
            items = [json.loads(l)["item"] for l in f if '"item"' in l]
        self.assertEqual(sorted(items), ["images/00004.jpg", "images/00005.jpg"])


class RefusalIsNotFailure(HandoffCase):
    """CLAUDE.md -> "Script Integration": exit 1 = the script worked and the
    answer is no; exit 2 = the script broke, fall back and do it by hand.

    This is the check that makes the fallback rule safe for this script. Every
    guard above is enforced by an exit 1. If one of them exited 2, an agent
    following the fallback rule would conclude the script was broken, redo the
    work manually, and in doing so route around the guard — turning a safety
    check into a speed bump. The distinction is only load-bearing while the
    codes stay separated.
    """

    def test_guards_refuse_with_1(self):
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["00001"]))
        for args in (("--accept",),
                     ("--accept", "--accept-partial", "0.99")):
            rc, _, _ = self.close(hid, *args)
            self.assertEqual(rc, 1, f"{args} should refuse (1), not break (2)")

    def test_close_before_receive_refuses_with_1(self):
        rc, _, _ = self.close(self.send()["handoff_id"], "--accept")
        self.assertEqual(rc, 1)

    def test_a_real_breakage_exits_2(self):
        rc, _, _ = run_script(SCRIPT, "receive", "--project", self.project,
                              "--id", "handoff_nope", "--returned", self.src)
        self.assertEqual(rc, 2)

    def test_every_refusal_says_what_it_is_protecting(self):
        """A refusal an agent cannot explain to the user is a refusal it will
        argue past. Each carries `why` and, where one exists, `fix`."""
        hid = self.send()["handoff_id"]
        self.receive(hid, self.ret_dir(["00001"]))
        _, out, _ = self.close(hid, "--accept")
        self.assertIn("why", out)
        self.assertIn("fix", out)


class TemplateMatchesWhatIsWritten(unittest.TestCase):
    """CLAUDE.md -> "Script Integration": if the script fails, the skill does the
    same work by hand. That promise is only true while the template describes the
    record the script actually writes — a manual fallback reproduces the template,
    and a record missing `latest` or `status` is one `status` cannot read.
    """

    def test_template_parses_and_covers_the_written_fields(self):
        path = os.path.join(REPO_ROOT, "lifecycle", "data-label", "handoff.json")
        with open(path, encoding="utf-8") as f:
            tmpl = json.load(f)
        keys = {k for k in tmpl if not k.startswith("_comment")}
        required = {"handoff_id", "project", "stage", "dataset", "kind", "status", "round",
                    "rework_of", "to", "channel", "channel_ref",
                    "description", "spec", "sent", "due_at", "latest", "rounds",
                    "accepted", "lineage", "created_at", "returned_at",
                    "closed_at", "outcome"}
        self.assertEqual(required - keys, set(), "template is missing fields the script writes")


if __name__ == "__main__":
    unittest.main()
