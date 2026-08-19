"""The conclusion layer — what a belief must not be allowed to outlive.

A run record is read by whoever ran it. A CONCLUSION is read by somebody six
weeks later who cannot re-run anything: the branch is gone, the corpus has
rolled, and all that survives is the sentence. That is exactly the bar in
CLAUDE.md -> "Conventions" for what earns a check — a record written now and
read later by someone who can no longer verify it.

Every check here covers one way a conclusion can read STRONGER than what is
behind it: a tier borrowed from the best evidence instead of the worst, a
refuted premise whose dependents go on standing, a number typed from memory, and
above all a conclusion whose evidence quietly stopped resolving while the status
still said `supported`. That last one is the reason the file exists, and it is
the case ARA's own status vocabulary does not have a word for.
"""

import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "conclude/conclude.py"
CORPUS = "datasets/boxes@260731"
REL = os.path.join("knowledge", "conclusions.json")
RUN = os.path.join("stages", "training", "runs", "run_20260712_141530")
NODE = "stages/exploration/graph.json#N07"


class ConcludeCase(TempDirCase):
    """A project with one resolvable run, one resolvable graph card, and a
    conclusion resting on both."""

    def setUp(self):
        super().setUp()
        self.write_json(os.path.join(RUN, "run.json"),
                        {"mode": "production", "metrics": {"AP50": 92.15}})
        self.write_json(os.path.join("stages", "exploration", "graph.json"),
                        {"nodes": [{"id": "N07", "state": "closed", "verdict": "won"}]})
        rc, out, err = self.c("new")
        self.assertEqual(rc, 0, f"new failed: {out or err}")

    def c(self, *args):
        return run_script(SCRIPT, *args, "--project", self.tmp)

    def rec(self):
        return self.read_json(REL)

    def one(self, cid="K01"):
        for c in self.rec()["conclusions"]:
            if c["id"] == cid:
                return c
        raise AssertionError(f"no conclusion {cid}")

    def add(self, statement="一对一匹配在这个语料上有效",
            falsified_if="重测时 AP50 差值落回噪声地板 0.42 以内", **over):
        args = ["add", "--statement", statement, "--falsified-if", falsified_if,
                "--corpus", CORPUS, "--mode", "production", "--metric", "AP50",
                "--provenance", "user"]
        for k, v in over.items():
            args += ["--" + k.replace("_", "-"), v]
        rc, out, err = self.c(*args)
        self.assertEqual(rc, 0, f"add failed: {out or err}")
        return out["id"]

    def evidence(self, cid, kind="run", ref=RUN, quote="AP50 = 92.15", tier="T3"):
        args = ["evidence", "--id", cid, "--kind", kind, "--ref", ref,
                "--quote", quote]
        if tier:
            args += ["--tier", tier]
        rc, out, err = self.c(*args)
        self.assertEqual(rc, 0, f"evidence failed: {out or err}")
        return out

    def check(self):
        rc, out, err = self.c("check", "--no-fail")
        self.assertEqual(rc, 0, f"check broke: {out or err}")
        return out

    def findings(self, cid=None, severity=None):
        return [f for f in self.check()["findings"]
                if (cid is None or f["id"] == cid)
                and (severity is None or f["severity"] == severity)]

    def status_of(self, cid="K01"):
        return self.check()["computed"][cid]["status"]

    def tier_of(self, cid="K01"):
        return self.check()["computed"][cid]["tier"]


# ---------------------------------------------------------------------------

class UnverifiableIsNotAWeakerSupported(ConcludeCase):
    """CLAUDE.md -> "Never silently": 「Never report data you could not look at.
    A machine that did not answer, a path that is not there, and a directory
    that is genuinely empty are three facts」 — and CLAUDE.md -> Skills ->
    `/repro`, whose four verdicts already draw this line for a run.

    The case ARA has no word for. Its `Status: supported | refuted` assumes the
    evidence stays put; MLClaw retires datasets, deletes checkpoints and loses
    snapshots, so the state that actually occurs is 「nobody can check any more」.
    Folding it into either neighbour is a record-integrity bug in a specific
    direction each way: `supported` promises a check nobody can run, `refuted`
    reports a measurement nobody took.
    """

    def test_evidence_that_stopped_resolving_makes_it_unverifiable(self):
        cid = self.add()
        self.evidence(cid)
        self.assertEqual(self.status_of(cid), "supported")
        import shutil
        shutil.rmtree(self.path(RUN))
        self.assertEqual(self.status_of(cid), "unverifiable")

    def test_it_is_never_reported_as_refuted(self):
        cid = self.add()
        self.evidence(cid)
        import shutil
        shutil.rmtree(self.path(RUN))
        self.assertNotEqual(self.status_of(cid), "refuted")

    def test_the_finding_says_which_of_the_three_facts_it_is(self):
        cid = self.add()
        self.evidence(cid)
        import shutil
        shutil.rmtree(self.path(RUN))
        blob = " ".join(f["detail"] for f in self.findings(cid))
        self.assertIn("UNVERIFIABLE", blob)
        self.assertIn("not false", blob.lower())

    def test_a_file_that_is_there_but_unparseable_is_not_reported_as_absent(self):
        """`gone` (looked, not there) and `unreadable` (found it, could not read
        it) are different facts — the same split `census.py` keeps."""
        cid = self.add()
        self.evidence(cid, kind="node", ref=NODE, quote="verdict: won", tier="T2")
        self.write(os.path.join("stages", "exploration", "graph.json"), "{not json")
        blob = " ".join(f["detail"] for f in self.findings(cid))
        self.assertIn("could not be parsed", blob)
        self.assertIn("not the same fact as absent", blob)


class TheTierIsTheWeakestEvidenceNotTheStrongest(ConcludeCase):
    """CLAUDE.md -> "Never silently": 「Never quote a number without the tier it
    was measured at. A `[T1 trend]` conclusion cited next week as a controlled
    one is how a soft number becomes a hard one, and it is how a false noise
    floor entered a record once already. The tier travels with the number
    forever, in every file and every sentence.」

    Applied to the thing the number is quoted FROM. A conclusion resting on one
    T3 arm and one T1 probe is a T1 conclusion; taking the best available tier
    is precisely the soft-number-becomes-hard mechanism, one level up.
    """

    def test_the_weakest_tier_wins(self):
        cid = self.add()
        self.evidence(cid, tier="T3")
        self.evidence(cid, kind="node", ref=NODE, quote="verdict: won", tier="T1")
        self.assertEqual(self.tier_of(cid), "T1")

    def test_a_recorded_tier_stronger_than_the_evidence_is_a_finding(self):
        cid = self.add()
        self.evidence(cid, tier="T1")
        rec = self.rec()
        rec["conclusions"][0]["tier"] = "T3"
        self.write_json(REL, rec)
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("weakest evidence is T1", blob)

    def test_an_approximation_cannot_carry_a_conclusion_about_the_original(self):
        """T4 is not on the ladder — `graph.py`'s TIER_POWER omits it for the
        same reason: it is an approximation priced for the original, so it
        cannot speak about the original."""
        cid = self.add()
        self.evidence(cid, tier="T4")
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("T4", blob)
        self.assertIn("about the original", blob.lower())


class ARefutedPremiseContestsItsDependentsRatherThanErasingThem(ConcludeCase):
    """`skills/explore/references/experiment-graph.md` §3.5 — two records
    may disagree without one being erased — and CLAUDE.md -> `/explore`, whose
    `graph.py check` 「reports, never repairs」.

    Both failure directions are real and opposite. Deleting the dependent
    destroys the only account of why a whole line of runs was launched; leaving
    it `supported` lets a refuted premise go on being quoted by everything that
    rested on it. `contested` is the state where somebody has to look.
    """

    def _chain(self):
        a = self.add(statement="一对一匹配有效")
        self.evidence(a, tier="T2")
        b = self.add(statement="瓶颈在后处理", depends_on=a)
        self.evidence(b, kind="node", ref=NODE, quote="verdict: won", tier="T2")
        return a, b

    def test_refuting_the_premise_contests_the_dependent(self):
        a, b = self._chain()
        rc, out, err = self.c("refute", "--id", a, "--by", RUN,
                              "--quote", "AP50 = 84.0", "--note", "重测不复现")
        self.assertEqual(rc, 0, f"refute failed: {out or err}")
        self.assertEqual(self.status_of(b), "contested")

    def test_the_dependent_is_not_deleted(self):
        a, b = self._chain()
        self.c("refute", "--id", a, "--by", RUN, "--quote", "AP50 = 84.0")
        self.assertIn(b, [c["id"] for c in self.rec()["conclusions"]])

    def test_the_dependent_does_not_inherit_refuted(self):
        """A refuted premise does not measure the dependent. Propagating
        `refuted` down the chain reports measurements nobody took."""
        a, b = self._chain()
        self.c("refute", "--id", a, "--by", RUN, "--quote", "AP50 = 84.0")
        self.assertNotEqual(self.status_of(b), "refuted")

    def test_propagation_reaches_the_whole_chain(self):
        a, b = self._chain()
        c3 = self.add(statement="所以下一轮不换 backbone", depends_on=b)
        self.evidence(c3, kind="node", ref=NODE, quote="verdict: won", tier="T2")
        self.c("refute", "--id", a, "--by", RUN, "--quote", "AP50 = 84.0")
        self.assertEqual(self.status_of(c3), "contested")

    def test_a_dependency_that_does_not_exist_is_a_finding(self):
        cid = self.add(depends_on="K99")
        self.evidence(cid)
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("K99", blob)


class AConclusionWithNoFalsifierIsNotAdmitted(ConcludeCase):
    """ARA (arXiv:2604.24658) `logic/claims.md` — `Falsification criteria` is a
    mandatory field — and its MLClaw precedent, `lifecycle/run.json ->
    verifies.falsified_if`, whose rule `graph.py` already enforces: 「a
    hypothesis nothing can refute is a wish」.

    The tautology case is the one worth checking, because it is worse than the
    empty field: 「如果最后发现没用」 looks filled, passes every measurement, and
    is met by nothing.
    """

    def test_the_field_is_required_at_the_cli(self):
        rc, out, err = self.c("add", "--statement", "s", "--corpus", CORPUS)
        self.assertNotEqual(rc, 0)

    def test_an_empty_falsifier_is_critical(self):
        cid = self.add()
        self.evidence(cid)
        rec = self.rec()
        rec["conclusions"][0]["falsified_if"] = ""
        self.write_json(REL, rec)
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("preference", blob)

    def test_a_tautology_naming_neither_number_nor_metric_is_flagged(self):
        cid = self.add(falsified_if="如果后来发现这个办法其实不行")
        self.evidence(cid)
        blob = " ".join(f["detail"] for f in self.findings(cid))
        self.assertIn("cannot execute", blob)

    def test_naming_the_scope_metric_is_enough(self):
        cid = self.add(falsified_if="换更大 backbone 后 AP50 反超")
        self.evidence(cid)
        blob = " ".join(f["detail"] for f in self.findings(cid))
        self.assertNotIn("cannot execute", blob)


class ANumberInTheBeliefCarriesTheLineItCameFrom(ConcludeCase):
    """CLAUDE.md -> "Never silently": 「Never record a metric you did not read」,
    and the same grounding rule `graph.py` applies to `sources[].quote`.

    A prohibition leaves no trace when it is broken: a value typed from memory
    and a value read off a log are the same JSON. The quote is what makes the
    difference visible.
    """

    def test_a_number_absent_from_every_quote_is_critical(self):
        cid = self.add(statement="AP50 从 84.16 抬到 92.15")
        self.evidence(cid, quote="AP50 = 92.15")     # 84.16 is nowhere
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("84.16", blob)

    def test_a_number_present_in_a_quote_passes(self):
        cid = self.add(statement="AP50 从 84.16 抬到 92.15")
        self.evidence(cid, quote="at_held 84.16 -> at_own 92.15")
        self.assertEqual(self.findings(cid, "critical"), [])

    def test_the_percentage_form_of_the_same_number_matches(self):
        """Records keep the fraction, logs print the percentage — the shared
        `digits()` helper exists so 0.0462 and \"4.62%\" are one number."""
        cid = self.add(statement="这类帧占 4.62")
        self.evidence(cid, quote="G>=52 frames: 254/5495 (4.62%)")
        self.assertEqual(self.findings(cid, "critical"), [])

    def test_an_ungrounded_number_in_the_interpretation_is_caught_too(self):
        cid = self.add(interpretation="大概能再吃到 3.5 个点")
        self.evidence(cid)
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("3.5", blob)

    def test_a_bare_path_is_not_grounding(self):
        rc, out, err = self.c("evidence", "--id", self.add(), "--kind", "run",
                              "--ref", RUN, "--quote", "")
        cid = "K01"
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("quote", blob)


class TheGroundingCheckDoesNotFloodOnIdentifiers(ConcludeCase):
    """CLAUDE.md -> "Key design principles", the 「Decide what evidence can
    decide」 bullet: 「Asking has a cost
    nothing else in this file names… A skill that stops to ask about a package
    install has taught its reader to skim — and the next thing skimmed is a
    checkpoint deletion.」

    The same cost applies to a finding. A metric name, a snapshot id and a date
    all contain digits and none of them is a measurement; demanding a source for
    each floods the report, and a report that gets skimmed is worse than no
    report. This is a check on the check, and it earns its place because the
    first implementation did exactly this.
    """

    def test_a_metric_name_is_not_a_measurement(self):
        cid = self.add(statement="AP50 上有效")
        self.evidence(cid, quote="won")
        self.assertEqual(self.findings(cid, "critical"), [])

    def test_a_snapshot_id_is_not_a_measurement(self):
        cid = self.add(statement="在 datasets/boxes@260731 上有效")
        self.evidence(cid, quote="won")
        self.assertEqual(self.findings(cid, "critical"), [])

    def test_a_run_id_is_not_a_measurement(self):
        cid = self.add(statement="见 run_20260712_141530")
        self.evidence(cid, quote="won")
        self.assertEqual(self.findings(cid, "critical"), [])

    def test_a_date_is_not_a_measurement(self):
        cid = self.add(statement="2026-08-14 那轮的结果")
        self.evidence(cid, quote="won")
        self.assertEqual(self.findings(cid, "critical"), [])

    def test_a_real_measurement_is_still_caught(self):
        """The anti-flood rule must not be the reason a number goes ungrounded."""
        cid = self.add(statement="AP50 达到 92.15")
        self.evidence(cid, quote="won")
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("92.15", blob)


class AConfidenceIsComputedNotWritten(ConcludeCase):
    """`lifecycle/references/layout.md` — the split between what goes and looks
    (`census.py`, dated, may be partial) and the durable contract
    (`dataset.json`) — applied here: the declared half is the statement, the
    falsifier and the refs; the derived half is `status` and `tier`.

    Writing the derived half by hand is the entire failure this record exists to
    catch. A conclusion whose confidence outlived its evidence is
    indistinguishable, in JSON, from one whose evidence is intact.
    """

    def test_status_cannot_be_set_by_hand(self):
        cid = self.add()
        rc, out, err = self.c("set", "--id", cid, "--field", "status",
                              "--value", "supported")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")
        self.assertIn("computed", json.dumps(out))

    def test_tier_cannot_be_set_by_hand(self):
        cid = self.add()
        rc, out, err = self.c("set", "--id", cid, "--field", "tier", "--value", "T3")
        self.assertEqual(rc, 1)

    def test_a_stored_status_that_outlived_its_evidence_is_reported(self):
        cid = self.add()
        self.evidence(cid)
        rec = self.rec()
        rec["conclusions"][0]["status"] = "supported"
        self.write_json(REL, rec)
        import shutil
        shutil.rmtree(self.path(RUN))
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("recorded as `supported`", blob)
        self.assertIn("unverifiable", blob)

    def test_the_refusal_exits_one_not_two(self):
        """CLAUDE.md -> "Script Integration": 1 = the script worked and the
        answer is no; 2 = it broke, fall back and do it by hand. A refusal that
        exits 2 gets worked around, which here means hand-writing the field the
        refusal exists to protect."""
        cid = self.add()
        rc, _, _ = self.c("set", "--id", cid, "--field", "status", "--value", "x")
        self.assertEqual(rc, 1)


class ARefutationNamesTheMeasurementThatDidIt(ConcludeCase):
    """CLAUDE.md -> "Never silently": 「Never let somebody's word become a
    checked fact. An answer is a `claim` until something other than the person
    confirms it.」

    Overturning a conclusion is the highest-consequence write in this file — it
    moves everything downstream. A refutation whose reference cannot be opened
    is an opinion overruling a record, and it produces exactly the outcome the
    rule names: 「the operator says it finished」 and a counted result stop being
    distinguishable.
    """

    def test_an_unresolvable_reference_is_refused(self):
        cid = self.add()
        self.evidence(cid)
        rc, out, err = self.c("refute", "--id", cid, "--by",
                              "stages/training/runs/run_nope", "--quote", "AP50 = 84")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")
        self.assertIn("opinion overruling a record", json.dumps(out))

    def test_the_refusal_does_not_write_the_status(self):
        cid = self.add()
        self.evidence(cid)
        self.c("refute", "--id", cid, "--by", "nope/nowhere", "--quote", "x")
        self.assertNotEqual(self.one(cid).get("status"), "refuted")

    def test_a_resolvable_reference_is_recorded_with_its_quote(self):
        cid = self.add()
        self.evidence(cid)
        rc, out, err = self.c("refute", "--id", cid, "--by", RUN,
                              "--quote", "AP50 = 84.0 on rerun", "--note", "n")
        self.assertEqual(rc, 0, f"refute failed: {out or err}")
        self.assertEqual(self.one(cid)["refuted_by"]["quote"], "AP50 = 84.0 on rerun")

    def test_a_status_of_refuted_without_a_reference_is_critical(self):
        cid = self.add()
        self.evidence(cid)
        rec = self.rec()
        rec["conclusions"][0]["status"] = "refuted"
        self.write_json(REL, rec)
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("refuted_by", blob)


class EvidenceNobodyHereConfirmedIsAClaim(ConcludeCase):
    """CLAUDE.md -> Skills -> `/ask-human`: 「Put a question to a person and
    record the answer as what it is — `claim` / `verified` / `decision`」, and
    the "Never silently" rule behind it.

    Citing a paper or a colleague's number is legitimate; letting a stack of
    them read as a settled conclusion is not. MLClaw already has the word for
    this state, and reusing it is the point — an all-external conclusion is a
    `claim`, and the vocabulary should say so rather than inventing a synonym.
    """

    def test_an_external_reference_does_not_resolve_and_says_so(self):
        cid = self.add()
        out = self.evidence(cid, kind="external", ref="arXiv:2304.08069",
                            quote="RT-DETR reports 53.1 AP", tier="T0")
        self.assertEqual(out["resolves"], "claim")

    def test_a_conclusion_resting_only_on_external_refs_is_flagged(self):
        cid = self.add(statement="一对一匹配普遍有效")
        self.evidence(cid, kind="external", ref="arXiv:2304.08069",
                      quote="one-to-one matching removes NMS", tier="T0")
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("CLAIM", blob)

    def test_one_local_measurement_is_enough_to_make_it_a_conclusion(self):
        cid = self.add()
        self.evidence(cid, kind="external", ref="arXiv:2304.08069",
                      quote="one-to-one matching removes NMS", tier="T2")
        self.evidence(cid, tier="T2")
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertNotIn("CLAIM", blob)


class AConclusionIsAboutACorpus(ConcludeCase):
    """CLAUDE.md -> "Never silently": 「Never let a share measured somewhere else
    stand for this corpus. A fault's share is a property of the corpus, not of
    the fault.」

    One level up from the share. 「多帧融合没用」 is a sentence about a corpus that
    does not name it, which is what makes it unanswerable six weeks later — and
    what makes it get repeated on a corpus where it was never measured.
    """

    def test_the_corpus_is_required_at_the_cli(self):
        rc, out, err = self.c("add", "--statement", "s", "--falsified-if", "AP50 down")
        self.assertNotEqual(rc, 0)

    def test_an_emptied_corpus_is_critical(self):
        cid = self.add()
        self.evidence(cid)
        rec = self.rec()
        rec["conclusions"][0]["scope"]["corpus"] = None
        self.write_json(REL, rec)
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("about a corpus", blob)

    def test_the_render_prints_the_corpus_beside_every_statement(self):
        cid = self.add()
        self.evidence(cid)
        rc, out, err = self.c("render")
        self.assertEqual(rc, 0, f"render failed: {out or err}")
        with open(out["path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn(CORPUS, md)


class SettledConclusionsAreNotRewritten(ConcludeCase):
    """`lifecycle/references/run-mechanics.md` -> "Record integrity", and the
    same rule `graph.py` applies to settled cards.

    What was believed at the time is what explains the runs launched at the
    time. Editing a refuted conclusion into agreement with today destroys the
    only account of why the money was spent — and it is the edit somebody makes
    with the best intentions, to 「keep the file accurate」.
    """

    def test_a_refuted_conclusion_cannot_be_edited(self):
        cid = self.add()
        self.evidence(cid)
        self.c("refute", "--id", cid, "--by", RUN, "--quote", "AP50 = 84")
        rc, out, err = self.c("set", "--id", cid, "--field", "statement",
                              "--value", "其实一直没用")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")

    def test_a_refuted_conclusion_cannot_take_new_evidence(self):
        cid = self.add()
        self.evidence(cid)
        self.c("refute", "--id", cid, "--by", RUN, "--quote", "AP50 = 84")
        rc, out, err = self.c("evidence", "--id", cid, "--kind", "run",
                              "--ref", RUN, "--quote", "AP50 = 99")
        self.assertEqual(rc, 1)

    def test_superseding_keeps_the_old_belief(self):
        old = self.add(statement="一对一匹配有效")
        self.evidence(old)
        new = self.add(statement="一对一匹配只在密集场景有效")
        self.evidence(new)
        rc, out, err = self.c("supersede", "--id", old, "--by", new, "--note", "n")
        self.assertEqual(rc, 0, f"supersede failed: {out or err}")
        self.assertIn(old, [c["id"] for c in self.rec()["conclusions"]])
        self.assertEqual(self.status_of(old), "superseded")

    def test_a_supersedes_pointer_must_land_somewhere(self):
        cid = self.add()
        self.evidence(cid)
        rec = self.rec()
        rec["conclusions"][0]["status"] = "superseded"
        rec["conclusions"][0]["superseded_by"] = "K99"
        self.write_json(REL, rec)
        blob = " ".join(f["detail"] for f in self.findings(cid, "critical"))
        self.assertIn("K99", blob)

    def test_ids_are_never_reused(self):
        a = self.add()
        b = self.add()
        self.assertNotEqual(a, b)
        self.evidence(a)
        self.c("refute", "--id", a, "--by", RUN, "--quote", "AP50 = 84")
        self.assertNotEqual(self.add(), a)


class CheckReportsAndNeverRepairs(ConcludeCase):
    """CLAUDE.md -> `/explore`: 「nine invariants, reported, never repaired」, and
    "Conventions" -> Contracts: 「When a check fails, the first question is 'is
    the contract still right?'」

    A record that silently corrects itself cannot be audited. Here the drift
    between stored and computed IS the finding — repairing it would erase the
    only evidence that a conclusion outlived its support, while turning the
    report green.
    """

    def test_check_does_not_touch_the_file(self):
        cid = self.add()
        self.evidence(cid)
        rec = self.rec()
        rec["conclusions"][0]["status"] = "supported"
        rec["conclusions"][0]["tier"] = "T3"
        self.write_json(REL, rec)
        before = self.read(REL)
        self.c("check", "--no-fail")
        self.assertEqual(self.read(REL), before)

    def test_check_says_it_repaired_nothing(self):
        cid = self.add()
        self.evidence(cid)
        self.assertIn("reports", self.check()["repaired"])

    def test_criticals_exit_one(self):
        self.add()                       # no evidence -> critical
        rc, out, err = self.c("check")
        self.assertEqual(rc, 1, f"expected a refusal, got {rc}: {out or err}")

    def test_a_clean_graph_exits_zero(self):
        cid = self.add()
        self.evidence(cid)
        rc, out, err = self.c("check")
        self.assertEqual(rc, 0, f"expected clean, got {rc}: {out or err}")


class TheArtifactSaysWhatTheRecordSays(ConcludeCase):
    """The user's ask, and ARA's `PAPER.md`: the conclusion has to be READABLE
    six weeks later, by someone who will not open the JSON.

    A renderer is where qualifiers go to die — the sentence survives and the
    tier, the corpus and the status do not. So the artifact is checked against
    the same rule as the record: CLAUDE.md's 「Never quote a number without the
    tier it was measured at… in every file and every sentence.」
    """

    def _section(self, cid):
        """The conclusion's OWN block, not the whole page.

        ‼️ Asserting against the whole file passes on the legend at the top,
        which names every status and would go on passing while the conclusion
        rendered with none of them. Found by mutation: two checks here were
        green against a build that had stopped computing `unverifiable`.
        """
        rc, out, err = self.c("render")
        self.assertEqual(rc, 0, f"render failed: {out or err}")
        with open(out["path"], encoding="utf-8") as f:
            md = f.read()
        body = md.split(f"## {cid} ", 1)
        self.assertEqual(len(body), 2, f"{cid} has no section in:\n{md}")
        return body[1].split("\n## ", 1)[0]

    def test_every_conclusion_prints_its_status_and_tier(self):
        cid = self.add()
        self.evidence(cid, tier="T1")
        sec = self._section(cid)
        self.assertIn("tier **T1**", sec)
        self.assertIn("`supported`", sec)

    def test_interpretation_is_rendered_under_its_own_heading(self):
        """ARA's separation is the borrowing. Printing the argument inside the
        statement is how a conclusion comes to read as if its mechanism had been
        measured."""
        cid = self.add(interpretation="推测是 NMS 抑制了密集场景的真阳性")
        self.evidence(cid)
        sec = self._section(cid)
        self.assertIn("Interpretation", sec)
        self.assertIn("未测量", sec)
        self.assertIn("推测是 NMS", sec)

    def test_the_artifact_shows_which_evidence_no_longer_resolves(self):
        cid = self.add()
        self.evidence(cid)
        import shutil
        shutil.rmtree(self.path(RUN))
        sec = self._section(cid)
        self.assertIn("gone", sec)           # the evidence row
        self.assertIn("`unverifiable`", sec)  # the conclusion's own heading


class AScreenOfBeliefsNobodyAskedFor(ConcludeCase):
    """CLAUDE.md -> `/explore`, and `graph.py`'s PROVENANCE rule: 「a graph that
    is all `ai-suggested` is a graph nobody asked for, and `check` says so」.

    It lands harder one level up. A card nobody asked for costs one run; a
    CONCLUSION nobody asked for gets quoted back at the user as their own
    position, and `ai-suggested` never auto-upgrades precisely so that the
    agent's own confidence cannot earn the tag.
    """

    def test_an_all_ai_suggested_file_is_flagged(self):
        rc, out, err = self.c("add", "--statement", "s", "--falsified-if",
                              "AP50 drops", "--corpus", CORPUS, "--metric", "AP50",
                              "--provenance", "ai-suggested")
        self.assertEqual(rc, 0, f"add failed: {out or err}")
        self.evidence(out["id"])
        blob = " ".join(f["detail"] for f in self.findings())
        self.assertIn("nobody has confirmed", blob)

    def test_one_user_conclusion_clears_it(self):
        rc, out, _ = self.c("add", "--statement", "s", "--falsified-if", "AP50 drops",
                            "--corpus", CORPUS, "--metric", "AP50",
                            "--provenance", "ai-suggested")
        self.evidence(out["id"])
        self.evidence(self.add())          # add() records provenance `user`
        blob = " ".join(f["detail"] for f in self.findings())
        self.assertNotIn("nobody has confirmed", blob)

    def test_the_tag_is_not_upgraded_by_attaching_evidence(self):
        rc, out, _ = self.c("add", "--statement", "s", "--falsified-if", "AP50 drops",
                            "--corpus", CORPUS, "--metric", "AP50",
                            "--provenance", "ai-suggested")
        cid = out["id"]
        self.evidence(cid)
        self.assertEqual(self.one(cid)["provenance"], "ai-suggested")


if __name__ == "__main__":
    unittest.main()
