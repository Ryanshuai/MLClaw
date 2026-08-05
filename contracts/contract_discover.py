"""On a handover, "I could not look" is the answer you get for weeks.

`/discover` runs when nobody can tell you what data exists, which means it
runs when access has not arrived yet — credentials come after responsibility
does. So the majority state of its findings, early on, is `unreachable`, and the
single thing that would make the skill actively harmful is spelling that `gone`.
Somebody would spend a week chasing data that is fine, and the one dataset that
really did vanish would be indistinguishable in the noise.

That is CLAUDE.md "Never report data you could not look at", in the domain where
it bites hardest: a census at least knows which machines it asked, while a
discovery sweep does not know what it does not know.

The checks below are grouped by what would go wrong if the code drifted: a
failure to look reported as a finding, a lead recorded without the evidence that
makes it interpretable later, and a report that reads as an inventory.
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

from helpers import TempDirCase, load_script, run_script

SCRIPT = "discover/discover.py"


def path_shape_of(path):
    """The shape reader, in-process: it is a pure function of a string and
    routing it through the CLI would need a lead per case."""
    return load_script(SCRIPT).path_shape(path)


class DiscoverCase(TempDirCase):
    def setUp(self):
        super().setUp()
        # A workspace, so resources.json sits one level above the project the
        # way it really does.
        self.project = self.path("ws", "proj")
        os.makedirs(self.project, exist_ok=True)
        self.write_json("ws/proj/project.json", {"name": "proj"})

    def resources(self, **blocks):
        self.write_json("ws/resources.json", {
            "aws": {"access_key_id": "", "secret_access_key": "", "region": "",
                    "s3_bucket": ""},
            "servers": {}, "local": {"base_paths": []},
            "outsourcing": {}, **blocks})

    def record(self, path, *extra, on=None, st="doc", ev="a wiki page"):
        args = ["record", "--project", self.project, "--path", path,
                "--source-type", st, "--evidence", ev]
        if on:
            args += ["--on", on]
        return run_script(SCRIPT, *args, *extra)

    def probe(self, *extra):
        return run_script(SCRIPT, "probe", "--project", self.project, *extra)

    def report(self):
        rc, out, err = run_script(SCRIPT, "report", "--project", self.project, "--json")
        self.assertEqual(rc, 0, err)
        return out

    def table(self):
        rc, out, err = run_script(SCRIPT, "report", "--project", self.project)
        self.assertEqual(rc, 0, err)
        return out

    def leads(self):
        return self.read_json("ws/proj/discovery/leads.json")["leads"]


class CouldNotLookIsNeverGone(DiscoverCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at.

    Four statuses exist so that two specific pairs never collapse. `claim` vs
    `verified` is the /ask-human split. `gone` vs `unreachable` is this one, and
    it is the pair that decides whether the skill is useful or actively
    misleading during the weeks when access is still arriving.
    """

    def test_a_local_path_that_is_not_there_is_gone(self):
        """The one place `gone` is a real reading: we could look, and it is not
        there."""
        self.record(self.path("nope", "highway_2024"))
        rc, out, _ = self.probe()
        self.assertEqual(rc, 1, "a `gone` finding is a verdict, exit 1")
        self.assertEqual(out["counts"]["gone"], 1)
        self.assertEqual(self.leads()[0]["status"], "gone")

    def test_a_directory_that_cannot_be_read_is_unreachable_not_gone(self):
        """Permission-denied is the most common way a sweep manufactures a false
        `gone`: the path exists, the data may be fine, and the only thing that
        happened is that we were not allowed to look."""
        d = self.path("locked")
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o000)
        self.addCleanup(os.chmod, d, 0o755)
        if os.access(d, os.R_OK):
            self.skipTest("running as a user that ignores directory permissions")
        self.record(d)
        rc, out, _ = self.probe()
        self.assertEqual(rc, 0, "not being allowed to look is not a finding")
        self.assertEqual(self.leads()[0]["status"], "unreachable")
        self.assertEqual(out["counts"]["gone"], 0)

    def test_an_s3_lead_with_no_credentials_is_unreachable(self):
        """The handover case exactly: the bucket name came off a wiki page and
        there is no key yet. `absent` here would send somebody hunting."""
        self.resources()
        self.record("s3://some-bucket/highway_2024/")
        rc, _out, _ = self.probe()
        self.assertEqual(rc, 0)
        self.assertEqual(self.leads()[0]["status"], "unreachable")

    def test_an_unknown_server_is_unreachable_not_gone(self):
        self.resources()
        self.record("/mnt/nas/highway", on="server:ghost")
        self.probe()
        lead = self.leads()[0]
        self.assertEqual(lead["status"], "unreachable")
        self.assertIn("ghost", lead["probes"][-1]["detail"])

    def test_an_unprobed_lead_is_a_claim_and_not_a_finding(self):
        """Recording is not checking. A lead sitting at `claim` has to be
        visibly outstanding, or a wiki page's assertions read as results."""
        self.record("s3://b/x/")
        self.assertEqual(self.leads()[0]["status"], "claim")
        r = self.report()
        self.assertEqual(len(r["not_checked"]["unprobed_leads"]), 1)
        self.assertEqual(r["verified"], [])


class ALeadCarriesTheEvidenceOrItIsARumour(DiscoverCase):
    """CLAUDE.md -> "Never silently": never let somebody's word become a checked
    fact. Here the word arrives as a path in a document, and the question a
    reader has six months later is which paths came from code that ran and which
    came from a wiki page written from memory — because that is what decides
    which `gone` is worth escalating.
    """

    def test_evidence_is_required(self):
        rc, _out, err = run_script(SCRIPT, "record", "--project", self.project,
                                   "--path", "/x", "--source-type", "doc")
        self.assertNotEqual(rc, 0, "a lead with no evidence must not be recordable")

    def test_the_source_type_is_recorded_and_survives_probing(self):
        self.record("/x", st="code", ev="train.py:44 DATA_ROOT")
        self.probe()
        lead = self.leads()[0]
        self.assertEqual(lead["source_type"], "code")
        self.assertEqual(lead["evidence"], "train.py:44 DATA_ROOT")

    def test_a_probe_appends_rather_than_replacing(self):
        """Access arrives late, so the interesting record is the sequence: this
        was unreachable in July and verified in August. Overwriting would lose
        the only evidence that the gap existed."""
        self.record(self.path("nope"))
        self.probe()
        self.probe("--all")
        self.assertEqual(len(self.leads()[0]["probes"]), 2)

    def test_a_duplicate_path_is_refused_not_silently_added(self):
        """Two leads for one place get probed twice and reported twice, turning
        one location into two findings and inflating every count."""
        self.record("/x")
        rc, out, _ = self.record("/x")
        self.assertEqual(rc, 1)
        self.assertIn("already recorded", out["refused"])
        self.assertEqual(len(self.leads()), 1)
        rc, _, _ = self.record("/x", "--again")
        self.assertEqual(rc, 0, "--again is the deliberate path")


class ASweepNeverReadsAsAnInventory(DiscoverCase):
    """CLAUDE.md -> "Never silently": a count from a partial reading is a lower
    bound and must be said as one.

    A discovery report is worse than a partial census in one respect: a census
    knows which machines it failed to ask, while a sweep cannot know about data
    nobody wrote down. Handing somebody a findings list on day three of a
    handover, with the caveats underneath, is how a missing dataset is
    discovered in month four.
    """

    def test_the_report_states_it_is_not_exhaustive(self):
        self.record("/x")
        r = self.report()
        self.assertFalse(r["exhaustive"])
        self.assertTrue(r["why_not_exhaustive"])

    def test_an_unmeasured_size_prints_as_a_dash_and_never_as_zero(self):
        """The table's `—` carries three different facts — nobody looked, we
        could not look, or the walk ran out of budget — and the row's status says
        which. A `0` would collapse all three into "there is no data here",
        which is the one conclusion none of them supports.
        """
        self.record("s3://b/x/")                       # recorded, never probed
        t = self.table()
        self.assertIn("—", t)
        self.assertNotIn(" 0 B", t)
        self.assertIn("LOWER BOUND", t,
                      "a total with anything unmeasured must say it is a floor")

    def test_a_measured_total_is_flagged_as_a_lower_bound_in_json_too(self):
        os.makedirs(self.path("real"), exist_ok=True)
        with open(self.path("real", "a.bin"), "wb") as fh:
            fh.write(b"x" * 2048)
        self.record(self.path("real"))
        self.record("s3://b/never-probed/", "--again")
        self.probe("--id", "lead_0001")
        m = self.report()["measured"]
        self.assertEqual(m["bytes"], 2048)
        self.assertEqual(m["files"], 1)
        self.assertTrue(m["is_lower_bound"])

    def test_gaps_are_keyed_above_the_findings(self):
        """Order, not merely presence: `not_checked` precedes the result keys so
        that reading top to bottom reaches the caveat first."""
        self.record("/x")
        keys = list(self.report())
        self.assertLess(keys.index("not_checked"), keys.index("verified"))
        self.assertLess(keys.index("not_checked"), keys.index("counts"))

    def test_every_source_says_whether_it_is_usable_and_why_not(self):
        """`sources` exists to make "what did you not check" answerable at all.
        Without a list of what could have been checked, a findings list is
        unfalsifiable — it looks identical after one probe and after twenty.

        Asserted structurally rather than on a count, because whether *this*
        machine has AWS credentials is not a property of MLClaw.
        """
        self.resources(servers={"box": {"host": "10.0.0.5"}, "nohost": {}})
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["sources"])
        for s in out["sources"]:
            self.assertIn("usable", s, f"{s} does not say whether it is usable")
            if not s["usable"]:
                self.assertTrue(s.get("why"), f"{s['source']} is blocked with no reason")
        blocked = [s for s in out["sources"] if not s["usable"]]
        self.assertEqual(out["counts"]["blocked"], len(blocked))
        # The two blockages yield DIFFERENT statuses downstream, so one sentence
        # cannot cover both. An access-blocked source produces `unreachable`; a
        # doc or a person produces `claim` no matter how long you wait.
        if any(s["blocked_by"] in ("credential", "registration") for s in blocked):
            self.assertIn("UNREACHABLE", out["note"],
                          "a blocked source must be said to yield UNREACHABLE, "
                          "not nothing")
        if any(s["blocked_by"] == "human" for s in blocked):
            self.assertIn("`claim`", out["note"],
                          "a doc or a person never yields `unreachable` — saying "
                          "so would put them in the waiting-for-access queue")

    def test_a_missing_resources_file_is_a_blocked_source_not_an_error(self):
        """Day one of a handover: nothing is registered yet. That is the normal
        state this skill runs in, so it must report rather than fail."""
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        self.assertTrue(any(s["source"] == "resources.json" and not s["usable"]
                            for s in out["sources"]))

    def test_a_vendor_is_never_probed(self):
        """An outsourcing party can hold the only copy of a batch, and no
        listing will ever reveal it. They are a source of answers, so the report
        routes to /ask-human instead of pretending a probe could settle it."""
        self.resources(outsourcing={"vendor-a": {"name": "Vendor A"}})
        _rc, out, _ = run_script(SCRIPT, "sources", "--project", self.project)
        row = next(s for s in out["sources"] if s["source"] == "outsourcing:vendor-a")
        self.assertFalse(row["usable"])
        self.assertEqual(row["fix"], "/ask-human")

    def test_it_never_declares_a_dataset(self):
        """`identity.unit_glob`'s depth decides every unit id and a wrong depth
        yields zero units with no error. A guessed contract is worse than none,
        so discovery hands over a verified path and stops."""
        os.makedirs(self.path("real", "260731", "s000"), exist_ok=True)
        self.record(self.path("real"))
        self.probe()
        self.assertEqual(self.leads()[0]["status"], "verified")
        self.assertFalse(os.path.isdir(os.path.join(self.project, "datasets")),
                         "discovery must not create a dataset declaration")
        self.assertIn("/data-check", self.report()["next"])


if __name__ == "__main__":
    unittest.main()


class AProbeNeverGuessesWhichProbe(DiscoverCase):
    """CLAUDE.md -> "Contracts": a record written now and read later by somebody
    who can no longer verify it. A lead carries a status and a `last_probed`, so a
    reader six months on takes "probed, unreachable" at face value — there is no
    way to tell from the file that nothing actually looked.

    The dispatch used to be `local` / `s3` / else -> server, with
    `on.split(":")[-1]` as the server key. Every unhandled value therefore probed
    as a server: `tracking:wandb` came back `unreachable: no server 'wandb' in
    resources.json`, which is the right verdict reached by nonsense, carrying a
    fix instruction that tells the reader to register a machine that does not
    exist. A typo did the same. This is the "wrong answer that reads as right"
    class, and it is worse than a refusal precisely because the lead looks done.

    Found by pointing the skill at a real handover: three S3 buckets classified
    correctly off a live AccessDenied, and the W&B locator quietly misfiled.
    """

    def test_an_unknown_on_is_refused_at_record_time(self):
        rc, out, _ = self.record("/x", on="tracker:wandb", st="code", ev="typo")
        self.assertEqual(rc, 2, "validated at write time so a value nothing can "
                                "dispatch never enters the record")
        self.assertIn("allowed", json.dumps(out))

    def test_a_tracking_lead_is_never_probed_as_a_server(self):
        """`tracking:wandb` has a probe now; what must never come back is a
        verdict about a SERVER named wandb, which is what the old dispatch
        produced. Whatever the environment yields here — no key, no package, a
        real answer — it must not mention resources.json."""
        self.record("ent/proj", on="tracking:wandb", st="code",
                    ev="train.py:8 wandb.init")
        rc, out, _ = self.probe("--all")
        self.assertEqual(rc, 0)
        got = out["probed"][0]
        self.assertNotIn("resources.json", got["detail"])
        self.assertNotEqual(got["status"], "gone",
                            "a missing key or package is never absence")

    def test_an_unknown_tracking_backend_says_it_is_not_absence(self):
        self.record("ent/proj", on="tracking:nosuchthing", st="code", ev="t.py:2")
        rc, out, _ = self.probe("--all")
        got = out["probed"][0]
        self.assertEqual(got["status"], "unreachable")
        self.assertIn("absent", got["detail"],
                      "an unbuilt probe must say it is not absence")
        self.assertNotIn("resources.json", got["detail"])

    def test_no_credential_says_it_is_a_credential_lead_not_an_absence(self):
        self.record("ent/proj", on="tracking:clearml", st="code", ev="t.py:2")
        rc, out, _ = self.probe("--all")
        got = out["probed"][0]
        self.assertEqual(got["status"], "unreachable")
        self.assertIn("credential", got["detail"])
        self.assertIn("not an absence", got["detail"])

    def test_a_credential_with_no_listing_adapter_states_both_halves(self):
        """Reporting only "not counted" throws away the actionable half — that a
        credential WAS found. Reporting only the credential implies somebody
        counted the runs. Both, or the reader draws one of two wrong conclusions.

        Driven against a SYNTHETIC backend, and the reason is a lesson this test
        already learned twice: it used to name mlflow, then neptune, and each time
        the named backend acquired a listing the check went stale while staying
        green about the wrong thing. No shipped backend is in this state now — every
        service one has a REST listing and wandb has its SDK — but the branch is
        what a NEW backend lands in, so it has to keep working. Naming a real one
        again would just schedule the same drift.
        """
        mod = load_script(SCRIPT)
        mod.TRACKING["futurething"] = {"family": "service", "pkg": "futurething",
                                       "env": ("FUTURETHING_TOKEN",),
                                       "files": (), "listing": None}
        os.environ["FUTURETHING_TOKEN"] = "not-a-real-token"
        self.addCleanup(os.environ.pop, "FUTURETHING_TOKEN", None)
        status, detail, _s, size = mod.probe_tracking(
            "tracking:futurething", "ent/proj", 5.0)
        self.assertEqual(status, "unreachable")
        self.assertIn("credential found", detail)
        self.assertIn("not counted", detail)
        self.assertIn("absent", detail)
        self.assertEqual(size["blocker"], "tracking:futurething:no_listing_adapter")

    def test_a_doc_lead_keeps_its_claim_instead_of_becoming_unreachable(self):
        """`unreachable` means "we could not look". For a doc or a person
        somebody CAN look — it is just not a probe. Overwriting the status would
        also move the lead out of the set /ask-human works on."""
        self.record("Handover Index", on="doc", st="doc", ev="confluence")
        rc, out, _ = self.probe("--all")
        self.assertEqual(rc, 0)
        self.assertEqual(out["probed"][0]["status"], "claim")
        self.assertIn("ask-human", out["probed"][0]["detail"])
        self.assertEqual(self.leads()[0]["status"], "claim")


class ALeadAndACandidateAreOneFact(DiscoverCase):
    """CLAUDE.md -> "Contracts": a record written now and read later. A lead in
    `leads.json` and a candidate in a stage's `input.json` describe ONE fact — is
    this data here — and were kept in step by hand with nothing joining them. So a
    lead probed `unreachable` could sit behind a candidate still marked `ok`, and
    /train-run would launch against a path nobody could reach.

    The load-bearing row is `claim`: a candidate is never `ok` on a lead that only
    a document or a person asserts. That is "never let somebody's word become a
    checked fact", applied to whether data exists.
    """

    def stage_input(self, candidates, items=None):
        self.write_json("ws/proj/stages/training/input.json", {
            "items": items if items is not None else {"train_images": {"type": "d"}},
            "candidates": {"items": candidates}})

    def reconcile(self):
        return run_script(SCRIPT, "reconcile", "--project", self.project,
                          "--stage", "training")

    def test_ok_on_an_unchecked_claim_is_drift(self):
        self.record("/somewhere", on="local", st="doc", ev="a wiki page")
        self.stage_input({"train_images": [
            {"location": "local", "path": "/somewhere", "match": "ok",
             "lead_id": "lead_0001"}]})
        rc, out, _ = self.reconcile()
        self.assertEqual(rc, 1, "drift is an answer, not a crash")
        self.assertEqual(len(out["drift"]), 1)
        self.assertIn("never `ok` on a lead nothing has checked",
                      out["drift"][0]["fix"])

    def test_ok_on_an_unreachable_lead_is_drift(self):
        self.record("s3://nope/x", on="s3", st="code", ev="train.py:1")
        self.probe("--all")                      # no creds in the fixture
        self.stage_input({"train_images": [
            {"location": "s3", "path": "s3://nope/x", "match": "ok",
             "lead_id": "lead_0001"}]})
        rc, out, _ = self.reconcile()
        self.assertEqual(rc, 1)
        self.assertEqual(out["drift"][0]["lead_status"], "unreachable")
        self.assertEqual(out["drift"][0]["allowed"], ["unreachable"])

    def test_a_code_default_entry_without_a_lead_is_not_drift(self):
        """`code_default` and `downloadable` are derived from the code, not from a
        sweep. Flagging them would make the check fire on every correct config,
        which is how a check gets ignored."""
        self.stage_input({"train_images": [
            {"location": "code_default", "path": "/data/x", "match": "absent"}]})
        rc, out, _ = self.reconcile()
        self.assertEqual(out["drift"], [])
        self.assertEqual(len(out["unlinked_candidates"]), 1)

    def test_a_declared_item_nothing_is_searching_for_is_a_coverage_gap(self):
        """The item-driven half. An item with no candidates looks identical to an
        item whose candidates all failed, so a need nothing is looking for is
        invisible without this."""
        self.stage_input({}, items={"val_labels": {"type": "coco_json"}})
        rc, out, _ = self.reconcile()
        self.assertEqual(rc, 1)
        self.assertEqual([g["item"] for g in out["coverage_gaps"]], ["val_labels"])

    def test_gaps_are_reported_before_findings(self):
        self.stage_input({}, items={"val_labels": {"type": "coco_json"}})
        _, out, _ = self.reconcile()
        keys = list(out.keys())
        self.assertLess(keys.index("coverage_gaps"), keys.index("drift"),
                        "same ordering rule as report and a partial census")

    def test_a_consistent_stage_passes(self):
        self.record("/there", on="local", st="code", ev="train.py:1")
        os.makedirs(self.path("there"), exist_ok=True)
        self.stage_input({"train_images": [
            {"location": "local", "path": self.path("there"), "match": "absent",
             "lead_id": "lead_0001"}]})
        rc, out, _ = self.reconcile()
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["consistent"])


class AccessCanExpireNotJustGoStale(DiscoverCase):
    """`--recheck-days` covers staleness in one direction: the world may have
    changed, go look again. It cannot say the other one — *this source stops being
    resolvable on a known date*. A departing account's tracking history, a wiki
    page in a personal space, a key pending rotation: all read identically to a
    lead that can be resolved next month, and the standing advice for
    `unreachable` ("come back when access arrives") is exactly wrong when access
    is about to be revoked instead of granted.

    `/ask-human`'s ask.json has `valid_until` for the same reason. This is its
    counterpart for a lead.
    """

    def expiring(self, path, when, *, on="local"):
        return self.record(path, "--access-expires-at", when, on=on, st="doc",
                           ev="a handover page")

    def test_an_expiry_inside_the_window_is_reported_before_any_count(self):
        soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        self.expiring("/x", soon)
        out = self.report()
        ids = [l["lead_id"] for l in out["not_checked"]["access_expiring_soon"]]
        self.assertEqual(ids, ["lead_0001"])

    def test_expired_and_still_unresolved_is_its_own_finding(self):
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.expiring("/x", past)
        out = self.report()
        got = out["not_checked"]["access_expired_and_unresolved"]
        self.assertEqual([l["lead_id"] for l in got], ["lead_0001"])
        self.assertIn("nothing else observes", got[0]["why_it_matters"])

    def test_an_expiry_that_was_resolved_in_time_is_not_a_finding(self):
        """The point is the unresolved ones. A lead that was verified before its
        access lapsed is a success, and listing it would bury the real cases."""
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        os.makedirs(self.path("there"), exist_ok=True)
        self.expiring(self.path("there"), past)
        self.probe("--all")
        self.assertEqual(self.leads()[0]["status"], "verified")
        out = self.report()
        self.assertEqual(out["not_checked"]["access_expired_and_unresolved"], [])

    def test_no_expiry_is_the_normal_case_and_reports_nothing(self):
        self.record("/x", on="local", st="doc", ev="e")
        out = self.report()
        self.assertEqual(out["not_checked"]["access_expiring_soon"], [])
        self.assertEqual(out["not_checked"]["access_expired_and_unresolved"], [])


class TheCredentialFreeFamilyIsProbedWithoutAKey(DiscoverCase):
    """train-init Step 0 marks "Local tracking leftovers" (`wandb/`, `mlruns/`,
    `lightning_logs/`) a default-yes because they need no credentials. That makes
    them the only tracking history probeable on day one of a handover, before any
    access has arrived — so they must not be lumped in with the service backends
    and left `unreachable` for weeks.

    `gone` is a real reading here, on the same bar probe_s3 sets for an empty
    prefix: the directory listed, and it holds no runs.
    """

    def tb(self, rel, n):
        for i in range(n):
            d = self.path(rel, f"run-{i}")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"events.out.tfevents.{i}.h.0"), "w").close()
        return self.path(rel)

    def test_tfevents_on_disk_verify_with_no_credential_present(self):
        root = self.tb("logs", 3)
        self.record(root, on="tracking:tensorboard", st="code", ev="train.py:9")
        rc, out, _ = self.probe("--all")
        got = out["probed"][0]
        self.assertEqual(got["status"], "verified")
        self.assertIn("3 run dir", got["detail"])
        self.assertIn("no credential needed", got["detail"])

    def test_verified_says_it_is_about_the_record_not_the_numbers(self):
        """`verified` on a tracking lead and `verified` in origin.confidence are
        two words with opposite bars. The one that means "somebody re-ran eval and
        it reproduced" is only reachable through /repro."""
        self.record(self.tb("logs", 1), on="tracking:tensorboard", st="code", ev="e")
        _, out, _ = self.probe("--all")
        self.assertIn("RECORD", out["probed"][0]["detail"])

    def test_a_directory_with_no_runs_is_gone_not_unreachable(self):
        empty = self.path("empty")
        os.makedirs(empty, exist_ok=True)
        self.record(empty, on="tracking:tensorboard", st="code", ev="e")
        rc, out, _ = self.probe("--all")
        self.assertEqual(out["probed"][0]["status"], "gone")
        self.assertEqual(rc, 1, "a gone lead is an answer, and exit 1 says so")

    def test_a_missing_directory_is_gone_and_names_the_machine_not_a_key(self):
        self.record(self.path("nope"), on="tracking:mlruns", st="doc", ev="e")
        _, out, _ = self.probe("--all")
        self.assertEqual(out["probed"][0]["status"], "gone")
        self.assertNotIn("credential", out["probed"][0]["detail"])


class TheAccessWorklistGroupsOnAssertedBlockers(DiscoverCase):
    """CLAUDE.md -> "Contracts". The worklist is the one output somebody can act
    on without reading a lead: which key to get, and what it unblocks. It grouped
    on the probe's prose, and `err[-300:]` cut those strings mid-token at an
    offset that depended on the path length — so ONE AccessDenied across three
    buckets produced three separate rows, which is the opposite of the point.
    """

    def test_one_blocker_across_several_leads_is_one_row(self):
        for i in range(3):
            self.record(f"ent/proj{i}", on="tracking:neptune", st="code", ev="e")
        self.probe("--all")
        rows = self.report()["access_worklist"]
        self.assertEqual(len(rows), 1, f"one missing token, one row: {rows}")
        self.assertEqual(rows[0]["blocks"], 3)
        self.assertEqual(len(rows[0]["leads"]), 3)

    def test_rows_are_ordered_by_how_much_they_unblock(self):
        for i in range(2):
            self.record(f"ent/n{i}", on="tracking:neptune", st="code", ev="e")
        self.record("ent/c", on="tracking:comet", st="code", ev="e")
        self.probe("--all")
        rows = self.report()["access_worklist"]
        self.assertGreaterEqual(rows[0]["blocks"], rows[-1]["blocks"])

    def test_a_blocker_key_is_stable_and_not_the_prose(self):
        self.record("ent/p", on="tracking:neptune", st="code", ev="e")
        self.probe("--all")
        row = self.report()["access_worklist"][0]
        self.assertEqual(row["blocker"], "tracking:neptune:no_credential")
        self.assertIsNotNone(row["example"], "the prose stays as the example")


class TheMlflowListingActuallyRuns(DiscoverCase):
    """CLAUDE.md -> "Contracts". Every other tracking service listing is staged but
    unexercised, because reaching it needs a vendor package no environment here
    has — and an unexercised probe is a promise, not a capability.

    MLflow is the exception and the reason is worth keeping: it has a documented
    REST surface, so the listing is urllib-only. That makes it runnable on the bare
    interpreter a handover starts with, and testable against a stub — which is what
    these checks are. Prefer `rest` over `pkg` when a backend offers both.

    What must hold: a real count when the server answers, `gone` only when the
    server itself says the experiment is not there, and `unreachable` when the host
    does not answer — never the two swapped.
    """

    EXPS = {"experiments": [{"experiment_id": "1", "name": "yolo26-kontoor"},
                            {"experiment_id": "2", "name": "yolo26-face"}]}

    def serve(self, exps=None, runs=8, runs_code=200):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        body_exps = self.EXPS if exps is None else exps
        body_runs = {"runs": [{"info": {"run_id": f"r{i:08d}", "status": "FINISHED"}}
                              for i in range(runs)]}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                b = _json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.endswith("/experiments/search"):
                    self._send(body_exps)
                elif self.path.endswith("/runs/search"):
                    self._send(body_runs, runs_code)
                else:
                    self._send({}, 404)

            def do_GET(self):
                self._send({}, 404)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def setUp(self):
        super().setUp()
        self.mod = load_script(SCRIPT)

    def probe_mlflow(self, loc, uri=None):
        if uri is not None:
            os.environ["MLFLOW_TRACKING_URI"] = uri
            self.addCleanup(os.environ.pop, "MLFLOW_TRACKING_URI", None)
        return self.mod.probe_tracking("tracking:mlflow", loc, 5.0)

    def test_a_reachable_server_is_verified_with_a_real_count(self):
        base = self.serve(runs=37)
        status, detail, _s, _z = self.probe_mlflow(base, uri=base)
        self.assertEqual(status, "verified")
        self.assertIn("2 experiment(s)", detail)
        self.assertIn("37 run(s)", detail)

    def test_verified_says_it_is_about_the_record_not_the_numbers(self):
        base = self.serve()
        _st, detail, _s, _z = self.probe_mlflow(base, uri=base)
        self.assertIn("RECORD", detail)
        self.assertIn("claim until /repro", detail)

    def test_an_experiment_name_the_server_does_not_list_is_gone(self):
        base = self.serve()
        status, detail, _s, _z = self.probe_mlflow("no-such-experiment", uri=base)
        self.assertEqual(status, "gone", "the server itself listed and it is absent")
        self.assertIn("none is named", detail)

    def test_a_host_that_does_not_answer_is_unreachable_never_gone(self):
        status, detail, _s, size = self.probe_mlflow("exp", uri="http://127.0.0.1:1")
        self.assertEqual(status, "unreachable")
        self.assertEqual(size["blocker"], "tracking:mlflow:unreachable_host")

    def test_auth_failure_is_unreachable_and_says_so(self):
        base = self.serve(exps={"error_code": "PERMISSION_DENIED"})
        # the stub answers 200 with a body carrying no experiments; the real
        # 401/403 path is covered by the code's explicit branch. What must not
        # happen is an auth-shaped answer becoming a count.
        status, _d, _s, _z = self.probe_mlflow(base, uri=base)
        self.assertIn(status, ("gone", "unreachable"))
        self.assertNotEqual(status, "verified")

    def test_a_file_store_is_routed_to_the_disk_family_not_guessed_at(self):
        """`file:./mlruns` is the on-disk family. Treating it as a service would
        report `unreachable` for something sitting right there on the disk."""
        status, detail, _s, size = self.probe_mlflow("exp", uri="file:./mlruns")
        self.assertEqual(status, "unreachable")
        self.assertIn("tracking:mlruns", detail)
        self.assertEqual(size["blocker"], "tracking:mlflow:no_http_uri")

    def test_experiments_answer_but_runs_fail_is_verified_and_says_not_counted(self):
        """The server is real, so `verified` is right; the count is not, so it must
        say the runs were not counted rather than reporting zero."""
        base = self.serve(runs=0, runs_code=500)
        status, detail, _s, _z = self.probe_mlflow(base, uri=base)
        self.assertEqual(status, "verified")
        self.assertIn("NOT COUNTED", detail)


class TheOtherRestListingsRun(DiscoverCase):
    """CLAUDE.md -> "Contracts", and `searches.md` -> the per-`on` probe table.

    Three backends used to stop at "credential found, no listing adapter". They now
    have urllib listings, for the reason stated in `REST_LISTINGS`: a REST listing
    needs no package AND is testable against a stub, so the probe can be exercised
    without owning an account on any of the three.

    That is also the limit of what these checks prove, and it is worth saying
    plainly: they verify the PARSING AND THE DISPATCH, not the endpoints. mlflow and
    wandb are the two that have answered a real server. So the property that has to
    hold here is not "the URL is right" — it is that being wrong about the URL, the
    auth scheme or the body shape can only ever produce `unreachable`. A false
    `gone` would tell somebody their history was deleted because this file guessed a
    path wrong, and that is the one failure that cannot be walked back: they stop
    looking.
    """

    def setUp(self):
        super().setUp()
        self.mod = load_script(SCRIPT)

    def env(self, **kw):
        for key, val in kw.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
            self.addCleanup(os.environ.pop, key, None)

    def serve(self, routes, code=200):
        """routes: {path_without_query: body_obj}. Anything unmatched -> 404.

        `code` applies to matched routes, so one dict serves the happy path and the
        auth-failure path without a second stub.
        """
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _route(self):
                if self.headers.get("Content-Length"):
                    self.rfile.read(int(self.headers["Content-Length"]))
                path = self.path.split("?")[0]
                if path in routes:
                    body, status = routes[path], code
                else:
                    body, status = {"error": "no such route"}, 404
                b = _json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            do_GET = do_POST = _route

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    # ---- fixtures, one per backend -------------------------------------------

    CLEARML = {
        "/auth.login": {"data": {"token": "tok"}},
        "/projects.get_all": {"data": {"projects": [{"id": "p1", "name": "unload"},
                                                    {"id": "p2", "name": "jenga"}]}},
        "/tasks.get_all": {"data": {"tasks": [
            {"id": f"t{i}", "name": f"train-{i}", "status": "completed"}
            for i in range(6)]}},
    }
    NEPTUNE = {
        "/api/backend/v1/authorization/oauth-token": {"accessToken": "acc"},
        "/api/backend/v1/projects": [{"organizationName": "anyware", "name": "seg"},
                                     {"organizationName": "anyware", "name": "box"}],
    }
    COMET = {
        "/api/rest/v2/workspaces": {"workspaceNames": ["anyware"]},
        "/api/rest/v2/projects": {"projects": [{"projectId": "pid1",
                                                "projectName": "seg"}]},
        "/api/rest/v2/experiments": {"experiments": [
            {"experimentKey": f"key{i:08d}"} for i in range(4)]},
    }

    def neptune_token(self, api):
        import base64
        return base64.b64encode(
            json.dumps({"api_url": api, "api_key": "k"}).encode()).decode()

    def clearml(self, base, path=None):
        self.env(CLEARML_API_HOST=base, CLEARML_API_ACCESS_KEY="ak",
                 CLEARML_API_SECRET_KEY="sk")
        return self.mod.probe_tracking("tracking:clearml",
                                       base if path is None else path, 5.0)

    def neptune(self, api, path=""):
        self.env(NEPTUNE_API_TOKEN=self.neptune_token(api))
        return self.mod.probe_tracking("tracking:neptune", path, 5.0)

    def comet(self, base, path="anyware/seg"):
        self.env(COMET_API_KEY="ck", COMET_URL_OVERRIDE=base)
        return self.mod.probe_tracking("tracking:comet", path, 5.0)

    # ---- the table and the dispatch agree ------------------------------------

    def test_every_rest_listing_named_in_the_table_has_an_adapter(self):
        """A `listing` value with no entry in REST_LISTINGS falls through to the
        wandb branch and reports "no listing adapter" for a backend that has one —
        green, wrong, and invisible."""
        named = {s["listing"] for s in self.mod.TRACKING.values()
                 if s.get("listing", "").endswith("_rest")}
        self.assertEqual(named - set(self.mod.REST_LISTINGS), set())

    def test_wandb_is_the_only_service_backend_left_on_the_package_path(self):
        """Not a style rule: a package listing cannot be exercised without an
        account, so this is the count of probes nobody can test. It was 4."""
        pkg_only = sorted(k for k, s in self.mod.TRACKING.items()
                          if s["family"] == "service"
                          and s.get("listing") not in self.mod.REST_LISTINGS)
        self.assertEqual(pkg_only, ["wandb"],
                         "if a backend must use its SDK, say why in TRACKING — "
                         "prefer REST whenever the backend publishes one")

    # ---- happy paths ---------------------------------------------------------

    def test_clearml_logs_in_then_counts_projects_and_tasks(self):
        status, detail, sample, _z = self.clearml(self.serve(self.CLEARML))
        self.assertEqual(status, "verified")
        self.assertIn("2 project(s)", detail)
        self.assertIn("6 task(s)", detail)
        self.assertIn("RECORD", detail)
        self.assertIn("claim until /repro", detail)
        self.assertTrue(sample)

    def test_neptune_decodes_the_token_for_the_host_then_lists(self):
        base = self.serve(self.NEPTUNE)
        status, detail, sample, _z = self.neptune(base)
        self.assertEqual(status, "verified")
        self.assertIn("2 project(s)", detail)
        self.assertIn("anyware/seg", sample)
        self.assertIn("RUNS ARE NOT COUNTED", detail,
                      "Neptune's listing gives no per-project run count; saying "
                      "nothing would read as zero")

    def test_comet_counts_experiments_under_a_named_project(self):
        status, detail, _s, _z = self.comet(self.serve(self.COMET))
        self.assertEqual(status, "verified")
        self.assertIn("1 project(s)", detail)
        self.assertIn("4 experiment(s)", detail)

    def test_comet_with_no_workspace_asks_the_key_what_it_can_see(self):
        """The handover shape: a key arrives and nobody wrote down the workspace.
        Refusing without one would fail the case discovery exists for."""
        status, detail, sample, _z = self.comet(self.serve(self.COMET), path="")
        self.assertEqual(status, "verified")
        self.assertIn("1 workspace(s)", detail)
        self.assertEqual(sample, ["anyware"])

    # ---- gone is only ever the server's own answer ---------------------------

    def test_a_name_the_server_does_not_list_is_gone(self):
        for label, call, empty in (
            ("clearml", lambda b: self.clearml(b, path="no-such-project"), None),
            ("neptune", lambda b: self.neptune(b, path="no-such-project"), None),
            ("comet", lambda b: self.comet(b, path="anyware/no-such"), None),
        ):
            with self.subTest(backend=label):
                fixture = {"clearml": self.CLEARML, "neptune": self.NEPTUNE,
                           "comet": self.COMET}[label]
                status, detail, _s, _z = call(self.serve(fixture))
                self.assertEqual(status, "gone",
                                 f"{label}: the server itself listed and the name "
                                 f"is not in it")
                self.assertIn("none is", detail.replace("holds no", "none is"))

    def test_an_empty_listing_is_gone_because_the_server_answered(self):
        cases = {
            "clearml": (dict(self.CLEARML, **{"/projects.get_all":
                                              {"data": {"projects": []}}}),
                        lambda b: self.clearml(b)),
            "neptune": (dict(self.NEPTUNE, **{"/api/backend/v1/projects": []}),
                        lambda b: self.neptune(b)),
            "comet": (dict(self.COMET, **{"/api/rest/v2/workspaces":
                                          {"workspaceNames": []}}),
                      lambda b: self.comet(b, path="")),
        }
        for label, (routes, call) in cases.items():
            with self.subTest(backend=label):
                status, _d, _s, size = call(self.serve(routes))
                self.assertEqual(status, "gone")
                self.assertIsNone(size["blocker"],
                                  "a real reading has no blocker — nobody is "
                                  "waiting on access")

    # ---- the load-bearing property ------------------------------------------

    def test_a_two_hundred_with_the_wrong_shape_is_unreachable_never_gone(self):
        """The whole reason these three are shippable while unexercised against a
        real server. If an endpoint moved or the body shape changed, the answer must
        be "I could not read it", not "it is not there"."""
        cases = {
            "clearml": (dict(self.CLEARML,
                             **{"/projects.get_all": {"unexpected": "shape"}}),
                        lambda b: self.clearml(b)),
            "neptune": (dict(self.NEPTUNE,
                             **{"/api/backend/v1/projects": {"nope": 1}}),
                        lambda b: self.neptune(b)),
            "comet": (dict(self.COMET,
                           **{"/api/rest/v2/projects": {"nope": 1}}),
                      lambda b: self.comet(b)),
        }
        for label, (routes, call) in cases.items():
            with self.subTest(backend=label):
                status, detail, _s, size = call(self.serve(routes))
                self.assertEqual(status, "unreachable",
                                 f"{label}: a body this code cannot parse says "
                                 f"nothing about whether the runs exist")
                self.assertNotEqual(status, "gone")
                self.assertIn("NOTHING WAS COUNTED", detail,
                              "silence here reads as zero")
                self.assertTrue(size["blocker"], "somebody has to fix this")

    def test_a_missing_endpoint_is_unreachable_not_gone(self):
        """A 404 on the LISTING is this code being wrong about a path. Only a
        successful listing that omits the name may say `gone` — the distinction the
        mlflow class draws for experiments, held for all three."""
        for label, call in (("clearml", lambda b: self.clearml(b)),
                            ("neptune", lambda b: self.neptune(b)),
                            ("comet", lambda b: self.comet(b))):
            with self.subTest(backend=label):
                status, _d, _s, _z = call(self.serve({}, code=404))
                self.assertEqual(status, "unreachable")

    def test_a_host_that_does_not_answer_is_unreachable(self):
        dead = "http://127.0.0.1:1"
        for label, call in (("clearml", lambda: self.clearml(dead)),
                            ("neptune", lambda: self.neptune(dead)),
                            ("comet", lambda: self.comet(dead))):
            with self.subTest(backend=label):
                status, _d, _s, size = call()
                self.assertEqual(status, "unreachable")
                self.assertEqual(size["blocker"],
                                 f"tracking:{label}:unreachable_host")

    # ---- auth is named as auth ----------------------------------------------

    def test_auth_failure_names_the_auth_stage_not_the_listing(self):
        """"the key is wrong" and "the key is fine, the endpoint moved" go to
        different people. Collapsing them sends somebody to request access they
        already have."""
        cases = {
            "clearml": (dict(self.CLEARML, **{"/auth.login": {"meta": {"result": 401}}}),
                        lambda b: self.clearml(b), "login_200"),
            "neptune": (dict(self.NEPTUNE,
                             **{"/api/backend/v1/authorization/oauth-token": {}}),
                        lambda b: self.neptune(b), "oauth_200"),
        }
        for label, (routes, call, blocker) in cases.items():
            with self.subTest(backend=label):
                status, detail, _s, size = call(self.serve(routes))
                self.assertEqual(status, "unreachable")
                self.assertEqual(size["blocker"], f"tracking:{label}:{blocker}")
                self.assertIn("authentication is the blocker", detail)

    def test_comet_401_is_the_key_not_absence(self):
        status, detail, _s, size = self.comet(self.serve(self.COMET, code=401))
        self.assertEqual(status, "unreachable")
        self.assertEqual(size["blocker"], "tracking:comet:http_401")
        self.assertIn("not absence", detail)

    def test_an_undecodable_neptune_token_does_not_implicate_the_project(self):
        """The token carries the host, so a mangled one — a shell ate the quotes —
        means nothing can even be addressed. Reporting that as absence would blame
        the server for a local typo."""
        self.env(NEPTUNE_API_TOKEN="not-base64-json!!")
        status, detail, _s, size = self.mod.probe_tracking("tracking:neptune", "", 5.0)
        self.assertEqual(status, "unreachable")
        self.assertEqual(size["blocker"], "tracking:neptune:undecodable_token")
        self.assertIn("project is not implicated", detail)

    def test_clearml_half_a_key_pair_says_which_half(self):
        """ClearML needs both halves and a config file often carries only one.
        "no credential" would be wrong and would send the reader to the wrong fix."""
        self.env(CLEARML_API_HOST="https://api.clear.ml",
                 CLEARML_API_ACCESS_KEY="ak", CLEARML_API_SECRET_KEY=None)
        status, detail, _s, size = self.mod.probe_tracking("tracking:clearml", "", 5.0)
        self.assertEqual(status, "unreachable")
        self.assertEqual(size["blocker"], "tracking:clearml:partial_credential")
        self.assertIn("secret_key no", detail)


class SomethingWatchesTheExpiryDate(DiscoverCase):
    """CLAUDE.md -> "On Conversation Start", step 4.

    `--access-expires-at` and the two report sections it feeds were the fix for a
    specific loss: a key rotates, an account is deactivated, and the history behind
    it stops being reachable on a date somebody already knew. But a computed section
    nobody opens is exactly the failure the field was added to prevent — `leads.json`
    turning into the wiki page it replaced. The field only pays off if something
    re-reads it on its own, and the only thing that runs unprompted is the
    conversation-start pass.

    So this check joins the two halves: the report must emit the sections, and
    CLAUDE.md must tell the session to read them. Either one alone is inert.
    """

    def report_keys(self):
        return set(self.report()["not_checked"])

    def test_the_report_emits_both_expiry_sections(self):
        self.record("s3://b/x/")
        keys = self.report_keys()
        for want in ("access_expiring_soon", "access_expired_and_unresolved"):
            self.assertIn(want, keys)

    def test_the_conversation_start_pass_is_told_to_read_them(self):
        from helpers import REPO_ROOT
        with open(os.path.join(REPO_ROOT, "CLAUDE.md")) as fh:
            text = fh.read()
        block = text[text.index("On Conversation Start"):]
        block = block[:block.index("\n## ")] if "\n## " in block else block
        for want in ("access_expiring_soon", "access_expired_and_unresolved"):
            self.assertIn(want, block,
                          f"the report computes {want} and nothing reads it — "
                          f"either wire it into the conversation-start pass or "
                          f"delete the section, because an unread deadline is "
                          f"worse than no deadline field at all")
        self.assertIn("deadline", block,
                      "the reason this entry outranks the others has to survive "
                      "in the text, or it gets reordered by whoever edits next")

    def test_an_expired_unresolved_lead_is_separated_from_one_merely_pending(self):
        """The two are the same JSON shape and completely different facts: one is a
        todo, the other is a loss that already happened and that no probe will ever
        report, because `unreachable` never becomes `gone` on its own."""
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        self.record("s3://b/lapsed/", "--access-expires-at", past)
        self.record("s3://b/rotating/", "--access-expires-at", soon)
        self.record("s3://b/no-deadline/")
        r = self.report()["not_checked"]
        self.assertEqual([l["path"] for l in r["access_expired_and_unresolved"]],
                         ["s3://b/lapsed/"])
        self.assertEqual([l["path"] for l in r["access_expiring_soon"]],
                         ["s3://b/rotating/"])
        self.assertEqual(len(r["unprobed_leads"]), 3,
                         "all three are still unprobed; the expiry sections are a "
                         "second axis over the same leads, not a partition")


class SourcesListsWhatCouldBeSweptNotJustWhatIsRegistered(DiscoverCase):
    """`searches.md` -> "Sources, ranked by what a mention is worth", and CLAUDE.md
    -> "Never report data you could not look at".

    `sources` is the falsifiability check on the whole skill: a findings list is
    unreadable without a list of what could have been looked at. It used to enumerate
    only what `resources.json` registered, and the failure that produces is specific
    and backwards — on day one of a handover, with nothing registered, it reported ONE
    blocked source. The project's git history and its on-disk tracking leftovers were
    sitting right there, needing no credential, and the verb whose job is "what did
    you not check" said there was nothing to check.

    That is worse than silence: it reads as an argument for waiting for access, in
    exactly the weeks when the credential-free sources are all anybody has.
    """

    def sources(self):
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        return out

    def names(self, out):
        return [s["source"] for s in out["sources"]]

    def test_the_credential_free_families_appear_with_nothing_registered(self):
        """No resources.json, no keys — the day-one state. The four families that
        need no access must still be listed, because they are the entire sweep that
        is possible today."""
        os.makedirs(self.path("ws", "proj", "stages", "training", "code"))
        got = self.names(self.sources())
        self.assertIn("code:training", got)
        self.assertIn("git_history:training", got)
        self.assertIn("tracking_disk", got)
        self.assertIn("doc", got)
        self.assertGreater(len(got), 4,
                           "this used to be a single row saying resources.json is "
                           "missing")

    def test_the_note_names_where_to_start_without_a_credential(self):
        os.makedirs(self.path("ws", "proj", "stages", "training", "code"))
        out = self.sources()
        self.assertIn("no credential at all", out["note"])
        self.assertIn("code:training", out["note"])

    def test_a_wiki_page_and_an_expired_key_are_not_the_same_blockage(self):
        """"Blocked" alone routes them to one queue. A key is a request to a person
        who can grant it; a doc is a person who has to go and read it, and no
        credential ever unblocks it. Different queue, different week."""
        out = self.sources()
        by = {s["source"]: s["blocked_by"] for s in out["sources"] if not s["usable"]}
        self.assertEqual(by.get("doc"), "human")
        self.assertEqual(by.get("person"), "human")
        self.assertEqual(by.get("resources.json"), "registration")
        self.assertGreaterEqual(out["blocked_by"].get("human", 0), 2)

    def test_every_service_backend_gets_its_own_row(self):
        """"Which tracking backends can I list right now" is the takeover question,
        and one aggregate row cannot answer it — the answer is per-key."""
        got = self.names(self.sources())
        for backend in ("wandb", "mlflow", "clearml", "neptune", "comet"):
            self.assertIn(f"tracking:{backend}", got)

    def test_a_blocked_row_says_which_env_vars_were_checked(self):
        """A bare "no credential" produces a lead nobody can clear. The names of the
        variables checked are the actionable half."""
        row = next(s for s in self.sources()["sources"]
                   if s["source"] == "tracking:comet")
        if row["usable"]:
            self.skipTest("this machine has a Comet credential")
        self.assertIn("COMET_API_KEY", row["why"])

    def test_a_non_git_code_tree_says_what_is_lost_rather_than_vanishing(self):
        """Silence would read as "there is no history to mine", which is true of the
        tree and false of the data."""
        os.makedirs(self.path("ws", "proj", "stages", "training", "code"))
        row = next(s for s in self.sources()["sources"]
                   if s["source"] == "git_history:training")
        self.assertFalse(row["usable"])
        self.assertIn("not a git tree", row["why"])
        self.assertEqual(row["blocked_by"], "absent")

    def test_every_row_says_its_kind(self):
        """mine / probe / ask. A place to grep and a person to ask are both
        "sources" and nothing else about them is alike."""
        for s in self.sources()["sources"]:
            self.assertIn(s["kind"], ("mine", "probe", "ask"), s)


class TheProbeUsesTheCredentialTheRegistryDeclares(DiscoverCase):
    """CLAUDE.md -> "Skills & Dependencies" (`/resources` is the registry every run
    skill reads through `${}`), and "Never silently": never report data you could
    not look at.

    Found by running the skill against a real handover, which is the only way it
    would have been found: `probe_server` reads `resources.json -> servers`, but
    `probe_s3` shelled out to a bare `aws s3 ls` and inherited whatever the CLI
    resolved ambiently. On the machine that surfaced it those were two DIFFERENT
    IAM users — the registry held a key that could list the buckets, the ambient
    default could not — so the sweep reported `unreachable` over 12 GB of readable
    training data and put "go get an S3 key" at the top of the access worklist for
    a key that was already registered one directory up.

    It never became a false `gone`, which is exactly why it survived: the answer
    stayed inside the safe band while being wrong about the world. And `cmd_sources`
    made it invisible from the other side — it DID read `aws.access_key_id`, so the
    checklist called s3 usable on the strength of a credential nothing then used.

    So two things must hold, and the second is what keeps them from drifting apart
    again: the probe resolves the registry first, and the checklist reports the same
    credential the probe will use.
    """

    def fake_aws(self, mode="ok"):
        """A stub `aws` on PATH that records which credential reached it.

        The point of the fixture is the recording. Asserting on status alone would
        pass for the buggy version too — it also returned a plausible answer.
        """
        bindir = self.path("bin")
        os.makedirs(bindir, exist_ok=True)
        seen = os.path.join(bindir, "seen.txt")
        body = {
            # exit 1 with the --summarize sentinel and no stderr: a real empty read
            "empty": 'echo "Total Objects: 0"; echo "   Total Size: 0"; exit 1',
            # exit 1 with nothing at all: this code cannot tell what happened
            "silent": 'exit 1',
            "denied": ('echo "aws: [ERROR]: An error occurred (AccessDenied) when '
                       'calling the ListObjectsV2 operation" >&2; exit 254'),
            "ok": ('echo "2026-06-22 10:52:43  647391178 a.tar.gz"; '
                   'echo "Total Objects: 1"; echo "   Total Size: 647391178"'),
        }[mode]
        with open(os.path.join(bindir, "aws"), "w") as fh:
            fh.write("#!/bin/sh\n"
                     f'printf "%s\\n" "${{AWS_ACCESS_KEY_ID:-<none>}}" >> {seen}\n'
                     f'printf "profile=%s\\n" "${{AWS_PROFILE:-<none>}}" >> {seen}\n'
                     f"{body}\n")
        os.chmod(os.path.join(bindir, "aws"), 0o755)
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = bindir + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        return seen

    def credential_seen(self, seen):
        with open(seen) as fh:
            return [l.strip() for l in fh if l.strip()]

    def registered(self, **extra):
        self.resources(aws={"access_key_id": "AKIAREGISTERED", "region": "us-west-2",
                            "secret_access_key": "shhh", "s3_bucket": "b", **extra})

    def test_the_registered_key_is_used_not_the_ambient_one(self):
        seen = self.fake_aws("ok")
        self.registered()
        os.environ["AWS_ACCESS_KEY_ID"] = "AKIAAMBIENT"
        self.addCleanup(os.environ.pop, "AWS_ACCESS_KEY_ID", None)
        self.record("s3://b/x/")
        self.probe()
        self.assertIn("AKIAREGISTERED", self.credential_seen(seen),
                      "the probe used a credential the registry did not declare")
        self.assertNotIn("AKIAAMBIENT", self.credential_seen(seen))

    def test_an_ambient_profile_cannot_override_the_registered_pair(self):
        """A leftover AWS_PROFILE silently wins over an explicit key pair, which
        reintroduces the whole bug one environment variable at a time."""
        seen = self.fake_aws("ok")
        self.registered()
        os.environ["AWS_PROFILE"] = "some-other-account"
        self.addCleanup(os.environ.pop, "AWS_PROFILE", None)
        self.record("s3://b/x/")
        self.probe()
        self.assertIn("profile=<none>", self.credential_seen(seen))

    def test_with_no_registered_key_the_ambient_one_is_still_used(self):
        """Nothing registered is the normal day-one state; falling back is right.
        What must not happen is falling back SILENTLY when a key was declared."""
        seen = self.fake_aws("ok")
        self.resources()
        os.environ["AWS_ACCESS_KEY_ID"] = "AKIAAMBIENT"
        self.addCleanup(os.environ.pop, "AWS_ACCESS_KEY_ID", None)
        self.record("s3://b/x/")
        self.probe()
        self.assertIn("AKIAAMBIENT", self.credential_seen(seen))

    def test_the_detail_names_which_credential_answered(self):
        """Six months on, "AccessDenied" alone does not say whose key was refused —
        and that is the difference between asking for a key and asking for a policy
        change."""
        self.fake_aws("denied")
        self.registered()
        self.record("s3://b/x/")
        self.probe()
        detail = self.leads()[0]["probes"][-1]["detail"]
        self.assertIn("resources.json -> aws.access_key_id", detail)

    def test_a_refused_registered_key_is_a_different_ask_from_no_key(self):
        """The worklist groups on the blocker, so this line decides what somebody
        goes and requests. A registered key that is refused needs a POLICY change
        from the bucket owner; no key needs a key. Sending someone to ask for
        access they already hold is how a worklist stops being believed."""
        self.fake_aws("denied")
        self.registered()
        self.record("s3://b/x/")
        self.probe()
        self.assertEqual(self.leads()[0]["probes"][-1]["blocker"],
                         "s3:denied_with_registered_key")

    def test_no_registered_key_and_denied_asks_for_a_key(self):
        self.fake_aws("denied")
        self.resources()
        self.record("s3://b/y/")
        self.probe()
        self.assertEqual(self.leads()[0]["probes"][-1]["blocker"],
                         "s3:no_usable_credential")

    def test_the_checklist_reports_the_credential_the_probe_will_use(self):
        """The two used to be computed differently, which is what made the drift
        invisible from both sides."""
        self.registered()
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        row = next(s for s in out["sources"] if s["source"] == "s3")
        self.assertTrue(row["usable"])
        self.assertIn("resources.json -> aws.access_key_id", row["credential"])
        self.assertIn("resources.json", row["why"])

    def test_the_recorded_credential_is_a_location_not_material(self):
        """searches.md -> "code": a record holds a credential's NAME and LOCATION,
        never its material. `leads.json` is git-tracked, so a key copied into it is
        a key committed twice."""
        self.fake_aws("denied")
        self.registered()
        self.record("s3://b/x/")
        self.probe()
        blob = json.dumps(self.read_json("ws/proj/discovery/leads.json"))
        self.assertNotIn("AKIAREGISTERED", blob)
        self.assertNotIn("shhh", blob)


class AnEmptyPrefixIsAReadingAndSilenceIsNot(DiscoverCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at, and
    the `on: s3` row
    of `searches.md`'s probe table: `gone` only when the listing genuinely ran and
    returned nothing.

    `aws s3 ls` exits NON-ZERO when it matched nothing, which by exit code alone is
    indistinguishable from failing. That ambiguity is what `--summarize` resolves
    and is most of what it is worth: "Total Objects: 0" is printed only when the
    listing executed. Same both-signals discipline as the ssh probe, inverted —
    there a zero exit needs a sentinel to be believed, here a non-zero exit needs
    one to be forgiven.

    Both directions matter. Without the sentinel branch a prefix that is genuinely
    empty can never be reported, so a document's claim that something is empty can
    never be confirmed. With the branch drawn too wide, any silent failure becomes
    a deletion report.
    """

    def fake_aws(self, mode):
        return TheProbeUsesTheCredentialTheRegistryDeclares.fake_aws(self, mode)

    def test_the_sentinel_with_no_stderr_is_gone(self):
        self.fake_aws("empty")
        self.resources()
        self.record("s3://b/create-datadump/")
        rc, _out, _ = self.probe()
        self.assertEqual(rc, 1, "a `gone` finding is a verdict, exit 1")
        lead = self.leads()[0]
        self.assertEqual(lead["status"], "gone")
        self.assertIn("listed successfully", lead["probes"][-1]["detail"])
        self.assertIsNone(lead["probes"][-1]["blocker"],
                          "a real reading leaves nobody waiting on access")

    def test_a_bare_nonzero_exit_is_unreachable(self):
        """No sentinel, no stderr — this code does not know what happened, and
        "I do not know" must not print as "it was deleted"."""
        self.fake_aws("silent")
        self.resources()
        self.record("s3://b/x/")
        rc, _out, _ = self.probe()
        self.assertEqual(rc, 0, "not knowing is not a finding")
        self.assertEqual(self.leads()[0]["status"], "unreachable")

    def test_a_denial_never_reaches_the_empty_branch(self):
        """AccessDenied writes to stderr, so the sentinel check must be gated on
        stderr being empty — otherwise a refusal on an empty-looking response
        becomes a deletion report."""
        self.fake_aws("denied")
        self.resources()
        self.record("s3://b/x/")
        self.probe()
        self.assertEqual(self.leads()[0]["status"], "unreachable")


class _NotInstalledHere:
    """Stands in for `ultralytics.nn.tasks.SegmentationModel`.

    Module-level so `pickle` can save it by reference; the discover.py subprocess
    cannot import `contract_discover`, so the reader meets a global it has no
    class for — exactly the situation a real checkpoint puts it in.
    """


class TheCheckpointIsASourceNotAFileWithASize(DiscoverCase):
    """`searches.md` -> "Sources, ranked by what a mention is worth", the
    `checkpoint` family. CLAUDE.md -> `/discover`: "Data, weights, somebody's
    recorded results, and the credentials the other probes turned out to need —
    one engine, one lead register".

    The tracking family's own tagline is "a run that trained recorded what it
    read". A modern checkpoint does precisely that, offline, inside the artifact,
    and the family was missing — so every sweep treated a `.pt` as a name and a
    byte count. The cost lands on evaluation specifically: eval's two needs are a
    checkpoint and a val set, the checkpoint is usually the easiest thing to
    obtain because it is the deployed artifact, and `train_args.data` NAMES THE
    VAL SET. Not opening it means the highest-yield search for the stage that most
    needs it was never run.

    Four properties, and the last two are the ones that could do harm:

      - the reader needs no framework (torch is not in a record script's path)
      - a shape it cannot read REFUSES, and never guesses
      - a path a checkpoint names has no host, and must not claim one
      - a metric read out of an artifact is still a `claim`, because it has no
        `scope`
    """

    def ckpt(self, top, name="best.pt"):
        """A real torch-shaped file: a zip whose */data.pkl holds `top`."""
        import pickle
        import zipfile
        path = self.path(name)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("best/data.pkl", pickle.dumps(top))
        return path

    def introspect(self, path, *extra):
        return run_script(SCRIPT, "introspect", "--project", self.project,
                          "--checkpoint", path, *extra)

    def ULTRA(self):
        return {
            "date": "2026-06-21T03:11:37",
            "version": "8.4.40",
            "epoch": -1,
            "git": {"branch": None, "commit": None, "origin": None, "root": "None"},
            "train_args": {"data": "/workspace/ai/src/dataset/kontoor_yolo/data.yaml",
                           "model": "/workspace/ai/src/unload_BL.pt",
                           "epochs": 140, "imgsz": 1024, "seed": 0},
            "train_metrics": {"metrics/mAP50-95(M)": 0.89116},
            "train_results": {"epoch": list(range(1, 141))},
        }

    def test_it_names_the_val_split(self):
        """The load-bearing one. A checkpoint's `train_args.data` is the only
        record of which units its metrics were measured on, so it decides whether
        the number can ever be reproduced."""
        rc, out, err = self.introspect(self.ckpt(self.ULTRA()))
        self.assertEqual(rc, 0, err)
        data = [l for l in out["leads_named"] if l["subject"] == "data"]
        self.assertEqual(len(data), 1, "the val split must be named exactly once")
        self.assertEqual(data[0]["path"],
                         "/workspace/ai/src/dataset/kontoor_yolo/data.yaml")
        self.assertIn("train_args.data", data[0]["evidence"],
                      "evidence must say which field it came from")

    def test_it_names_the_parent_and_its_own_numbers(self):
        rc, out, _ = self.introspect(self.ckpt(self.ULTRA()))
        subjects = sorted(l["subject"] for l in out["leads_named"])
        self.assertEqual(subjects, ["data", "results", "weights"])
        self.assertEqual(out["recorded_by_the_training_process"]["epochs_in_curve"],
                         140)
        self.assertFalse(out["recorded_by_the_training_process"]["resumable"],
                         "epoch == -1 is a stripped checkpoint, not a null")

    def test_a_null_commit_is_reported_as_the_writer_finding_none(self):
        """`/repro` axes.md: "the commit resolves" and "no commit was recorded" are
        different facts. The checkpoint's own git block distinguishes them, and
        dropping it would collapse the code axis into an unexplained null."""
        rc, out, _ = self.introspect(self.ckpt(self.ULTRA()))
        self.assertEqual(rc, 0)
        self.assertIsNone(out["code_axis"]["commit"])
        self.assertIn("branch", out["code_axis"])

    def test_looking_is_not_recording(self):
        rc, out, _ = self.introspect(self.ckpt(self.ULTRA()))
        self.assertEqual(rc, 0)
        self.assertNotIn("recorded", out)
        self.assertIn("NONE recorded", out["note"])
        self.assertFalse(os.path.exists(self.leads_file()),
                         "a look must not create the register")

    def test_recording_writes_them_as_claims_with_the_checkpoint_source(self):
        rc, out, err = self.introspect(self.ckpt(self.ULTRA()), "--record")
        self.assertEqual(rc, 0, err)
        leads = self.leads()
        self.assertEqual(len(leads), 3)
        for l in leads:
            self.assertEqual(l["source_type"], "checkpoint")
            self.assertEqual(l["status"], "claim",
                             "reading a field out of an artifact is evidence, not "
                             "a probe — the lead is still a claim")

    def test_a_metric_from_an_artifact_is_still_a_claim_because_it_has_no_scope(self):
        """CLAUDE.md -> "Never compare metrics across different `mode` or
        non-equivalent `scope`". Nothing in a checkpoint says which units its
        metrics were measured on, so a `verified` here would be a number that reads
        as checked and cannot lawfully be compared to anything."""
        self.introspect(self.ckpt(self.ULTRA()), "--record")
        res = [l for l in self.leads() if l["subject"] == "results"][0]
        self.assertEqual(res["status"], "claim")
        self.assertIn("NO SCOPE", res["what"])

    def test_a_path_a_checkpoint_names_has_no_host_and_does_not_claim_one(self):
        """The two obvious substitutes are both wrong in a way that reads as right:
        `local` asserts this machine (a false `gone` waiting to happen), and a
        made-up `server:UNKNOWN` reports "no server 'UNKNOWN' in resources.json"
        and sends the reader to register a machine that does not exist."""
        self.introspect(self.ckpt(self.ULTRA()), "--record")
        for l in self.leads():
            if l["subject"] in ("data", "weights"):
                self.assertEqual(l["on"], "host_unknown")
                self.assertNotEqual(l["on"], "local")

    def test_probing_a_hostless_path_is_unreachable_and_names_the_real_task(self):
        self.introspect(self.ckpt(self.ULTRA()), "--record")
        self.resources()
        rc, _out, err = self.probe()
        self.assertEqual(rc, 0, err)
        hostless = [l for l in self.leads() if l["on"] == "host_unknown"]
        self.assertTrue(hostless)
        for l in hostless:
            self.assertEqual(l["status"], "unreachable",
                             "never `gone` — nothing was looked at")
            blocker = l["probes"][-1]["blocker"]
            self.assertNotIn("credential", blocker,
                             "the blocker must name identifying the machine, not "
                             "a credential — a key would not have helped")
            # Which of the two it is depends on the path's shape, and the shape
            # is the more useful answer where it applies: for a container path,
            # naming the host still does not resolve it. Both are the same
            # category of ask and neither is a key.
            self.assertIn(blocker, ("host_unidentified",
                                    "container_path_not_host_path"), blocker)

    def test_an_unreadable_shape_refuses_rather_than_guessing(self):
        """Exit 1, not 2. The script worked and the answer is no: the file is
        present and this code cannot read it. Falling back to a hand-guess about
        what trained would destroy the only property that makes this family worth
        anything — that the training process wrote these fields, not a person."""
        junk = self.path("weights.safetensors")
        with open(junk, "wb") as fh:
            fh.write(b"\x00" * 64)
        rc, out, _ = self.introspect(junk)
        self.assertEqual(rc, 1)
        self.assertIn("no reader", json.dumps(out))
        self.assertIn("readers_missing", out)

    def test_a_bare_state_dict_records_nothing_and_says_so(self):
        """A checkpoint whose top level is not a dict of metadata is a real
        reading, distinct from an unreadable file: it IS a torch archive and it
        genuinely carries no training record."""
        rc, out, _ = self.introspect(self.ckpt([1, 2, 3], name="bare.pt"))
        self.assertEqual(rc, 1)
        self.assertIn("state_dict", json.dumps(out))

    def test_the_reader_does_not_need_the_framework_that_wrote_it(self):
        """A record-keeping script must not put torch in its dependency path, or
        the answer becomes "the package is not installed" instead of a finding —
        the same reason `discover.py` prefers REST over vendor SDKs.

        `_NotInstalledHere` pickles by reference as `contract_discover.…`, which
        the discover.py subprocess cannot import — standing in for the
        `ultralytics.nn.tasks.SegmentationModel` a real checkpoint names.
        """
        top = self.ULTRA()
        top["model"] = _NotInstalledHere()
        rc, out, err = self.introspect(self.ckpt(top, name="exotic.pt"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out["leads_named"]), 3,
                         "an unresolvable class must not stop the metadata read")
        self.assertEqual(out["recorded_by_the_training_process"]["framework_version"],
                         "8.4.40")

    def test_sources_ranks_it_first_and_needs_no_credential(self):
        """It needs less than every other family — one local file, no host, no
        key — and what it names was written by the run rather than asserted by a
        person. `sources` must therefore never report it blocked."""
        self.resources()
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        names = [r["source"] for r in out["sources"]]
        self.assertEqual(names[0], "checkpoint",
                         "first in searches.md's ranking, so first in the listing")
        row = out["sources"][0]
        self.assertTrue(row["usable"])
        self.assertIsNone(row["blocked_by"])
        self.assertEqual(row["kind"], "mine",
                         "it produces candidate locations; it does not classify one")

    def leads_file(self):
        return os.path.join(self.project, "discovery", "leads.json")


class AProjectThatIsNotThereIsNotAProjectWithNothingInIt(DiscoverCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at. A
    machine that did not answer, a path that is not there, and a directory that is
    genuinely empty are three facts, and only the last one means the data is gone.

    The same three facts, one level up from the probe. `report` is the verb somebody
    runs to ask "what do we know about this project", and it is the one output a
    reader trusts without re-deriving. Reaching its empty-record branch on a project
    that does not exist answers a question about a typo as though it were a question
    about the data — and the answer, "nothing recorded yet", is indistinguishable
    from a real project nobody has swept.

    Every other verb (`sources`, `record`, `reconcile`) already refuses a missing
    directory. `report` was the one that did not, which is the wrong one to miss:
    the others are things you do, this is the thing you believe.
    """

    def test_a_missing_project_is_refused_not_reported_empty(self):
        rc, out, _err = run_script(SCRIPT, "report", "--json",
                                   "--project", self.path("ws", "no-such-proj"))
        self.assertEqual(rc, 2, "a mistyped path is the script breaking, not a "
                                "verdict about the data")
        self.assertIn("project not found", json.dumps(out))
        self.assertNotIn("nothing recorded yet", json.dumps(out),
                         "the empty-record wording must never describe a project "
                         "that was never there")

    def test_a_real_project_with_no_leads_still_reports_empty(self):
        """The refusal must not swallow the legitimate case it sits in front of:
        a project that exists and genuinely has not been swept."""
        out = self.report()
        self.assertEqual(out["leads"], [])
        self.assertIn("nothing recorded yet", out["note"])


class TheRecordIsKeptNotJustWritten(DiscoverCase):
    """CLAUDE.md -> "Contracts": what earns a check is "a record written now and
    read later by someone who can no longer verify it". SKILL.md -> "The record is
    the handover artifact": leads.json "is the thing you hand the next person
    instead of a Confluence page".

    Writing a file and keeping it are different things, and the gap was invisible
    from every side. `atomic_write_json` makes the record crash-safe on one disk.
    `project-init` runs `git init` and commits once. Nothing in between ever
    committed anything, so every record this tool produces — leads, censuses,
    handoffs, questions — accumulated untracked. `leads.json` therefore survived a
    crash and went nowhere on a clone, a push, or a `git clean`, which is how a
    handover actually happens.

    A handover artifact that does not survive the handover is the exact failure
    this skill was written against, sitting inside the skill.

    Two properties, and the second is the one that could do harm. The record must
    be committable in one step; and that step must touch NOTHING ELSE — a record
    skill running `git add -A` would sweep up whatever the user had in progress
    and commit it under a message about a dataset sweep.
    """

    def git(self, *args):
        import subprocess
        return subprocess.run(["git", "-C", self.project, *args],
                              capture_output=True, text=True)

    def init_repo(self):
        self.git("init", "-q")
        self.git("config", "user.email", "c@example.com")
        self.git("config", "user.name", "c")
        self.git("add", "--", "project.json")
        self.git("commit", "-qm", "init")

    def save(self, *extra):
        return run_script(SCRIPT, "save", "--project", self.project, *extra)

    def test_a_swept_record_can_be_committed_in_one_step(self):
        self.init_repo()
        self.record("s3://b/x/")
        rc, out, err = self.save()
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["committed"])
        self.assertTrue(out["sha"])
        self.assertTrue(self.git("ls-files", "--error-unmatch", "--",
                                 "discovery/leads.json").returncode == 0)

    def test_the_brief_is_saved_with_the_register(self):
        """They are one sweep in two forms. Committing the register without the
        write-up hands somebody a lead list with no reading of it; committing the
        write-up without the register is worse — a document describing a sweep the
        repo does not contain."""
        self.init_repo()
        self.record("s3://b/x/", "--subject", "data")
        run_script(SCRIPT, "brief", "--project", self.project)
        self.save()
        files = sorted(self.git("show", "--name-only", "--format=", "HEAD")
                       .stdout.split())
        self.assertEqual(files, ["discovery/brief.md", "discovery/leads.json"])

    def test_saving_touches_nothing_but_the_record(self):
        """The property that makes this safe to run at all. Both a modified
        tracked file and an untracked one must survive untouched — the first is
        the user's edit, the second is their scratch work, and neither belongs in
        a commit about a discovery sweep."""
        self.init_repo()
        self.write_json("ws/proj/project.json", {"name": "proj", "edited": True})
        with open(self.path("ws", "proj", "WIP.txt"), "w") as fh:
            fh.write("half-written thing\n")
        self.record("s3://b/x/")
        self.save()
        files = self.git("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(files, ["discovery/leads.json"],
                         "the commit reached beyond the record")
        status = self.git("status", "--short").stdout
        self.assertIn("project.json", status, "the user's edit was committed")
        self.assertIn("WIP.txt", status, "the user's scratch file was committed")

    def test_saving_twice_is_safe_and_says_so(self):
        """Idempotence matters because the honest workflow re-runs it: probe,
        save, probe again, save again. A second save must not fail and must not
        make an empty commit."""
        self.init_repo()
        self.record("s3://b/x/")
        self.save()
        before = self.git("rev-list", "--count", "HEAD").stdout.strip()
        rc, out, _ = self.save()
        self.assertEqual(rc, 0)
        self.assertFalse(out["committed"])
        self.assertIn("has not changed", out["why"])
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(),
                         before, "an empty commit was made")

    def test_the_commit_subject_records_the_status_counts(self):
        """`git log -- discovery/` should show access ARRIVING — 9 unreachable in
        July, 6 verified in August. The leads file holds only the current status,
        so the history of the sweep exists nowhere else."""
        self.init_repo()
        self.record(self.path("nope"))
        self.record("s3://b/x/")
        self.probe()
        self.save()
        subject = self.git("log", "-1", "--format=%s").stdout.strip()
        self.assertIn("2 lead(s)", subject)
        self.assertIn("gone", subject)

    def test_a_non_git_tree_is_refused_and_says_what_is_lost(self):
        """Exit 1, not 2: the script worked and the answer is no. And it must say
        what the consequence is, because "not a git work tree" alone reads as a
        technicality rather than as "nothing will carry this to anybody"."""
        self.record("s3://b/x/")
        rc, out, _ = self.save()
        self.assertEqual(rc, 1)
        self.assertFalse(out["committed"])
        self.assertIn("not a git work tree", out["why"])
        self.assertIn("carry it", out["why"])

    def test_saving_before_there_is_anything_to_save_is_refused(self):
        self.init_repo()
        rc, out, _ = self.save()
        self.assertEqual(rc, 1)
        self.assertIn("no sweep to save", out["refused"])

    def test_a_gitignored_record_is_reported_not_silently_skipped(self):
        """`.gitignore` excluding the record would make every save a no-op that
        reported success. Projects legitimately ignore whole directories."""
        self.init_repo()
        with open(self.path("ws", "proj", ".gitignore"), "w") as fh:
            fh.write("discovery/\n")
        self.record("s3://b/x/")
        rc, out, _ = self.save()
        self.assertEqual(rc, 1)
        self.assertFalse(out["committed"])
        self.assertTrue(out["skipped_ignored"])
        self.assertIn(".gitignore", out["why"])

    def test_the_report_says_when_the_record_is_unsaved(self):
        """The gap has to be visible without anybody thinking to look for it —
        an untracked record looks exactly like a tracked one on disk."""
        self.init_repo()
        self.record("s3://b/x/")
        self.assertTrue(self.report()["record_unsaved"])
        self.assertIn("UNSAVED", self.table())
        self.save()
        self.assertFalse(self.report()["record_unsaved"])
        self.assertNotIn("UNSAVED", self.table())


class TheSweepSaysWhichCellsItNeverLookedIn(DiscoverCase):
    """`searches.md` -> "Sources, ranked by what a mention is worth" and "What each
    subject needs found"; CLAUDE.md -> "Never silently": never report data you
    could not look at.

    That file specifies the sweep as a table — source families down one side, four
    subjects across — and nothing computed it, so the record could not answer the
    question a reader of a sweep actually has. Not "what did you find" but **"which
    cells did you never look in"**. A flat findings list reads identically after one
    search and after four, which is the same unfalsifiability `sources` exists to
    fix, one level up: `sources` says what COULD be swept, this says what WAS.

    The record could not express it either, because a lead had no `subject`. That
    axis is the enabling change and it is deliberately not inferred from the path —
    a guessed subject makes the table confidently wrong, which is worse than an
    `unclassified` row.
    """

    def brief(self):
        rc, out, err = run_script(SCRIPT, "brief", "--project", self.project)
        self.assertEqual(rc, 0, err)
        with open(self.path("ws", "proj", "discovery", "brief.md")) as fh:
            return out, fh.read()

    def test_a_lead_records_which_search_it_belongs_to(self):
        self.record("s3://b/x/", "--subject", "weights")
        self.assertEqual(self.leads()[0]["subject"], "weights")

    def test_an_unstated_subject_is_unclassified_never_guessed(self):
        """Inferring `data` from a path that looks like a dataset would put a lead
        in a cell nobody chose, and the table would then be evidence for coverage
        that was never decided."""
        self.record("s3://b/looks-like-a-dataset/")
        self.assertEqual(self.leads()[0]["subject"], "unclassified")
        cov = self.report()["coverage"]
        self.assertEqual(cov["unclassified"], 1)

    def test_the_grid_crosses_source_against_subject(self):
        self.record("s3://b/a/", "--subject", "data", st="doc")
        self.record("s3://b/b/", "--subject", "weights", st="code",
                    ev="train.py:12")
        cells = {(g["source_type"], g["subject"]): g["leads"]
                 for g in self.report()["coverage"]["grid"]}
        self.assertEqual(cells[("doc", "data")], 1)
        self.assertEqual(cells[("code", "weights")], 1)

    def test_an_unswept_source_is_only_reported_when_it_is_reachable(self):
        """Reporting `git_history` as unswept on a tree that is not a git repo is
        noise, and a coverage report that cries wolf gets ignored — which costs
        more than it ever saved."""
        self.record("s3://b/x/", "--subject", "data", st="code", ev="t.py:1")
        unswept = [g["source_type"] for g in
                   self.report()["coverage"]["sources_available_but_unswept"]]
        self.assertNotIn("code", unswept, "no stage has a code/ directory")
        self.assertNotIn("git_history", unswept)
        self.assertIn("doc", unswept, "a wiki is always available and unswept here")

    def test_unswept_sources_come_back_in_rank_order(self):
        """The ranking IS the priority. Nothing from `code` is a different
        sentence from nothing from `doc`, and a list in arbitrary order makes the
        reader re-derive that every time."""
        self.record("s3://b/x/", "--subject", "data")
        gaps = self.report()["coverage"]["sources_available_but_unswept"]
        self.assertEqual([g["rank"] for g in gaps], sorted(g["rank"] for g in gaps))

    def test_the_credentials_search_counts_as_covered_by_the_worklist(self):
        """searches.md says that search "is generated by the other searches
        failing", so its output is the access worklist, not leads. Calling the
        credentials row "a search nobody ran" while leads sit blocked on a missing
        key is simply false — and this check exists because the first version did
        exactly that."""
        self.resources()
        self.record("s3://some-bucket/x/", "--subject", "data")
        self.probe()                      # no credentials -> a worklist entry
        cov = self.report()["coverage"]
        self.assertTrue(cov["credentials_via_worklist"])
        self.assertNotIn("credentials", cov["subjects_with_nothing"])

    def test_with_no_blockers_an_empty_credentials_row_is_a_real_gap(self):
        """The other direction, or the check above would pass on code that never
        reports the gap at all."""
        self.record("s3://b/x/", "--subject", "data")
        cov = self.report()["coverage"]
        self.assertFalse(cov["credentials_via_worklist"])
        self.assertIn("credentials", cov["subjects_with_nothing"])


class AnomaliesAreComputedNotNoticed(DiscoverCase):
    """CLAUDE.md -> "Never silently", and `searches.md` -> "Cross-cutting rules".

    The things worth a second look in a sweep are mechanical, and leaving them to
    be noticed means they are noticed by whoever happens to read carefully. Two
    objects with the same name and the same byte count in different prefixes; a
    location that was verified and is now gone; a page that has already been wrong
    about one of its claims. The Kontoor handover page states the duplication
    problem itself and then says "sizes and dates are the only way to tell copies
    apart" — which is a job for a script.

    The limit is the other half of the contract: a detector reports what it
    OBSERVED and what that would MEAN, and stops. Whether it matters here is
    judgment, which is why `brief.md` ends with a section no script writes.
    """

    def kinds(self):
        return {a["kind"] for a in self.report()["anomalies"]}

    def test_verified_then_gone_is_urgent(self):
        """The most urgent thing this tool can find: data that existed when we
        looked and does not now."""
        d = self.path("vanishing")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "f.bin"), "w") as fh:
            fh.write("x")
        self.record(d)
        self.probe()
        self.assertEqual(self.leads()[0]["status"], "verified")
        import shutil
        shutil.rmtree(d)
        self.probe("--all")
        flips = [a for a in self.report()["anomalies"]
                 if a["kind"] == "status_flip"]
        self.assertEqual(len(flips), 1)
        self.assertEqual(flips[0]["severity"], "urgent")
        self.assertIn("does not now", flips[0]["means"])

    def test_gone_then_verified_is_reported_as_the_probe_being_wrong(self):
        """The reverse flip is not good news. An earlier `absent` that was wrong
        means every conclusion drawn from it is suspect, and that the probe may be
        reporting absence too eagerly."""
        d = self.path("appearing")
        self.record(d)
        self.probe()
        self.assertEqual(self.leads()[0]["status"], "gone")
        os.makedirs(d, exist_ok=True)
        self.probe("--all")
        flips = [a for a in self.report()["anomalies"]
                 if a["kind"] == "status_flip"]
        self.assertEqual(flips[0]["severity"], "watch")
        self.assertIn("needs revisiting", flips[0]["means"])

    def test_a_source_a_probe_contradicted_is_flagged_for_its_other_claims(self):
        """One wrong path does not make a page useless; it changes what the rest
        of it is worth. Nothing else in the record carries that."""
        ev = "Confluence 123 -> the dataset table"
        self.record(self.path("nope"), ev=ev)
        self.record(self.path("also-nope"), ev=ev)
        os.makedirs(self.path("also-nope"), exist_ok=True)
        self.probe()
        doubted = [a for a in self.report()["anomalies"]
                   if a["kind"] == "source_now_doubted"]
        self.assertEqual(len(doubted), 1)
        self.assertIn("1 of 2", doubted[0]["observed"])

    def test_one_wrong_claim_from_a_single_claim_source_is_not_doubt(self):
        """A source with one claim that was wrong tells you nothing about a
        pattern, and flagging it would fire on every `gone` there is."""
        self.record(self.path("nope"), ev="a one-off note")
        self.probe()
        self.assertNotIn("source_now_doubted", self.kinds())

    def test_a_verified_empty_location_falls_between_the_statuses(self):
        """Not `gone` — the path is there — and not a finding either. It would
        read as a success unless something says otherwise."""
        d = self.path("empty-but-there")
        os.makedirs(d, exist_ok=True)
        self.record(d)
        self.probe()
        self.assertEqual(self.leads()[0]["status"], "verified")
        self.assertIn("verified_but_empty", self.kinds())

    def test_urgent_sorts_before_watch(self):
        """A list where the deletion is third is a list that gets skimmed."""
        d = self.path("gonesoon")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "f"), "w") as fh:
            fh.write("x")
        e = self.path("empty2")
        os.makedirs(e, exist_ok=True)
        self.record(d)
        self.record(e)
        self.probe()
        import shutil
        shutil.rmtree(d)
        self.probe("--all")
        sev = [a["severity"] for a in self.report()["anomalies"]]
        self.assertEqual(sev, sorted(sev, key=lambda s: 0 if s == "urgent" else 1))
        self.assertEqual(sev[0], "urgent")

    def test_a_quiet_sweep_reports_no_anomalies_rather_than_inventing_one(self):
        d = self.path("fine")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "f"), "w") as fh:
            fh.write("data")
        self.record(d)
        self.probe()
        self.assertEqual(self.report()["anomalies"], [])


class TheBriefIsReadInTheOrderThatKeepsItHonest(DiscoverCase):
    """SKILL.md -> "Step 3 — `report`, which states the gaps first", applied to the
    document a person is actually handed.

    `leads.json` is the record and the table is a view; neither is a write-up. The
    section order is the argument: what is odd, then what to look at next, then who
    is blocking, then — last — what was found. A findings list read first becomes an
    inventory and the caveats under it get skipped, which is how a missing dataset
    is discovered in month four.

    And the last section is empty on purpose. Every computed section can only
    report what it measured; generated prose that reads like judgment misrepresents
    where the judgment came from.
    """

    def brief_text(self):
        rc, _out, err = run_script(SCRIPT, "brief", "--project", self.project)
        self.assertEqual(rc, 0, err)
        with open(self.path("ws", "proj", "discovery", "brief.md")) as fh:
            return fh.read()

    def test_the_sections_are_ordered_gaps_before_findings(self):
        self.record("s3://b/x/", "--subject", "data")
        t = self.brief_text()
        for earlier, later in (("What is odd", "What to look at next"),
                               ("What to look at next", "Who is blocking"),
                               ("Who is blocking", "Coverage"),
                               ("Coverage", "What was found"),
                               ("What was found", "Reading")):
            self.assertLess(t.index(earlier), t.index(later),
                            f"{earlier!r} must come before {later!r}")

    def test_it_says_it_is_not_exhaustive_before_any_finding(self):
        self.record("s3://b/x/")
        t = self.brief_text()
        self.assertIn("Not exhaustive, ever", t)
        self.assertLess(t.index("Not exhaustive"), t.index("What was found"))

    def test_the_judgment_section_is_empty_and_says_why(self):
        """A script that filled this in would be asserting that a measurement is
        an interpretation."""
        self.record("s3://b/x/")
        t = self.brief_text()
        self.assertIn("Empty until somebody writes it", t)

    def test_a_lower_bound_is_named_as_one(self):
        d = self.path("some")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "f"), "w") as fh:
            fh.write("xyz")
        self.record(d)
        self.record("s3://b/unreachable/")   # never probed -> not sized
        self.probe("--id", "lead_0001")
        self.assertIn("lower bound", self.brief_text())

    def test_the_grid_renders_every_ranked_source_including_empty_rows(self):
        """An empty row is the finding. Omitting rows with no leads would make the
        table show only what was swept, which is the original problem."""
        self.record("s3://b/x/", "--subject", "data")
        t = self.brief_text()
        for src in ("code", "git_history", "tracking", "s3", "doc", "person"):
            self.assertIn(f"`{src}`", t)

    def test_regenerating_is_safe_and_overwrites(self):
        self.record("s3://b/x/")
        first = self.brief_text()
        self.record("s3://b/y/")
        second = self.brief_text()
        self.assertNotEqual(first, second)
        self.assertIn("2 lead(s)", second)

    def test_writing_up_nothing_is_refused(self):
        rc, out, _ = run_script(SCRIPT, "brief", "--project", self.project)
        self.assertEqual(rc, 1)
        self.assertIn("no sweep to write up", out["refused"])


class ALinkIsAConvenienceNotAFinding(DiscoverCase):
    """`searches.md` -> "The two axes, and why they are not the same"; CLAUDE.md ->
    "Never silently": never let somebody's word become a checked fact.

    The brief is markdown so a reader can open things, and that immediately raises
    two questions the axes already answer. A lead has two URLs, not one: where the
    THING is, and where the CLAIM came from. Collapsing them sends somebody to a
    wiki page when they asked to see the bucket.

    And a derived link is a guess about a console, not a fact about the data — so
    it is derived at RENDER time and never stored, and it is marked. A record that
    stored a console route would go wrong when a vendor reorganises their URLs
    while reading like part of the finding. The path is the record; the worst a
    dead link can do is waste a click, and only as long as nobody mistakes it for
    the status.
    """

    def brief_text(self):
        rc, _out, err = run_script(SCRIPT, "brief", "--project", self.project)
        self.assertEqual(rc, 0, err)
        with open(self.path("ws", "proj", "discovery", "brief.md")) as fh:
            return fh.read()

    def test_the_thing_and_the_claim_get_separate_links(self):
        self.record("s3://b/x/", "--url", "https://console/thing",
                    "--evidence-url", "https://wiki/page")
        lead = self.leads()[0]
        self.assertEqual(lead["url"], "https://console/thing")
        self.assertEqual(lead["evidence_url"], "https://wiki/page")
        t = self.brief_text()
        self.assertIn("https://console/thing", t)
        self.assertIn("https://wiki/page", t)

    def test_a_derived_link_is_not_written_into_the_record(self):
        """Storing it would put a claim about somebody's web routes inside a record
        of what exists, and the two rot on completely different schedules."""
        self.record("s3://b/x/")
        self.assertIsNone(self.leads()[0]["url"])
        self.assertIn("s3.console.aws.amazon.com", self.brief_text())

    def test_a_derived_link_is_marked_and_a_given_one_is_not(self):
        """A guessed console route that 404s must not read like a verified
        location. The dagger is the whole distinction."""
        self.record("s3://b/derived/")
        self.record("s3://b/given/", "--url", "https://exact/place")
        t = self.brief_text()
        self.assertIn("†", t)
        self.assertIn("link built by this script", t)
        given = [l for l in t.splitlines() if "https://exact/place" in l]
        self.assertTrue(given)
        self.assertNotIn(")†", given[0], "a link somebody typed is not a guess")

    def test_an_object_and_a_prefix_get_different_console_routes(self):
        """Sending an object to the prefix view lands on an empty listing, which
        reads exactly like the data being gone — the one confusion this whole skill
        is built to prevent, reintroduced by a URL."""
        mod = load_script(SCRIPT)
        obj, _d = mod.lead_url({"on": "s3", "path": "s3://b/k/file.tar.gz"}, None)
        pre, _d = mod.lead_url({"on": "s3", "path": "s3://b/k/"}, None)
        self.assertIn("/s3/object/b?prefix=k/file.tar.gz", obj)
        self.assertIn("/s3/buckets/b?prefix=k/", pre)

    def test_no_region_is_asserted_in_an_s3_link(self):
        """`resources.json -> aws.region` is one global setting and a bucket has
        its own. The sweep that exercised this had us-west-2 configured and hit a
        bucket named `…-repo-ohio`; a guessed region lands on the wrong console."""
        mod = load_script(SCRIPT)
        url, _d = mod.lead_url({"on": "s3", "path": "s3://b-ohio/k/"},
                               {"aws": {"region": "us-west-2"}})
        self.assertNotIn("us-west-2", url)
        self.assertIn("s3.console.aws.amazon.com", url)

    def test_nothing_is_invented_for_a_source_with_no_stable_url(self):
        """A remote path over ssh has no URL a browser opens, and ClearML's web
        host is a different subdomain from its API host. A document whose links are
        half dead is one nobody clicks twice."""
        mod = load_script(SCRIPT)
        for lead in ({"on": "server:box", "path": "/mnt/data"},
                     {"on": "tracking:clearml", "path": "org"},
                     {"on": "doc", "path": "a wiki page"},
                     {"on": "person", "path": "ask Li"}):
            with self.subTest(on=lead["on"]):
                self.assertEqual(mod.lead_url(lead, None), (None, False))

    def test_a_lead_id_links_to_its_own_row(self):
        """The blocking list names ids and the row carries the evidence — two
        screens apart in a real brief."""
        self.resources()
        self.record("s3://some-bucket/x/")
        self.probe()
        t = self.brief_text()
        self.assertIn('<a id="lead_0001"></a>', t)
        self.assertIn("(#lead_0001)", t)

    def test_the_evidence_column_is_rendered_at_all(self):
        """`--evidence` is mandatory and was not in the table, so the one field
        that separates a path a script read from a line on a wiki was invisible in
        the document people actually read."""
        self.record("s3://b/x/", st="code", ev="train.py:44 DATA_ROOT")
        t = self.brief_text()
        self.assertIn("claim came from", t)
        self.assertIn("train.py:44 DATA_ROOT", t)
        self.assertIn("code:", t)

    def test_a_paren_in_a_path_cannot_break_out_of_the_link(self):
        self.record("s3://b/we(ird)/", )
        self.assertNotIn("ird)/)", self.brief_text())


class ALeadIsAmendedNotEdited(DiscoverCase):
    """CLAUDE.md -> "Contracts": a record written now and read later by someone who
    can no longer verify it. And "Never silently": never let somebody's word become
    a checked fact — including your own earlier word.

    A probe records what a machine saw. Nothing recorded what a PERSON later found
    out, and three real cases arrived at once: a path had been mistyped, a directory
    turned out to be inside a container so the probe was looking at the wrong
    filesystem, and half the images in an archive had no annotation.

    The obvious implementation is to edit the lead, and it destroys exactly what the
    record exists for. The case that proves it: a lead was recorded against a
    mistyped S3 prefix, probed `gone` — correct about a path nobody had claimed —
    and that `gone` read as confirming a document's claim that the real prefix was
    empty. Correcting the path in place would leave a record that looks like it was
    always right, and a later reader would have no way to know a conclusion had once
    been drawn from the wrong thing. So the wrong lead stays, annotated, pointing at
    the lead that supersedes it.
    """

    def note(self, lead_id, text, kind="finding", *extra):
        return run_script(SCRIPT, "note", "--project", self.project,
                          "--id", lead_id, "--note", text, "--kind", kind, *extra)

    def test_a_note_appends_and_overwrites_nothing(self):
        self.record("s3://b/x/", ev="the original evidence")
        self.note("lead_0001", "first thing found")
        self.note("lead_0001", "second thing found")
        lead = self.leads()[0]
        self.assertEqual([n["note"] for n in lead["notes"]],
                         ["first thing found", "second thing found"])
        self.assertEqual(lead["evidence"], "the original evidence",
                         "the original claim must survive being annotated")
        self.assertEqual(lead["path"], "s3://b/x/")

    def test_a_note_is_dated(self):
        """Undated, an annotation cannot be placed relative to the probe it
        contradicts — which is the only thing that makes it interpretable."""
        self.record("s3://b/x/")
        self.note("lead_0001", "found later")
        n = self.leads()[0]["notes"][0]
        self.assertRegex(n["at"], r"^\d{4}-\d\d-\d\dT[\d:]+\+00:00$")

    def test_a_correction_is_a_different_kind_from_a_finding(self):
        """They are read differently and must not collapse: a correction says an
        earlier reading was drawn from the wrong thing, so anything concluded from
        it is suspect. A finding merely adds."""
        self.record("s3://b/x/")
        self.note("lead_0001", "the path was wrong", "correction")
        self.assertEqual(self.leads()[0]["notes"][0]["kind"], "correction")

    def test_a_superseded_lead_says_so_on_itself(self):
        """Stated on the lead, not only in a write-up, so anything reading the
        register sees the entry has been replaced."""
        self.record("s3://b/wrong/")
        self.record("s3://b/right/")
        self.note("lead_0001", "mistyped", "correction", "--supersedes", "lead_0002")
        self.assertEqual(self.leads()[0]["superseded_by"], "lead_0002")

    def test_a_note_does_not_change_the_status(self):
        """Only a probe moves a status. A person writing "actually it is fine"
        would otherwise turn their word into a checked fact — the one thing this
        skill's four statuses exist to prevent."""
        self.record(self.path("nope"))
        self.probe()
        self.assertEqual(self.leads()[0]["status"], "gone")
        self.note("lead_0001", "I think this is actually fine", "context")
        self.assertEqual(self.leads()[0]["status"], "gone")

    def test_annotating_an_unknown_lead_is_refused_and_lists_what_exists(self):
        self.record("s3://b/x/")
        rc, out, _ = self.note("lead_9999", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("no lead", out["refused"])
        self.assertEqual(out["known"], ["lead_0001"])

    def test_corrections_reach_the_brief_before_the_findings(self):
        """An annotation nobody reads is the same failure as an uncommitted record.
        A correction changes how everything below it should be read, so it cannot
        sit in the JSON only."""
        self.record("s3://b/wrong/")
        self.record("s3://b/right/")
        self.note("lead_0001", "recorded one level too deep", "correction",
                  "--supersedes", "lead_0002")
        run_script(SCRIPT, "brief", "--project", self.project)
        with open(self.path("ws", "proj", "discovery", "brief.md")) as fh:
            t = fh.read()
        self.assertIn("Corrections recorded on leads", t)
        self.assertIn("recorded one level too deep", t)
        self.assertLess(t.index("Corrections recorded"), t.index("What was found"))
        self.assertIn("SUPERSEDED BY", t)

    def test_a_plain_finding_shows_in_the_row_without_crying_correction(self):
        self.record("s3://b/x/")
        self.note("lead_0001", "counted 1,895 images and 1,888 annotations")
        run_script(SCRIPT, "brief", "--project", self.project)
        with open(self.path("ws", "proj", "discovery", "brief.md")) as fh:
            t = fh.read()
        self.assertIn("counted 1,895 images", t)
        self.assertNotIn("Corrections recorded on leads", t)


class RegeneratingTheBriefDoesNotEatTheJudgment(DiscoverCase):
    """SKILL.md -> "Step 3 — `brief`, which is the deliverable": every section above
    the last one is computed, the last one is written by hand.

    The brief's own header tells the reader "do not edit above the last section",
    which is an instruction to write in the last one — and the first version then
    overwrote the whole file on every run. So the document invited somebody to spend
    an hour on the only part that required judgment and destroyed it the next time
    anybody regenerated, silently, having just pointed them at that spot.

    Regenerating has to be safe or nobody runs it twice, and a stale brief is worse
    than none: it describes a sweep that has since moved on.
    """

    def brief(self):
        rc, _o, err = run_script(SCRIPT, "brief", "--project", self.project)
        self.assertEqual(rc, 0, err)
        return self.path("ws", "proj", "discovery", "brief.md")

    def write_reading(self, text):
        p = self.brief()
        with open(p) as fh:
            t = fh.read()
        head, _, _ = t.partition("## Reading — filled in by hand")
        with open(p, "w") as fh:
            fh.write(head + "## Reading — filled in by hand\n\n" + text + "\n")

    def test_a_hand_written_reading_survives_regeneration(self):
        self.record("s3://b/x/", "--subject", "data")
        self.write_reading("The Kontoor set is 1,895 images, not ~1,000.")
        self.record("s3://b/y/", "--subject", "data")      # something changed
        with open(self.brief()) as fh:
            t = fh.read()
        self.assertIn("The Kontoor set is 1,895 images", t)
        self.assertIn("2 lead(s)", t, "the computed part must still refresh")

    def test_the_placeholder_is_not_carried_over_as_if_it_were_content(self):
        """Otherwise the untouched placeholder gets pinned in forever and the
        section stops being able to report that nobody has written it."""
        self.record("s3://b/x/")
        self.brief()
        self.record("s3://b/y/")
        with open(self.brief()) as fh:
            t = fh.read()
        self.assertEqual(t.count("Empty until somebody writes it"), 1)
        self.assertNotIn("carried over", t)

    def test_a_carried_section_says_it_is_carried(self):
        self.record("s3://b/x/")
        self.write_reading("judgment goes here")
        with open(self.brief()) as fh:
            t = fh.read()
        self.assertIn("never regenerated", t)


class TheDeclaredBucketIsNotTheSweepableSurface(DiscoverCase):
    """CLAUDE.md -> "Never silently" (never report data you could not look at), and
    `searches.md` -> "Sources, ranked by what a mention is worth".

    `sources` reported the s3 row as `bucket: <resources.json -> aws.s3_bucket>` —
    one bucket, the default a RUN writes to. A credential's reach is a different
    quantity and is routinely much larger; on the project this was found in, the key
    could list twenty and the row named one.

    The under-report is worse than a refusal. "No access" sends somebody to get a
    key. One bucket out of twenty reads as the whole world and sends nobody
    anywhere — a sweep covering 5% of the surface produces a findings list that
    looks complete, which is the exact failure `sources` exists to prevent.

    So the reach is a MEASUREMENT, taken by `surface` (network, dated, may be
    partial) and read by `sources` (records only). The same split as `census.py
    scan` vs `dataset.json`, and it must not be collapsed: `sources` runs at
    conversation start, where four network timeouts before the user's first
    sentence is not a greeting.
    """

    def sources_s3(self):
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        rows = [s for s in out["sources"] if s["source"] == "s3"]
        self.assertEqual(len(rows), 1, "exactly one s3 row")
        return rows[0]

    def write_surface(self, buckets, *, declared="b-declared", enumerated=True):
        by_state = {}
        for b, st in buckets.items():
            by_state.setdefault(st, []).append(b)
        self.write_json("ws/proj/discovery/surface.json", {
            "project": self.project, "measured_at": "2026-08-04T00:00:00+00:00",
            "s3": {"declared_bucket": declared, "enumerated": enumerated,
                   "why": "test", "visible_count": len(buckets),
                   "buckets": {b: {"state": st, "detail": "", "declared": b == declared}
                               for b, st in buckets.items()},
                   "by_state": {k: sorted(v) for k, v in by_state.items()}}})

    def test_the_row_no_longer_names_one_bucket_as_the_surface(self):
        """`bucket` is gone as a field name. A reader who sees one bucket named on
        the checklist scopes the sweep to it, and nothing on the row contradicts
        them — which is how twenty buckets became three."""
        self.resources(aws={"access_key_id": "k", "secret_access_key": "s",
                            "region": "us-east-1", "s3_bucket": "b-declared"})
        row = self.sources_s3()
        self.assertNotIn("bucket", row,
                         "the bare field is what invited the misreading")
        self.assertEqual(row["declared_bucket"], "b-declared")

    def test_a_reach_that_was_never_measured_says_so(self):
        """Absence of a surface reading is not a small surface. It has to warn, or
        the default state of every project is a silent under-report."""
        self.resources(aws={"access_key_id": "k", "secret_access_key": "s",
                            "region": "us-east-1", "s3_bucket": "b-declared"})
        row = self.sources_s3()
        self.assertIsNone(row["reachable_buckets"])
        self.assertIn("NEVER been enumerated", row["surface_warning"])
        self.assertIn("not the surface", row["surface_warning"])

    def test_a_measured_reach_is_reported_with_its_age(self):
        """A reading is dated for the same reason a census is: the answer moves."""
        self.resources(aws={"access_key_id": "k", "secret_access_key": "s",
                            "region": "us-east-1", "s3_bucket": "b-declared"})
        self.write_surface({"b-declared": "listable", "b-two": "listable",
                            "b-three": "access_denied"})
        row = self.sources_s3()
        self.assertEqual(row["reachable_count"], 3)
        self.assertIn("b-three", row["reachable_buckets"])
        self.assertIsNotNone(row["surface_measured_days_ago"])
        self.assertNotIn("surface_warning", row)

    def test_a_failed_enumeration_is_reported_as_a_lower_bound(self):
        """ListAllMyBuckets is an account-level permission and routinely absent on
        a key that reads specific buckets fine. Failing to enumerate must never
        read as "the reach is what we happened to see"."""
        self.resources(aws={"access_key_id": "k", "secret_access_key": "s",
                            "region": "us-east-1", "s3_bucket": "b-declared"})
        self.write_surface({"b-declared": "listable"}, enumerated=False)
        row = self.sources_s3()
        self.assertIn("LOWER BOUND", row["surface_warning"])

    def test_access_denied_is_kept_apart_from_no_key(self):
        """The two need opposite asks. A visible-but-unlistable bucket is a POLICY
        change by its owner; a missing key is a key. Somebody sent to request
        access they already hold comes back a week later with nothing, and the
        blocker is the field the worklist groups on."""
        self.resources(aws={"access_key_id": "k", "secret_access_key": "s",
                            "region": "us-east-1", "s3_bucket": "b-declared"})
        self.write_surface({"b-declared": "listable", "b-denied": "access_denied"})
        row = self.sources_s3()
        self.assertEqual(row["surface_by_state"]["access_denied"], ["b-denied"])
        self.assertTrue(row["usable"], "the credential works; one bucket refuses it")

    def test_the_surface_reading_is_part_of_the_saved_record(self):
        """A tracked leads.json beside an untracked surface.json is a findings
        list whose SCOPE lives on one disk. `record_unsaved` has to catch it —
        a handover is a clone, and the clone would carry the findings without
        the coverage they were measured against."""
        self.resources(aws={"access_key_id": "k", "secret_access_key": "s",
                            "region": "us-east-1", "s3_bucket": "b-declared"})
        self.record("s3://b/x", st="s3")
        subprocess.run(["git", "init", "-q", "."], cwd=self.project,
                       capture_output=True)
        for cfg in (("user.email", "t@t"), ("user.name", "t"),
                    ("commit.gpgsign", "false")):
            subprocess.run(["git", "config", *cfg], cwd=self.project,
                           capture_output=True)
        subprocess.run(["git", "add", "discovery/leads.json"], cwd=self.project,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "leads only"], cwd=self.project,
                       capture_output=True)
        rc, out, err = run_script(SCRIPT, "report", "--project", self.project,
                                  "--json")
        self.assertEqual(rc, 0, err)
        self.assertFalse(out["record_unsaved"], "leads.json is tracked")
        self.write_surface({"b-declared": "listable", "b-two": "listable"})
        rc, out, err = run_script(SCRIPT, "report", "--project", self.project,
                                  "--json")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["record_unsaved"],
                        "an untracked surface reading is an unsaved record")

    def test_surface_refuses_without_a_registry(self):
        rc, out, _ = run_script(SCRIPT, "surface", "--project", self.project)
        self.assertEqual(rc, 1)
        self.assertIn("resources.json", json.dumps(out))

    def test_surface_breaks_on_a_bad_project_path(self):
        """Exit 2, not a false empty. `report` had this bug: a bare project NAME
        produced a clean, empty, entirely wrong answer."""
        rc, out, _ = run_script(SCRIPT, "surface", "--project", "not-a-path")
        self.assertEqual(rc, 2)


class TheFrameworkBlindSpotIsCheckable(DiscoverCase):
    """`layout.md` -> "Code Source Resolution" (the `framework` mode), and
    run-mechanics.md -> "Record integrity".

    `code_snapshot.py` writes FRAMEWORK_BLIND_SPOT on every framework record: a
    pinned version "CANNOT see a local edit to the installed package, where a tree
    would have produced a dirty patch". That was written as a permanent limitation
    and it is not one — pip's dist-info RECORD holds a sha256 per installed file, so
    the question is answerable offline and exactly.

    A limitation nobody can check and a check nobody ran read identically in a
    record. This is the difference.
    """

    def verify(self, spec, *extra):
        return run_script(SCRIPT, "verify-framework", "--spec", spec, *extra)

    def test_a_clean_install_is_as_published(self):
        rc, out, err = self.verify("json==1.0", "--python", sys.executable)
        self.assertEqual(rc, 0, err)
        # stdlib `json` is not a distribution, so this exercises the honest
        # negative rather than a fake pass.
        self.assertEqual(out["state"], "not_installed")

    def test_not_installed_here_is_not_not_installed_anywhere(self):
        """The unreachable/gone split, in the newest probe in the engine."""
        rc, out, _ = self.verify("definitely-not-a-package==1.0",
                                 "--python", sys.executable)
        self.assertEqual(rc, 0)
        self.assertEqual(out["state"], "not_installed")
        self.assertIn("NOT a statement about the environment that ran", out["means"])

    def test_a_missing_interpreter_breaks_rather_than_passing(self):
        rc, out, _ = self.verify("anything==1.0", "--python", "/no/such/python")
        self.assertEqual(rc, 2, "a bad argument is the script breaking, not an answer")
        self.assertIn("RUN environment", json.dumps(out))

    def test_an_unpinned_spec_still_reads_the_installed_version(self):
        """The version question and the integrity question are separate and both
        can fail; collapsing them loses the fix."""
        rc, out, _ = self.verify("definitely-not-a-package", "--python", sys.executable)
        self.assertEqual(rc, 0)
        self.assertIsNone(out["pinned_version"])
        self.assertIsNone(out["version_matches_pin"])

    def test_every_state_carries_its_meaning(self):
        """`means` travels with the reading, not with whichever caller prints it.
        `/repro`'s code axis writes this into a run record; a meaning that only
        appears in the CLI is missing exactly where the record gets written."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fi", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))),
                "lifecycle", "scripts", "shared", "framework_integrity.py"))
        fi = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fi)
        for state in ("as_published", "edited", "incomplete", "not_installed",
                      "unverifiable"):
            self.assertIn(state, fi.STATE_MEANS)
            self.assertTrue(fi.STATE_MEANS[state].strip())


class ThePathsOwnShapeIsEvidence(DiscoverCase):
    """`searches.md` -> "Probes, by `on` — and the fussiness is the point", and
    CLAUDE.md -> "Never silently" (never report data you could not look at).

    `host_unknown` on `/workspace/anyware-ai/src/dataset/...` produced one worklist
    item: find out whose disk this was. Both halves of that are wrong, and a person
    who has seen a Dockerfile sees it instantly:

      * `/workspace` is the container convention. Naming the host does NOT resolve
        the path — the host filesystem has no such path. Somebody sent to ssh in
        and `ls` looks where the path was never at and comes back with a `gone`
        that means nothing, which is the false-`gone` this engine is built against,
        arrived at by following the engine's own advice.
      * the segment after the prefix is almost always a REPO NAME — a lead of a
        different family, needing none of the access that is blocking this one.

    On the run this was built from, the second inference produced the training
    script (34 of 35 recorded hyperparameters identical to the checkpoint's) and
    the tracking project and run name. From a string the register had already
    stored and never read.

    A shape is not a status and never overrides a probe. It changes the FIX.
    """

    def test_a_container_path_says_the_host_is_not_the_answer(self):
        self.record("/workspace/anyware-ai/src/dataset/d.yaml", on="host_unknown",
                    st="checkpoint", ev="train_args.data")
        lead = self.leads()[0]
        self.assertEqual(lead["path_shape"]["environment"], "container")
        self.assertIn("INSIDE a container", lead["path_shape"]["why"])
        self.assertIn("bind mount", lead["path_shape"]["resolved_by"])

    def test_the_repo_name_becomes_a_lead_worth_opening(self):
        """The payoff, and it needs none of the blocked access."""
        self.record("/workspace/anyware-ai/src/x.pt", on="host_unknown",
                    st="checkpoint", ev="train_args.model")
        shape = self.leads()[0]["path_shape"]
        self.assertEqual(shape["likely_repo"], "anyware-ai")
        self.assertIn("REPO NAME", shape["derived_lead"])

    def test_the_probe_reports_the_better_blocker(self):
        """The worklist groups on the blocker, so this is the line that decides
        what somebody actually goes and does."""
        self.record("/workspace/anyware-ai/src/x.pt", on="host_unknown",
                    st="checkpoint", ev="train_args.model")
        rc, out, err = self.probe()
        self.assertEqual(rc, 0, err)
        pr = self.leads()[0]["probes"][-1]
        self.assertEqual(pr["blocker"], "container_path_not_host_path",
                         "the generic blocker sends somebody to the wrong place")
        self.assertIn("REPO NAME", pr["open_this_instead"])

    def test_a_plain_host_path_keeps_the_generic_blocker(self):
        """No shape is the normal case and must not be dressed up. Inventing an
        environment for `/data/...` would be a confident wrong answer, which is
        strictly worse than the honest generic one."""
        self.record("/data/captures/2026/x.tar", on="host_unknown",
                    st="checkpoint", ev="train_args.data")
        self.assertIsNone(self.leads()[0]["path_shape"])
        rc, out, err = self.probe()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.leads()[0]["probes"][-1]["blocker"],
                         "host_unidentified")

    def test_ephemeral_and_managed_layouts_are_named(self):
        """Each says something different about what survived, and collapsing them
        loses the recovery route: SageMaker's container is gone but its S3
        channels are not, and `/tmp` usually means the loss is already real."""
        for path, env in (("/opt/ml/input/data/train", "sagemaker"),
                          ("/content/drive/x", "colab"),
                          ("/kaggle/input/ds/x", "kaggle"),
                          ("/tmp/scratch/x", "ephemeral")):
            self.assertEqual(path_shape_of(path)["environment"], env, path)

    def test_a_shape_is_never_a_status(self):
        """It is read off a string and can be wrong — somebody's host really may
        have a `/workspace`. So it may inform the fix and must never decide
        whether the data is there."""
        self.record("/workspace/repo/x", on="host_unknown", st="doc", ev="a page")
        self.assertEqual(self.leads()[0]["status"], "claim")
        rc, out, _ = self.probe()
        self.assertEqual(json.loads(json.dumps(out))["counts"]["gone"], 0,
                         "a shape may never produce a `gone`")


class AnEmptyWandbListingIsNotAnAbsence(DiscoverCase):
    """CLAUDE.md -> "Never silently" (never report data you could not look at),
    and `searches.md` -> "Probes, by `on` — and the fussiness is the point".

    The comment this replaces claimed an entity with no projects is `gone` "on the
    same bar as an empty S3 prefix: it answered, and there is nothing in it". The
    two APIs behave oppositely, and it is measurable: `aws s3 ls` on a bucket that
    does not exist ERRORS, while `wandb.Api().projects(<nonsense>)` returns `[]`.
    On a live account a real entity gave 10 projects and
    `definitely-no-such-entity-9f3a` gave 0 — no error either time.

    So `[]` conflates "empty" with "no such thing" and cannot be `gone`. This is
    the engine's own worst failure mode, committed by the engine, and it surfaced
    the way it always will: a locator that did not parse came back `gone` about a
    project holding 25 runs — one of them the deployed model's own training.

    The locator half is the other lesson. The strip tolerated `wandb:ent/proj` but
    not `tracking:wandb:ent/proj`, which is the form `report` RENDERS — so the
    string a person copies out of the tool's own output was the one it could not
    read, and the whole thing became the entity name.
    """

    def probe_tracking(self, path):
        self.record(path, on="tracking:wandb", st="code", ev="a config")
        return load_script(SCRIPT)

    def test_every_prefix_the_tool_itself_renders_is_stripped(self):
        d = load_script(SCRIPT)
        seen = {}

        def fake_entity(entity, where, budget_s):
            seen["entity"] = entity
            return "verified", "ok", ["p"], {}

        d.probe_wandb_entity = fake_entity
        for path in ("ent", "wandb:ent", "tracking:wandb:ent"):
            seen.clear()
            d.probe_tracking("tracking:wandb", path, 5.0)
            self.assertEqual(seen.get("entity"), "ent", path)

    def test_an_empty_entity_listing_is_unreachable_not_gone(self):
        d = load_script(SCRIPT)
        real = d.subprocess.run

        class R:
            returncode = 0
            stdout = json.dumps({"entity": "e", "projects": []})
            stderr = ""

        d.subprocess.run = lambda *a, **k: R()
        try:
            status, detail, _s, extra = d.probe_wandb_entity("e", "a key", 5.0)
        finally:
            d.subprocess.run = real
        self.assertEqual(status, "unreachable",
                         "[] cannot distinguish an empty entity from no entity")
        self.assertEqual(extra["blocker"], "tracking:wandb:empty_is_ambiguous")
        self.assertIn("does not exist", detail)

    def test_a_populated_entity_still_verifies(self):
        """The fix must not make the working case unanswerable."""
        d = load_script(SCRIPT)
        real = d.subprocess.run

        class R:
            returncode = 0
            stdout = json.dumps({"entity": "e", "projects": ["a", "b"]})
            stderr = ""

        d.subprocess.run = lambda *a, **k: R()
        try:
            status, _d, sample, _x = d.probe_wandb_entity("e", "a key", 5.0)
        finally:
            d.subprocess.run = real
        self.assertEqual(status, "verified")
        self.assertEqual(sample, ["a", "b"])

    def test_an_empty_project_needs_a_second_signal_to_be_gone(self):
        """`gone` is earned by the project appearing in the entity's own list,
        not by the run list being empty. Same both-signals discipline as the ssh
        sentinel and S3's `Total Objects: 0`."""
        d = load_script(SCRIPT)
        real = d.subprocess.run

        def run_with(payload):
            class R:
                returncode = 0
                stdout = json.dumps(payload)
                stderr = ""
            return lambda *a, **k: R()

        cases = {
            True: ("gone", "the project exists"),
            False: ("gone", "absent from the entity"),
            None: ("unreachable", "could not be read"),
        }
        for exists, (want_status, fragment) in cases.items():
            d.subprocess.run = run_with({"n": 0, "names": [],
                                         "project_exists": exists})
            try:
                status, detail, _s, extra = d.probe_tracking(
                    "tracking:wandb", "ent/proj", 5.0)
            finally:
                d.subprocess.run = real
            self.assertEqual(status, want_status, f"project_exists={exists}")
            self.assertIn(fragment, detail)
            if want_status == "unreachable":
                self.assertEqual(extra["blocker"],
                                 "tracking:wandb:empty_is_ambiguous")
