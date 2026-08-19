"""Holding many machines at once, and the four ways that loses money in silence.

The lease layer had no checks at all until this file, which was defensible while it
held one box for one run: a mistake there shows up as one bill nobody wanted. A fleet
changes the arithmetic — N boxes, held across a search that runs overnight, released by
a conversational agent that may not be there when it matters. Every rule below is one
where the wrong behaviour produces **no error at all**: a report that says zero, a
figure nobody confirmed, a trial counted as a refutation, a stopped box read as gone.

Contracts enforced:
  fleet.md -> "The two questions, and why one list cannot answer both"
  fleet.md -> "Slot, not machine"
  fleet.md -> "Owned before rented"
  fleet.md -> "A preempted trial is not a failed trial"
  fleet.md -> "Cost is reported before it is spent, once, by the caller"
  `skills/lease/references/contract.md` -> "Scope completeness"
  `skills/lease/references/contract.md` -> "Normalized enums"

What is deliberately NOT checked: anything that needs a provider to answer. These run
with no network and no credential, which is the only way they mean the same thing on
every machine.
"""
import io
import json
import os
import shutil
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout

from helpers import load_script, REPO_ROOT, TempDirCase, run_script


def capture(fn, *args, **kwargs):
    """Run something that `emit`s and hand back the parsed payload."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*args, **kwargs)
    except SystemExit:
        pass
    return json.loads(buf.getvalue() or "null")


class SweepScope(unittest.TestCase):
    """`skills/lease/references/contract.md` -> "Scope completeness".

    A host that did not answer holds an unknown number of claims; a host that answered
    and reported none holds zero. `sweep` used to return `[]` for both, and `reap` then
    printed `orphans: []` — which is the same output as "there are no forgotten boxes",
    and is the one output anybody reads.
    """

    def setUp(self):
        self.ssh = load_script("lease/provider_ssh.py")

    def _sweep(self, servers, reachable):
        self.ssh.load_servers = lambda _path: servers
        self.ssh.remote_info = lambda entry, want_ram=False: (
            {"now": 0, "gpus": [], "claims": {}, "ram_gb": None}
            if entry["host"] in reachable else None)
        args = types.SimpleNamespace(res="unused", tag_prefix="mlclaw-", billing_only=False)
        return capture(self.ssh.v_sweep, args)

    def test_unreachable_host_is_not_zero_claims(self):
        out = self._sweep({"box": {"host": "dead"}}, reachable=set())
        self.assertEqual(out["units"], [])
        self.assertFalse(out["scope"]["complete"],
                         "an unreachable host must make the sweep incomplete, not empty")
        self.assertEqual([u["scope"] for u in out["scope"]["unreached"]], ["box"])

    def test_reachable_and_empty_is_complete(self):
        out = self._sweep({"box": {"host": "alive"}}, reachable={"alive"})
        self.assertTrue(out["scope"]["complete"])
        self.assertEqual(out["scope"]["unreached"], [])

    def test_partial_fleet_is_incomplete(self):
        out = self._sweep({"a": {"host": "alive"}, "b": {"host": "dead"}},
                          reachable={"alive"})
        self.assertFalse(out["scope"]["complete"])
        self.assertEqual([u["scope"] for u in out["scope"]["unreached"]], ["b"])

    def test_scope_helper_cannot_claim_complete_while_naming_a_gap(self):
        """`complete` is derived, never passed in — otherwise an adapter can report a
        clean scope in the same breath as the corner it missed."""
        common = load_script("lease/_common.py")
        out = common.sweep_result([], checked=["a"], unreached=[{"scope": "b", "why": "x"}])
        self.assertFalse(out["scope"]["complete"])


class LeaseMergesScope(TempDirCase):
    """`skills/lease/references/contract.md` -> "Scope completeness", layer 2's half.

    L2 merges several adapters. Two ways the merged answer can lie: an adapter that
    errored outright (its whole scope went unread, and `errors` is a field nobody reads
    next to an empty orphan list), and an adapter predating the envelope, whose bare list
    cannot have enumerated what it never walked.
    """

    def setUp(self):
        super().setUp()
        self.lease = load_script("lease/lease.py")

    def _collect(self, payloads, verb="sweep"):
        self.lease.call = lambda name, res, v, *extra: payloads[name]
        return self.lease.collect(list(payloads), "unused", verb)

    @staticmethod
    def _envelope(units, storage=(), **scope):
        """A sweep payload that has looked at storage, so these checks exercise the
        scope rules rather than tripping the storage one on every fixture."""
        return (True, {"units": units, "storage": list(storage),
                       "scope": {"complete": True, "checked": [], "unreached": [], **scope}})

    def test_failed_provider_makes_the_result_incomplete(self):
        _, errors, scope, _ = self._collect(
            {"boom": (False, {"error": "credential_expired", "detail": "token"})})
        self.assertIn("boom", errors)
        self.assertFalse(scope["complete"],
                         "a provider that errored is an unreached corner, not just a "
                         "line in `errors` beside an empty orphan list")

    def test_bare_list_from_sweep_is_treated_as_unknown_scope(self):
        rows, _, scope, _ = self._collect({"old": (True, [{"instance_id": "i", "tag": "t"}])})
        self.assertEqual(len(rows), 1)
        self.assertFalse(scope["complete"])

    def test_a_shape_this_layer_cannot_read_is_an_unread_corner_not_a_crash(self):
        """`capacity` is contractually a bare list. An adapter that wraps its rows in an
        object of its own used to be spread key-by-key here and blew up on a string — a
        TypeError raised by L2 for a mistake in L1, which sends the reader to the wrong
        file. It is refused as an unread corner instead, which is also what makes the
        result honest: those rows were not merged, so the answer covers less than it looks."""
        rows, errors, scope, _ = self._collect(
            {"odd": (True, {"options": [{"machine_type": "x"}]})}, verb="capacity")
        self.assertEqual(rows, [])
        self.assertIn("odd", errors)
        self.assertFalse(scope["complete"])

    def test_capacity_may_return_a_bare_list(self):
        """Only `sweep`/`history` carry the envelope; holding `capacity` to it would
        make every adapter incomplete for no reason."""
        _, _, scope, _ = self._collect({"p": (True, [{"machine_type": "x"}])}, verb="capacity")
        self.assertTrue(scope["complete"])

    def test_envelope_is_unwrapped_and_merged(self):
        rows, _, scope, _ = self._collect({"p": self._envelope(
            [{"instance_id": "i", "tag": "t"}], complete=False, checked=["one"],
            unreached=[{"scope": "two", "why": "timeout"}])})
        self.assertEqual(rows[0]["provider"], "p", "L2 injects provider identity, not L1")
        self.assertFalse(scope["complete"])
        self.assertEqual(scope["unreached"][0]["provider"], "p")


class ReapSaysWhetherItLooked(TempDirCase):
    """fleet.md -> "The two questions, and why one list cannot answer both".

    `orphans: []` is the entire product of `reap`, and it is byte-identical whether the
    sweep reached every project or none of them. The flag beside it is the only thing
    that separates "there are no forgotten boxes" from "nobody looked".
    """

    def setUp(self):
        super().setUp()
        self.lease = load_script("lease/lease.py")
        self.res = self.path("resources.json")
        self.write_json("resources.json", {"compute": {}, "servers": {}})

    def _reap(self, scope_complete):
        self.lease.providers = lambda _res: ["p"]
        self.lease.collect = lambda names, res, verb, *extra: (
            [], {} if scope_complete else {"p": {"error": "transient"}},
            {"complete": scope_complete, "checked": [], "unreached":
             [] if scope_complete else [{"provider": "p", "why": "timeout"}]}, [])
        args = types.SimpleNamespace(res=self.res, tag_prefix="mlclaw-", billing_only=False)
        return capture(self.lease.v_reap, args)

    def test_empty_orphans_off_a_partial_sweep_says_lower_bound(self):
        out = self._reap(scope_complete=False)
        self.assertEqual(out["orphans"], [])
        self.assertFalse(out["complete"])
        self.assertTrue(out["orphans_is_lower_bound"])

    def test_empty_orphans_off_a_whole_sweep_is_an_answer(self):
        out = self._reap(scope_complete=True)
        self.assertTrue(out["complete"])
        self.assertFalse(out["orphans_is_lower_bound"])


class StoppedIsNotGone(unittest.TestCase):
    """`skills/lease/references/contract.md` -> "Normalized enums".

    Compute billing stops on STOPPED and storage billing does not. Mapping it to `gone`
    closes the lease row, drops the box out of every "what am I paying for" report, and
    leaves a multi-TB boot disk billing with nothing anywhere pointing at it.
    """

    def test_stopped_maps_to_running(self):
        neb = load_script("lease/provider_nebius.py")
        self.assertEqual(neb.STATE_MAP["STOPPED"], "running")
        self.assertEqual(neb.STATE_MAP["STOPPING"], "running")
        self.assertNotIn("gone", neb.STATE_MAP.values(),
                         "`gone` is proven by the instance being absent, never by a "
                         "state string the provider reports about a box that still exists")

    def test_machine_type_round_trips_from_a_capacity_row(self):
        """The contract's whole L3 flow is show-the-table, user picks a row, lease that
        row. A `machine_type` that does not parse back breaks it at the last step."""
        neb = load_script("lease/provider_nebius.py")
        region, platform, preset, pool = neb.parse_mt(
            "eu-north1:gpu-h100-sxm/1gpu-16vcpu-200gb@preemptible")
        self.assertEqual((region, platform, pool), ("eu-north1", "gpu-h100-sxm", "preemptible"))
        self.assertEqual(preset, "1gpu-16vcpu-200gb")


class HistoryIsTheOnlyPastTense(unittest.TestCase):
    """fleet.md -> "Group by id, never by name".

    A name can span two machines — one dead, one alive — and a name-keyed verdict then
    reports the live one as released, which is the exact inverse of the truth delivered
    confidently. And an id can be stopped, started and stopped again, so "does a DELETE
    appear anywhere" is not the question either.
    """

    def setUp(self):
        self.neb = load_script("lease/provider_nebius.py")

    def _ev(self, action, at, outcome="DONE", name=None):
        return {"instance_id": "i-1", "name": name, "action": action, "at": at,
                "actor": "someone", "outcome": outcome, "tag": None}

    def test_last_decisive_event_wins(self):
        verdict = self.neb._verdict([self._ev("CREATE", "01"), self._ev("STOP", "02"),
                                     self._ev("START", "03")])
        self.assertTrue(verdict["verdict"].startswith("ALIVE?"),
                        "a box stopped then started is not stopped")

    def test_stop_is_reported_as_still_billing(self):
        verdict = self.neb._verdict([self._ev("CREATE", "01"), self._ev("STOP", "02")])
        self.assertIn("DISK STILL BILLS", verdict["verdict"])

    def test_a_failed_create_does_not_invent_a_machine(self):
        verdict = self.neb._verdict([self._ev("CREATE", "01", outcome="ERROR")])
        self.assertTrue(verdict["verdict"].startswith("UNKNOWN"),
                        "a CREATE that ended in ERROR left nothing behind; counting it "
                        "invents a machine that never existed")

    def test_a_rename_is_one_machine_with_an_alias(self):
        verdict = self.neb._verdict([self._ev("CREATE", "01", name="auto-name"),
                                     self._ev("UPDATE", "02", name="scene-gen"),
                                     self._ev("START", "03", name="scene-gen")])
        self.assertEqual(verdict["name"], "scene-gen")
        self.assertEqual(verdict["aka"], ["auto-name"])

    def test_an_adapter_with_no_log_says_so_rather_than_inferring(self):
        ssh = load_script("lease/provider_ssh.py")
        ssh.load_servers = lambda _path: {"box": {}}
        out = capture(ssh.v_history, types.SimpleNamespace(res="unused"))
        self.assertFalse(out["supported"])
        self.assertEqual(out["events"], [])


class PoolSpend(TempDirCase):
    """fleet.md -> "Cost is reported before it is spent, once, by the caller".

    The only moment a fleet's cost is visible is before `open`. A pool acquired without
    that number reads as free, and the search that acquired it runs unattended.
    """

    def setUp(self):
        super().setUp()
        self.pool = load_script("shared/pool.py")

    def _plan(self, options, scope=None):
        self.pool.lease = lambda *a, **kw: (True, {
            "options": options, "scope": scope or {"complete": True}, "errors": None})

    def _args(self, **kw):
        base = dict(slots=2, gpu_count=1, gpu_memory_gb=0, arch_min=None, host_ram_gb=None,
                    allow_preemptible=True, hours=4, session=self.path("s"),
                    ttl_s=3600, project=None, reopen=False, confirmed_usd_per_hr=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_open_refuses_a_billing_pool_with_no_confirmed_figure(self):
        self._plan([{"machine_type": "r:p/x@on_demand", "provider": "n", "avail": 4,
                     "max_single_instance": 4, "gpu_count": 1, "price_hr": 3.85,
                     "price_status": "claim"}])
        out = capture(self.pool.v_open, self._args())
        self.assertIn("no confirmed figure", out["error"])
        self.assertFalse(os.path.exists(os.path.join(self.path("s"), "pool.json")),
                         "refusing must happen before anything is written or acquired")

    def test_open_refuses_when_the_price_moved_above_what_was_confirmed(self):
        self._plan([{"machine_type": "r:p/x@on_demand", "provider": "n", "avail": 4,
                     "max_single_instance": 4, "gpu_count": 1, "price_hr": 9.0,
                     "price_status": "claim"}])
        out = capture(self.pool.v_open, self._args(confirmed_usd_per_hr=7.7))
        self.assertIn("more than was confirmed", out["error"])

    def test_free_hardware_needs_no_confirmation(self):
        """An owned GPU costs nothing, and making the user confirm $0.00 is how a real
        confirmation stops being read."""
        self._plan([{"machine_type": "local", "provider": "ssh", "avail": 4,
                     "max_single_instance": 4, "gpu_count": 1, "price_hr": 0}])
        self.pool.lease = lambda *a, **kw: (
            (True, {"options": [{"machine_type": "local", "provider": "ssh", "avail": 4,
                                 "max_single_instance": 4, "gpu_count": 1, "price_hr": 0}],
                    "scope": {"complete": True}, "errors": None})
            if a[0] == "capacity" else (True, {"lease_id": "lease_x"}))
        out = capture(self.pool.v_open, self._args())
        self.assertEqual(out["slots_open"], 2)

    def test_unpriced_machines_make_the_total_a_lower_bound(self):
        self._plan([{"machine_type": "r:p/x@on_demand", "provider": "n", "avail": 4,
                     "max_single_instance": 4, "gpu_count": 1, "price_hr": None}])
        plan = self.pool.build_plan(self._args())
        self.assertFalse(plan["cost_is_complete"])
        self.assertEqual(plan["price_confidence"], "unknown")

    def test_an_incomplete_capacity_read_marks_the_plan_untrustworthy(self):
        """A plan short on slots because one account did not answer looks exactly like a
        plan short on slots because the cards are gone. Only one of them is worth waiting
        out."""
        self._plan([{"machine_type": None, "label": "unread: acct", "avail": 0,
                     "binding_limit": "NOT CHECKED — timeout"}])
        plan = self.pool.build_plan(self._args())
        self.assertFalse(plan["plan_trustworthy"])


class PoolPlacement(TempDirCase):
    """fleet.md -> "Owned before rented" and "Slot, not machine"."""

    def setUp(self):
        super().setUp()
        self.pool = load_script("shared/pool.py")

    def _args(self, slots=4, gpu_count=1):
        return types.SimpleNamespace(
            slots=slots, gpu_count=gpu_count, gpu_memory_gb=0, arch_min=None,
            host_ram_gb=None, allow_preemptible=True, hours=1)

    def test_free_hardware_is_filled_before_anything_that_bills(self):
        self.pool.lease = lambda *a, **kw: (True, {"options": [
            {"machine_type": "cloud", "provider": "n", "avail": 8, "max_single_instance": 8,
             "gpu_count": 1, "price_hr": 3.85},
            {"machine_type": "owned", "provider": "ssh", "avail": 2, "max_single_instance": 2,
             "gpu_count": 1, "price_hr": 0},
        ], "scope": {"complete": True}, "errors": None})
        plan = self.pool.build_plan(self._args(slots=4))
        self.assertEqual(plan["picks"][0]["machine_type"], "owned")
        self.assertEqual(plan["picks"][0]["units"], 2)
        self.assertEqual(plan["usd_per_hr"], 3.85 * 2)

    def test_one_multi_gpu_box_serves_several_single_gpu_trials(self):
        """Renting eight 1-GPU boxes for eight single-GPU trials, when one 8-GPU box was
        cheaper per card and staged once, is the standard way a sweep costs triple."""
        self.pool.lease = lambda *a, **kw: (True, {"options": [
            {"machine_type": "big", "provider": "n", "avail": 8, "max_single_instance": 8,
             "gpu_count": 8, "price_hr": 30.8},
        ], "scope": {"complete": True}, "errors": None})
        plan = self.pool.build_plan(self._args(slots=8, gpu_count=1))
        self.assertEqual(plan["picks"][0]["units"], 1, "one box, not eight")
        self.assertEqual(plan["picks"][0]["slots"], 8)

    def test_a_fleet_is_never_sized_off_capacity_one_create_cannot_place(self):
        """`avail` is the total across placement domains; `max_single_instance` is what
        one create can actually be given. Sizing off the total asks for units that
        individually cannot be placed."""
        self.pool.lease = lambda *a, **kw: (True, {"options": [
            {"machine_type": "spread", "provider": "n", "avail": 40,
             "max_single_instance": 2, "gpu_count": 1, "price_hr": 1.0},
        ], "scope": {"complete": True}, "errors": None})
        plan = self.pool.build_plan(self._args(slots=10))
        self.assertLessEqual(plan["picks"][0]["units"], 2)


class ClaimedRecoveryNeedsARecord(TempDirCase):
    """CLAUDE.md -> "Never silently": 「Never let somebody's word become a
    checked fact. An answer is a `claim` until something other than the person
    confirms it」 — and `pool.py`'s own ARTIFACTS axis, where `recovered` is
    defined as 「pulled off and verified. The only state that permits destroying
    the box」.

    That definition contained the word `verified` and nothing verified anything.
    The operator typed `--artifacts recovered`, the box was destroyed, and a
    half-transferred checkpoint went with it — which is the failure `/evacuate`
    was built for, sitting on top of the one action MLClaw cannot undo.

    ‼️ Only `recovered` is gated. The other three states are honest about not
    knowing, and demanding paperwork to say 「I could not look」 would push
    people toward the one disposition that needs none — which is the opposite of
    the intent, and the shape of every safety control that gets routed around.
    """

    def setUp(self):
        super().setUp()
        self.pool = load_script("shared/pool.py")
        self.session = self.path("sess")
        self.write_json("sess/pool.json", {
            "session": self.session, "opened_at": 0, "closed_at": None,
            "shape": {}, "allow_preemptible": True, "ttl_s": 3600, "plan": {},
            "slots": [{"slot": "slot_0", "lease_id": "lease_a", "provider": "n",
                       "machine_type": "m", "price_hr": 1.0, "preemptible": True,
                       "state": "busy", "run": "run_007",
                       "history": [{"run": "run_007", "at": 0, "outcome": None}]}]})

    def _release(self, artifacts, clearance=None, outcome="ok"):
        return capture(self.pool.v_release, types.SimpleNamespace(
            session=self.session, slot="slot_0", outcome=outcome,
            artifacts=artifacts, clearance=clearance))

    def _clearance(self, verdict):
        return self.write_json("evac.json", {"clearance": {"verdict": verdict}})

    def test_recovered_without_a_clearance_is_refused(self):
        out = self._release("recovered")
        self.assertIn("error", out)
        self.assertNotIn("safe_to_destroy_the_box", out,
                         "a refusal must not also emit a release payload")
        self.assertIn("claim standing where a check belongs", out["error"])

    def test_a_blocked_clearance_does_not_permit_recovered(self):
        out = self._release("recovered", self._clearance("blocked"))
        self.assertIn("error", out)
        self.assertIn("still has work on it", out["error"])

    def test_an_undecided_clearance_does_not_permit_recovered(self):
        out = self._release("recovered", self._clearance(None))
        self.assertIn("error", out)
        self.assertIn("not decided", out["error"])

    def test_an_unreadable_clearance_is_not_a_passing_one(self):
        self.write("evac.json", "{not json")
        out = self._release("recovered", self.path("evac.json"))
        self.assertIn("error", out)
        self.assertIn("unreadable", out["error"])

    def test_a_clear_clearance_permits_it(self):
        out = self._release("recovered", self._clearance("clear"))
        self.assertTrue(out["safe_to_destroy_the_box"])

    def test_size_only_clearance_also_permits_it(self):
        """`clear_size_only` is a weaker fact that is still a fact, and it says
        so in its own name — CLAUDE.md's rule that a lower bound is reported as
        one rather than withheld."""
        out = self._release("recovered", self._clearance("clear_size_only"))
        self.assertTrue(out["safe_to_destroy_the_box"])

    def test_the_other_dispositions_need_no_paperwork(self):
        for a in ("present_unreachable", "absent", "unverifiable"):
            out = self._release(a)
            self.assertEqual(out["artifacts"], a, f"{a} should not be gated")


class PreemptionIsNotEvidence(TempDirCase):
    """fleet.md -> "A preempted trial is not a failed trial".

    The one rule in this file that breaks with nothing raising anywhere. A trial cut off
    at epoch 3 has a truncated curve and a bad final metric; recorded as an ordinary
    result it teaches the search that the *configuration* was bad, and the belief is then
    indistinguishable from a real one for the rest of the session.
    """

    def setUp(self):
        super().setUp()
        self.pool = load_script("shared/pool.py")
        self.session = self.path("sess")
        self.write_json("sess/pool.json", {
            "session": self.session, "opened_at": 0, "closed_at": None,
            "shape": {}, "allow_preemptible": True, "ttl_s": 3600, "plan": {},
            "slots": [{"slot": "slot_0", "lease_id": "lease_a", "provider": "n",
                       "machine_type": "m", "price_hr": 1.0, "preemptible": True,
                       "state": "busy", "run": "run_007",
                       "history": [{"run": "run_007", "at": 0, "outcome": None}]}]})

    def _release(self, outcome, artifacts="present_unreachable"):
        # `artifacts` became required for infrastructure outcomes when preemption
        # grew its second axis (see PreemptionHasTwoAxesNotOne). These checks are
        # about the EVIDENCE axis, which is unchanged — so they supply a
        # disposition and go on testing what they were testing.
        #
        # The disposition is deliberately NOT `recovered`: that one now requires
        # an `/evacuate` clearance (see ClaimedRecoveryNeedsARecord), and a check
        # about preemption should not have to stand up an evacuation to say so.
        # Picking one that needs no paperwork is also the honest reading — these
        # fixtures never pulled anything off a box.
        return capture(self.pool.v_release, types.SimpleNamespace(
            session=self.session, slot="slot_0", outcome=outcome,
            artifacts=artifacts, clearance=None))

    def test_preempted_is_flagged_as_not_evidence(self):
        out = self._release("preempted")
        self.assertFalse(out["trial_counts_as_evidence"])
        self.assertIn("do NOT", out["note"])

    def test_a_crash_is_still_the_model_s_own_outcome(self):
        """A crash is the code's, not the infrastructure's — an OOM at the chosen batch
        size is real information about the hypothesis and must stay countable."""
        self.assertTrue(self._release("crashed")["trial_counts_as_evidence"])
        self.assertTrue(self._release("ok")["trial_counts_as_evidence"])

    def test_releasing_a_slot_does_not_release_its_lease(self):
        """The next trial wants that box, already provisioned and already staged. A pool
        that tears down between trials pays for both, every time."""
        called = []
        self.pool.lease = lambda *a, **kw: called.append(a) or (True, {})
        self._release("ok")
        self.assertEqual(called, [], "release is a pool-side state change only")
        self.assertEqual(self.read_json("sess/pool.json")["slots"][0]["state"], "free")


class StorageIsTheSecondMeter(TempDirCase):
    """`skills/lease/references/contract.md` -> "Storage is the second meter".

    Compute is the loud meter and it is the one that stops by itself: a dead-man switch
    fires, the instance halts, the large number goes to zero. Storage is the quiet one —
    it starts at create, it does not stop when the box stops, and it survives the box
    being deleted. A `reap` that counted only instances answered "nothing is running",
    which is true, standing next to a volume that has billed every hour since.

    The failure has no error in it anywhere. It is a smaller number than the truth,
    printed with the same confidence as the right one.
    """

    def setUp(self):
        super().setUp()
        self.lease = load_script("lease/lease.py")
        self.common = load_script("lease/_common.py")
        self.res = self.path("resources.json")
        self.write_json("resources.json", {"compute": {}, "servers": {}})

    # --- the envelope ---------------------------------------------------------

    def test_never_looked_and_looked_and_found_none_are_different_payloads(self):
        """One keystroke apart in an adapter, a world apart in a bill."""
        never = self.common.sweep_result([], checked=["a"])
        looked = self.common.sweep_result([], checked=["a"], storage=[])
        self.assertNotIn("storage", never,
                         "an adapter that passed nothing must not emit `storage: []` — "
                         "that is the sentence 'I looked and there is none'")
        self.assertIn("storage", looked)
        self.assertFalse(self.common.sweep_storage_known(never))
        self.assertTrue(self.common.sweep_storage_known(looked))

    def test_an_adapter_that_never_looked_makes_the_sweep_incomplete(self):
        """Same treatment as a project whose list errored, and for the same reason: the
        answer covers less than it appears to. Anything else lets `reap` print a residual
        total for a category nobody enumerated."""
        self.lease.call = lambda name, res, verb, *extra: (True, {
            "units": [], "scope": {"complete": True, "checked": ["all"], "unreached": []}})
        _, _, scope, _ = self.lease.collect(["blind"], self.res, "sweep")
        self.assertFalse(scope["complete"])
        self.assertEqual([u["scope"] for u in scope["unreached"]], ["storage"])

    def test_an_adapter_that_looked_and_found_none_stays_complete(self):
        """Owned hardware is this case and it is the common one — the disk was bought,
        so releasing a claim accrues nothing. Marking it incomplete would make every
        sweep of a laptop-plus-one-server workspace a lower bound forever, and a warning
        that is always on is a warning nobody reads."""
        self.lease.call = lambda name, res, verb, *extra: (True, {
            "units": [], "storage": [],
            "scope": {"complete": True, "checked": ["all"], "unreached": []}})
        _, _, scope, storage = self.lease.collect(["ssh"], self.res, "sweep")
        self.assertTrue(scope["complete"])
        self.assertEqual(storage, [])

    def test_storage_rows_carry_provider_injected_by_l2(self):
        self.lease.call = lambda name, res, verb, *extra: (True, {
            "units": [], "storage": [{"storage_id": "d1", "attached_to": None}],
            "scope": {"complete": True, "checked": [], "unreached": []}})
        _, _, _, storage = self.lease.collect(["p"], self.res, "sweep")
        self.assertEqual(storage[0]["provider"], "p",
                         "adapter identity is L2's knowledge for storage as for units")

    # --- which side of the API was believed -----------------------------------

    def test_attachment_is_read_from_the_side_that_carries_an_id(self):
        """The rule this check exists for was written backwards first, and a live account
        proved it: reading the instance side returned `attached_to: null` for all 20 disks,
        including the boot disks of running boxes. A managed disk is named — not id'd — on
        the instance, while the disk itself carries `read_write_attachment`."""
        neb = load_script("lease/provider_nebius.py")
        disk = {"status": {"read_write_attachment": "computeinstance-x",
                           "managed_by": "computeinstance-x"}}
        self.assertEqual(neb.volume_attachment(disk),
                         ("computeinstance-x", "read_write_attachment"))
        self.assertEqual(neb.volume_attachment({"status": {"state": "READY"}}), (None, None))

    def test_a_name_only_declaration_still_links_but_says_it_is_a_name(self):
        """Allowed, because the API offers nothing else — labelled, because a rename
        breaks it and the break produces a FALSE ORPHAN, which is the direction that
        proposes a deletion."""
        neb = load_script("lease/provider_nebius.py")
        names = neb.instance_volume_names([{
            "metadata": {"id": "computeinstance-x"},
            "spec": {"boot_disk": {"managed_disk": {"name": "box-boot"}}}}])
        self.assertEqual(names, {"box-boot": "computeinstance-x"})

    # --- what counts as abandoned --------------------------------------------

    def test_a_volume_with_nothing_attached_is_an_orphan(self):
        out = self.lease.storage_orphans(
            [{"storage_id": "d1", "attached_to": None, "tag": ""}], set(), set())
        self.assertEqual(len(out), 1)
        self.assertIn("unattached", out[0]["orphan_reason"])

    def test_a_volume_on_a_live_instance_is_not_an_orphan(self):
        out = self.lease.storage_orphans(
            [{"storage_id": "d1", "attached_to": "i-1", "tag": ""}], set(), {"i-1"})
        self.assertEqual(out, [])

    def test_a_volume_still_attached_to_a_closed_lease_is_an_orphan(self):
        """The one that hides. The instance row was released, the ledger agrees, and the
        disk it declared goes on billing because nothing asked the storage list about it."""
        out = self.lease.storage_orphans(
            [{"storage_id": "d1", "attached_to": "i-dead", "tag": ""}], set(), set())
        self.assertEqual(len(out), 1)
        self.assertIn("no open lease", out[0]["orphan_reason"])

    def test_a_volume_under_an_open_lease_is_left_alone(self):
        out = self.lease.storage_orphans(
            [{"storage_id": "d1", "attached_to": None, "tag": "mlclaw-live"}],
            {"mlclaw-live"}, set())
        self.assertEqual(out, [], "a lease is still open; the volume is in use by design")

    # --- what the total is allowed to claim -----------------------------------

    def test_an_unpriced_row_is_a_missing_term_not_a_free_one(self):
        total, unpriced = self.lease.usd_hr(
            [{"price_hr": 2.5}, {"price_hr": None}])
        self.assertEqual(total, 2.5)
        self.assertEqual(unpriced, 1,
                         "folding a null in as 0 is how a total reads as complete while "
                         "missing its largest term")

    def test_reap_reports_the_two_meters_apart_and_says_when_the_total_is_short(self):
        self.lease.providers = lambda _res: ["p"]
        self.lease.collect = lambda names, res, verb, *extra: (
            [{"instance_id": "i-1", "tag": "mlclaw-x", "price_hr": 3.0}],
            {}, {"complete": True, "checked": [], "unreached": []},
            [{"storage_id": "d1", "attached_to": None, "tag": "", "price_hr": None}])
        out = capture(self.lease.v_reap,
                      types.SimpleNamespace(res=self.res, tag_prefix="mlclaw-", billing_only=False))
        self.assertEqual(len(out["orphan_storage"]), 1,
                         "an unattached volume is a finding, not a footnote")
        self.assertEqual(out["compute_usd_per_hr"], 3.0)
        self.assertEqual(out["storage_usd_per_hr"], 0.0)
        self.assertTrue(out["total_is_lower_bound"],
                        "the volume's rate is unknown, so the total is short by an "
                        "unknown amount and must say so")


class OwnershipIsNeverTheClock(unittest.TestCase):
    """`skills/lease/references/contract.md` -> "Ownership on a shared account".

    Only bites on a sweep scoped wider than this tool's own tag — but that is exactly the
    sweep somebody runs to ask "what is burning money", and on a shared account the answer
    contains other people's machines. Measured on a live account while writing this: six
    running boxes, four the user's and two colleagues' including one at $30.80/hr. Read
    without attribution, all six are candidates.
    """

    def setUp(self):
        self.neb = load_script("lease/provider_nebius.py")

    def test_a_known_creator_is_stamped_with_where_it_came_from(self):
        rows = [{"instance_id": "i-1"}]
        self.neb.attribute(rows, {"i-1": {"actor": "someone@example.com", "at": "T"}})
        self.assertEqual(rows[0]["operator"], "someone@example.com")
        self.assertEqual(rows[0]["operator_status"], "audit_create")

    def test_outside_the_window_is_unknown_and_says_which_kind_of_unknown(self):
        """`null` here means LOOKED AND DID NOT FIND. It must not read as unowned, and it
        must never read as yours — that reading is how a colleague's box gets reaped."""
        rows = [{"instance_id": "i-old"}]
        self.neb.attribute(rows, {})
        self.assertIsNone(rows[0]["operator"])
        self.assertEqual(rows[0]["operator_status"], "no_create_event_in_window")

    def test_nobody_asked_leaves_no_keys_at_all(self):
        """The third state. A row with no `operator` key was never attributed, which is a
        different sentence from one attributed and not found — same three-way split the
        storage key uses, for the same reason."""
        rows = [{"instance_id": "i-1"}]
        self.assertNotIn("operator", rows[0])

    def test_storage_is_attributed_on_its_own_id_not_its_instance(self):
        """A volume outlives its box, so its creator is its own — joining through an
        instance that no longer exists would attribute exactly the abandoned volumes this
        layer exists to find to nobody."""
        rows = [{"storage_id": "d-1"}]
        self.neb.attribute(rows, {"d-1": {"actor": "owner@example.com", "at": "T"}})
        self.assertEqual(rows[0]["operator"], "owner@example.com")


class AnAuditEventIsParsedDeepEnough(unittest.TestCase):
    """fleet.md -> "Group by id, never by name".

    That rule is enforced by recording a rename as an alias rather than as a second
    machine — and the alias list can only be built from the event's NAME. Read against a
    live log, every event came back `name: None`, because identity sits at
    `resource.metadata.{id,name}` and the parse read `resource.{id,name}`, one level too
    shallow.

    Nothing failed. The id survived on a regex fallback, so the verdicts looked right;
    what was silently dead was every feature built on the name — `--name` matched
    nothing, and `aka` was always empty, which means the rename tracking the contract
    makes a point of was decorative. A check that would have caught it is one that
    asserts the real payload shape, not a hand-made flat one.
    """

    def setUp(self):
        self.neb = load_script("lease/provider_nebius.py")

    # The shape the provider actually returns, trimmed to the fields that matter.
    EVENT = {
        "type": "ai.nebius.compute.computeinstance.create",
        "action": "CREATE", "time": "2026-08-17T16:51:25.560650797Z",
        "status": {"code": "DONE"},
        "authentication": {"subject": {"tenant_user_id": "tenantuseraccount-e00a",
                                       "name": "someone@example.com"}},
        "resource": {
            "metadata": {"id": "computeinstance-e00r", "name": "box9dof-e135yqh",
                         "type": "computeinstance"},
            "state": {"current": {"metadata": {
                "id": "computeinstance-e00r", "name": "box9dof-e135yqh",
                "labels": {"mlclaw_tag": "mlclaw-lease_x"}}}}},
    }

    def test_the_name_is_found_where_the_provider_actually_puts_it(self):
        row = self.neb._audit_row(self.EVENT)
        self.assertEqual(row["name"], "box9dof-e135yqh",
                         "a null name silently disables rename tracking and --name")
        self.assertEqual(row["instance_id"], "computeinstance-e00r")

    def test_the_id_does_not_depend_on_the_regex_fallback(self):
        """The fallback is a last resort for events with no resource block. Leaning on it
        is what let the shallow read go unnoticed."""
        row = self.neb._audit_row(
            {**self.EVENT, "resource": {"metadata": {"id": "computedisk-e00x",
                                                     "name": "box-boot"}}})
        self.assertEqual(row["instance_id"], "computedisk-e00x",
                         "a disk event must key on the disk, not on whichever instance "
                         "id happens to appear elsewhere in the payload")

    def test_the_tag_is_read_from_the_resource_state_not_only_the_request(self):
        self.assertEqual(self.neb._audit_row(self.EVENT)["tag"], "mlclaw-lease_x")

    def test_a_rename_becomes_an_alias_rather_than_a_second_machine(self):
        """The end-to-end point of the parse. One id, two names over its life: the later
        name is reported and the earlier kept as `aka`. Grouped by name instead, this is
        two machines and the live one reads as released."""
        rows = [self.neb._audit_row({**self.EVENT, "time": t, "action": a,
                                     "resource": {"metadata": {
                                         "id": "computeinstance-e00r", "name": n}}})
                for t, a, n in (("2026-08-17T10:00:00Z", "CREATE", "aquamarine-parakeet-8"),
                                ("2026-08-17T11:00:00Z", "UPDATE", "scene-gen"))]
        verdict = self.neb._verdict(rows)
        self.assertEqual(verdict["name"], "scene-gen")
        self.assertEqual(verdict["aka"], ["aquamarine-parakeet-8"])


class EveryAdapterDeclaresItsLimits(unittest.TestCase):
    """`skills/lease/references/contract.md` -> "The eight verbs", "What every
    adapter declares", "Shape resolution: requirements → machine type is a lookup, not a
    translation".

    Written across ALL adapters rather than against the one that exists today, because
    the skill's promise is that a new provider is one new file — and a promise like that
    decays by the second file, not the tenth. Everything here runs with no network and no
    credential: it reads what each adapter DECLARES, which is exactly the part a caller
    trusts before it has ever made a call.
    """

    LEASE_DIR = os.path.join(REPO_ROOT, "scripts", "lease")
    VERBS = ("capacity", "up", "addr", "state", "down", "renew", "sweep", "history")

    @classmethod
    def adapters(cls):
        return sorted(f for f in os.listdir(cls.LEASE_DIR)
                      if f.startswith("provider_") and f.endswith(".py"))

    def test_there_is_more_than_one_adapter(self):
        """The contract's claims about provider-blindness are untested by one adapter:
        anything provider-specific that leaked upward is invisible while there is nothing
        to disagree with it."""
        self.assertGreaterEqual(len(self.adapters()), 2)

    def test_every_adapter_registers_every_verb(self):
        """A missing verb is not a degraded adapter, it is a crash at the moment L2 needs
        it — and `history` is the one that goes missing, because it is the only verb a
        working single-box flow never calls."""
        for fname in self.adapters():
            for verb in self.VERBS:
                with self.subTest(adapter=fname, verb=verb):
                    rc, _, err = run_script(f"lease/{fname}", verb, "--help")
                    self.assertEqual(rc, 0, f"{fname} does not accept `{verb}`: {err[:200]}")

    def test_every_table_declares_its_capabilities(self):
        required = {"credential_ttl_s", "credential_refreshes", "native_ttl",
                    "image_bake", "tags", "billing_granularity_s"}
        for fname in sorted(os.listdir(self.LEASE_DIR)):
            if not (fname.startswith("machines_") and fname.endswith(".json")):
                continue
            with self.subTest(table=fname), open(os.path.join(self.LEASE_DIR, fname),
                                                 encoding="utf-8") as fh:
                caps = json.load(fh).get("capabilities") or {}
                self.assertEqual(required - set(caps), set(),
                                 "a caller reads these before it can size a job or "
                                 "trust an expiry")

    def test_every_shape_row_declares_arch_and_how_sure_it_is(self):
        """`arch` is a hard admission check, not metadata: a wrong value either refuses a
        machine that would have worked or admits one whose kernels will not load. An
        adapter whose table omits it disables the check silently."""
        for fname, rows in self._shape_rows():
            for name, spec in rows.items():
                with self.subTest(table=fname, row=name):
                    self.assertTrue(spec.get("arch"))
                    self.assertIn(spec.get("arch_status"), ("verified", "claim"))

    def test_no_price_field_anywhere_is_zero(self):
        """Zero is the correct price for owned hardware and must not also mean 'nobody
        wrote it down' — one reading makes a fleet estimate silently omit whole machine
        types while presenting as a complete bill."""
        for fname, rows in self._shape_rows(include_storage=True):
            for name, spec in rows.items():
                for key, value in spec.items():
                    if not key.startswith("price_") or key in ("price_status", "price_asof"):
                        continue
                    with self.subTest(table=fname, row=name, field=key):
                        self.assertNotEqual(value, 0)
                        if value is not None:
                            self.assertIn(spec.get("price_status"), ("verified", "claim"))
                            if spec.get("price_status") == "claim":
                                self.assertTrue(spec.get("price_asof"),
                                                "a claimed price with no date cannot be "
                                                "judged stale")

    def _shape_rows(self, include_storage=False):
        """(filename, {row_name: spec}) over every machine table. The shapes dict is
        named per provider (`platforms`, `instance_types`), so this walks whichever
        top-level dicts hold row-shaped values rather than pinning one key name."""
        out = []
        for fname in sorted(os.listdir(self.LEASE_DIR)):
            if not (fname.startswith("machines_") and fname.endswith(".json")):
                continue
            with open(os.path.join(self.LEASE_DIR, fname), encoding="utf-8") as fh:
                table = json.load(fh)
            for key, block in table.items():
                if key in ("capabilities", "defaults", "_comment"):
                    continue
                if key == "storage" and not include_storage:
                    continue
                rows = {k: v for k, v in block.items()
                        if not k.startswith("_") and isinstance(v, dict)}
                if rows:
                    out.append((f"{fname}:{key}", rows))
        return out


class TableProvenance(unittest.TestCase):
    """`skills/lease/references/contract.md` -> "Shape resolution: requirements → machine type is a lookup, not a translation".

    `arch` decides whether a job's kernels load at all, and price is what a human is
    quoted. Both are hand-written here, and a hand-written value that reads like a
    measured one is the failure — not the hand-writing.
    """

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "scripts", "lease",
                               "machines_nebius.json")) as fh:
            self.table = json.load(fh)

    def test_every_platform_declares_arch_and_how_sure_it_is(self):
        for name, spec in self.table["platforms"].items():
            with self.subTest(platform=name):
                self.assertTrue(spec.get("arch"), "an adapter whose table omits arch "
                                                  "disables the compatibility check")
                self.assertIn(spec.get("arch_status"), ("verified", "claim"))

    def test_an_unknown_price_is_null_and_never_zero(self):
        """Zero is the correct price for owned hardware. It must not also mean 'nobody
        wrote it down', or a fleet estimate silently omits whole machine types."""
        for name, spec in self.table["platforms"].items():
            with self.subTest(platform=name):
                self.assertNotEqual(spec.get("price_gpu_hr"), 0)
                if spec.get("price_gpu_hr") is not None:
                    self.assertEqual(spec.get("price_status"), "claim")
                    self.assertTrue(spec.get("price_asof"),
                                    "a price with no date cannot be judged stale")

    def test_the_table_does_not_duplicate_what_the_api_serves(self):
        """gpu_count / vCPU / host RAM / GPU memory all come back live from
        resource-advice. A second copy here is a second author, and the stale one is the
        one that gets read."""
        served_live = {"gpu_count", "vcpu", "host_ram_gb", "gpu_memory_gb", "preset"}
        for name, spec in self.table["platforms"].items():
            with self.subTest(platform=name):
                self.assertEqual(served_live & set(spec), set())

    def test_no_infrastructure_id_is_pinned_anywhere_in_the_layer(self):
        """Ids drift, and a pinned one points somewhere wrong in silence — the first trap
        in "Scope completeness". It is also the rule that keeps private infrastructure
        out of a repo that is not the never-committed file."""
        pattern = ("tenant-e0", "project-e0", "project-u0", "project-i0",
                   "vpcsubnet-", "vpcnetwork-", "computeinstance-e0", "useraccount-e0")
        targets = [os.path.join(REPO_ROOT, "scripts", "lease", f)
                   for f in sorted(os.listdir(
                       os.path.join(REPO_ROOT, "scripts", "lease")))
                   if f.endswith((".py", ".json"))]
        targets += [os.path.join(REPO_ROOT, "scripts", "shared", "pool.py"),
                    os.path.join(REPO_ROOT, "lifecycle", "resources.json"),
                    os.path.join(REPO_ROOT, "references", "fleet.md")]
        for path in targets:
            rel = os.path.relpath(path, REPO_ROOT)
            with self.subTest(file=rel):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for needle in pattern:
                    self.assertNotIn(needle, text,
                                     f"{rel} pins {needle}… — discover it at call time")


class PreemptionHasTwoAxesNotOne(unittest.TestCase):
    """fleet.md -> "A preempted trial is not a failed trial", and the half that
    rule does not cover.

    `outcome` answers "is this trial evidence about its hypothesis". It does not
    answer "did the work survive", and on a real fleet those came apart: of ten
    preempted L40S boxes, four held weights reachable only by attaching the volume
    elsewhere, and three probably held nothing — but that could not be checked,
    because starting the box back up hit the same tenure wall that preempted it.
    All seven were `--outcome preempted` and the record could not tell them apart.

    ‼️ `unverifiable` is not `absent`. A tenure wall produces the first while it
    reads like the second — the same distinction `census.py` keeps between a
    location that did not answer and a directory that is genuinely empty, and the
    one `/repro` keeps between an unprobed axis and an intact one.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlclaw_pool_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pool = load_script("shared/pool.py")

    def run_pool(self, *args):
        return run_script("shared/pool.py", *args)

    def opened(self):
        rc, out, err = self.run_pool("open", "--session", self.tmp, "--slots", "1",
                                     "--provider", "ssh", "--machine-type", "local")
        return rc, out, err

    def test_the_vocabulary_keeps_unverifiable_apart_from_absent(self):
        self.assertIn("unverifiable", self.pool.ARTIFACTS)
        self.assertIn("absent", self.pool.ARTIFACTS)
        self.assertIn("NEVER", self.pool.ARTIFACTS["unverifiable"],
                      "the vocabulary entry has to carry the rule, because the two "
                      "words are one keystroke apart and a world apart in a bill")

    def test_only_recovered_permits_destroying_the_box(self):
        """`present_unreachable` is the state that most reads like done: the weights
        exist, they are simply not here yet. Releasing the lease deletes the disk."""
        safe = [k for k, v in self.pool.ARTIFACTS.items() if "permits destroying" in v]
        self.assertEqual(safe, ["recovered"])

    def test_an_infrastructure_outcome_cannot_be_recorded_without_a_disposition(self):
        """Refused BEFORE any state is read, which is both the right order for a
        usage error and what makes the guard checkable without a live pool."""
        rc, out, err = self.run_pool("release", "--session", self.tmp, "--slot", "0",
                                     "--outcome", "preempted")
        self.assertNotEqual(rc, 0)
        blob = json.dumps(out) if not isinstance(out, str) else out
        self.assertIn("--artifacts", blob + err)
        self.assertNotIn("no slot", blob + err,
                         "a usage error must not be reported as a state error")

    def test_crashed_carries_the_same_requirement(self):
        rc, out, err = self.run_pool("release", "--session", self.tmp, "--slot", "0",
                                     "--outcome", "crashed")
        self.assertNotEqual(rc, 0)
        self.assertIn("--artifacts", (json.dumps(out) if not isinstance(out, str) else out) + err)

    def test_a_normal_release_is_not_burdened(self):
        """Friction belongs only where the box may be going away with work on it.
        Universal friction is the kind that gets routed around."""
        rc, out, err = self.run_pool("release", "--session", self.tmp, "--slot", "0",
                                     "--outcome", "ok")
        blob = (json.dumps(out) if not isinstance(out, str) else out) + err
        self.assertNotIn("--artifacts", blob,
                         "an ok release must fail on the missing pool, not on artifacts")


class WhatARoundCostIsAFloorUnlessEverythingWasPriced(unittest.TestCase):
    """CLAUDE.md -> "Never silently" — its "Never report data you could not look at"
    rule, applied to money.

    `status` answers what is burning now; `cost` answers what a round has cost,
    which is the question that was actually asked — repeatedly — across a six-day
    search. It is a record somebody reads later to decide whether to keep renting
    or buy a box, and the way it goes wrong raises nothing: `--price-hr` is
    optional on `up`, so a ledger routinely holds unpriced rows, and a total
    summed over the priced ones alone reads exactly like the whole bill.

    Same shape as a census with `complete: false`. A round that rented ten boxes
    and priced six does not have a cost; it has a floor and four unknowns.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlclaw_cost_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lease = load_script("lease/lease.py")
        self.res = os.path.join(self.tmp, "resources.json")
        with io.open(self.res, "w", encoding="utf-8") as fh:
            json.dump({"compute": {"nebius": {}}}, fh)

    def ledger(self, *rows):
        now = int(time.time())
        out = []
        for i, r in enumerate(rows):
            out.append({"lease_id": r.get("id", f"L{i}"), "provider": "nebius",
                        "tag": r.get("tag", "t"), "machine_type": "H100",
                        "project": r.get("project", "p"), "run": None,
                        "price_hr": r.get("price_hr"), "ttl_s": 9999,
                        "requested_at": now - r.get("held_h", 1) * 3600,
                        "released_at": None if r.get("open") else now,
                        "state": "held" if r.get("open") else "released",
                        "instance_ids": ["i"], "error": None})
        with io.open(os.path.join(self.tmp, "leases.json"), "w", encoding="utf-8") as fh:
            json.dump({"leases": out}, fh)
        return now

    def cost(self, **kw):
        args = types.SimpleNamespace(res=self.res, project=kw.get("project"),
                                     tag=kw.get("tag"),
                                     since_epoch=kw.get("since_epoch"))
        return capture(self.lease.v_cost, args)

    def test_an_unpriced_lease_is_excluded_and_the_total_says_so(self):
        self.ledger({"price_hr": 3.0, "held_h": 2}, {"price_hr": None, "held_h": 10})
        out = self.cost()
        self.assertEqual(out["usd"], 6.0)
        self.assertEqual(out["unpriced"], 1)
        self.assertFalse(out["complete"],
                         "a total over some of the rows is a floor, not the bill")

    def test_the_lower_bound_is_stated_in_words_not_only_in_a_flag(self):
        """A `complete: false` a reader can skip past is how a floor gets quoted as a
        bill. The same reason `census.py` spells out what a partial scan means."""
        self.ledger({"price_hr": 3.0}, {"price_hr": None})
        out = self.cost()
        blob = " ".join(str(v) for v in out.values())
        self.assertIn("LOWER BOUND", blob)

    def test_a_fully_priced_round_is_complete(self):
        self.ledger({"price_hr": 3.0, "held_h": 2}, {"price_hr": 1.0, "held_h": 1})
        out = self.cost()
        self.assertTrue(out["complete"])
        self.assertEqual(out["usd"], 7.0)
        self.assertNotIn("\u203c\ufe0f", out)

    def test_unpriced_hours_are_reported_so_the_gap_has_a_size(self):
        """"Four rows unpriced" does not say whether the gap is a rounding error or
        larger than the total. The machine-hours behind it do."""
        self.ledger({"price_hr": 3.0, "held_h": 1}, {"price_hr": None, "held_h": 40})
        out = self.cost()
        self.assertEqual(out["gpu_hours_unpriced"], 40.0)

    def test_an_open_lease_is_counted_to_now_and_flagged(self):
        self.ledger({"price_hr": 2.0, "held_h": 3, "open": True})
        out = self.cost()
        self.assertEqual(out["usd"], 6.0)
        self.assertEqual(len(out["still_open"]), 1)
        self.assertIn("accruing", out["note"])

    def test_the_window_admits_that_it_includes_provisioning(self):
        """The ledger's only start stamp is when the lease was ASKED FOR; billing
        starts when the box comes up. One machine type took ~80 minutes to
        provision on the round this was built for — a systematic over-count in a
        direction nobody can see unless the report says so."""
        self.ledger({"price_hr": 1.0})
        out = self.cost()
        self.assertIn("provisioning", out["window"])

    def test_the_total_cannot_diverge_from_the_shared_partition(self):
        """`usd_hr` and `usd_over` are one rule at two quantities. Written twice it
        gets fixed once, which is why they share `_priced`."""
        rows = [{"price_hr": 2.0}, {"price_hr": None}]
        rate, un_a = self.lease.usd_hr(rows)
        total, un_b = self.lease.usd_over(rows, lambda r: 3.0)
        self.assertEqual((rate, un_a), (2.0, 1))
        self.assertEqual((total, un_b), (6.0, 1))

    def test_filtering_by_project_does_not_silently_widen(self):
        self.ledger({"price_hr": 1.0, "project": "p"},
                    {"price_hr": 100.0, "project": "other"})
        out = self.cost(project="p")
        self.assertEqual(out["leases"], 1)
        self.assertEqual(out["usd"], 1.0)


class TheOnlyLambdaRentalPathRefusesAnImmortalBox(unittest.TestCase):
    """`skills/lease/references/contract.md` -> Money rule 3; `provider_lambda`
    docstring -> "The three places this provider is not Nebius" (3).

    These two assertions were free while a second implementation existed: the global
    `lambda_server` skill rented boxes with its own `curl`, so a regression here cost one
    provider, not the ability to rent. That skill is now a shell over this adapter, which
    makes the refusal below **the only thing standing between `lambda.sh up` and a GPU
    that bills forever** — the shell has no TTL of its own to fall back on, by design.

    Both run with no network and no credential: what is checked is that the refusal
    happens BEFORE the launch call, which is the only ordering that does not cost money
    to discover.
    """

    def setUp(self):
        self.lam = load_script("lease/provider_lambda.py")

    def _args(self, **kw):
        base = {"res": None, "machine_type": "us-west-1:gpu_1x_a10", "ttl_s": 3600,
                "tag": "mlclaw-lease_x", "run": None, "project": None,
                "gpu_count": None, "gpu_memory_gb": None, "arch_min": None,
                "host_ram_gb": None}
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_up_refuses_before_it_launches_when_nothing_can_expire(self):
        """A box that exists with no switch is already the failure. Discovering the
        missing credential after the create call means paying for the discovery."""
        reached = []
        self.lam.conf = lambda _res: {"api_base": "https://example.invalid/api/v1",
                                      "api_key": "k", "ssh_key_names": ["kn"]}
        self.lam.api = lambda *a, **kw: reached.append(a) or ({}, None)
        out = capture(self.lam.v_up, self._args())
        self.assertEqual(out["error"], "permission")
        self.assertEqual(reached, [],
                         "the launch endpoint was called before the switch was checked")
        self.assertIn("expire", out["detail"],
                      "the refusal must say what is missing is the ability to expire, "
                      "not merely that a config key is unset")

    def test_nothing_provider_side_can_expire_the_box_either(self):
        """Why the refusal above cannot be softened into a warning. The refusal is only
        the right behaviour while BOTH other switches are absent, and both absences are
        declared rather than assumed: no provider-side TTL, and — the inverted one — no
        stopped state, so the guest-side `shutdown -h` the contract offers as a fallback
        elsewhere stops no billing here and removes the last way in. If a future API grows
        either one, this test fails and the refusal should be revisited; that is the point
        of pinning it rather than pinning only the refusal."""
        caps = self.lam.table()["capabilities"]
        self.assertFalse(caps["native_ttl"],
                         "a provider-side TTL would make the dead-man optional")
        self.assertNotIn("stopped", set(self.lam.STATE_MAP.values()),
                         "a stopped state would mean halting the guest pauses the meter, "
                         "which is what makes shutdown a usable fallback elsewhere")

    def test_out_of_stock_and_does_not_exist_are_different_rows(self):
        """The defect the merge retired. The standalone skill filtered
        `regions_with_capacity_available | length > 0`, so a card Lambda sells but has no
        stock of rendered identically to a card Lambda does not sell — and a capacity
        conclusion reached that way is how the L40S call went wrong on the other provider.
        """
        known = sorted(self.lam.table()["instance_types"])[0]
        self.lam.conf = lambda _res: {"api_base": "x", "api_key": "k"}
        self.lam.api_or_die = lambda _cfg, _path, **kw: {"data": {
            known: {"instance_type": {"name": known, "price_cents_per_hour": 75,
                                      "specs": {"gpus": 1, "memory_gib": 200}},
                    "regions_with_capacity_available": []}}}
        rows = capture(self.lam.v_capacity, self._args())
        self.assertEqual(len(rows), 1, "an out-of-stock type must still report a row")
        self.assertEqual(rows[0]["avail"], 0)
        self.assertIsNone(rows[0]["machine_type"],
                          "unusable right now, so there must be nothing for a caller to "
                          "pass to `up`")
        self.assertIn("stock", (rows[0]["binding_limit"] or "").lower(),
                      "the row has to say WHY it is zero, or it reads as a card that "
                      "does not exist")

    def test_a_type_the_table_has_never_seen_is_named_not_dropped(self):
        """The other half of the same rule, in the other direction: the provider sells
        something this repo has not been taught. It cannot pass the `arch` check, so it is
        unusable — but silently omitting it reads as "Lambda does not sell that card"."""
        self.lam.conf = lambda _res: {"api_base": "x", "api_key": "k"}
        self.lam.api_or_die = lambda _cfg, _path, **kw: {"data": {
            "gpu_1x_notinthetable": {
                "instance_type": {"name": "gpu_1x_notinthetable",
                                  "price_cents_per_hour": 999,
                                  "specs": {"gpus": 1, "memory_gib": 200}},
                "regions_with_capacity_available": [{"name": "us-west-1"}]}}}
        rows = capture(self.lam.v_capacity, self._args())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["avail"], 0)
        self.assertIn("machines_lambda.json", rows[0]["binding_limit"],
                      "the row must name the file that has to be taught")

    def test_price_is_read_live_and_says_so(self):
        """On this provider the price is a measurement, so `price_status` must be
        `verified`. A hand-copied table value would be a second author for a number that
        has one — and `claim` is what a caller reads before trusting a cost estimate."""
        known = sorted(self.lam.table()["instance_types"])[0]
        self.lam.conf = lambda _res: {"api_base": "x", "api_key": "k"}
        self.lam.api_or_die = lambda _cfg, _path, **kw: {"data": {
            known: {"instance_type": {"name": known, "price_cents_per_hour": 75,
                                      "specs": {"gpus": 1, "memory_gib": 200}},
                    "regions_with_capacity_available": [{"name": "us-west-1"}]}}}
        row = [r for r in capture(self.lam.v_capacity, self._args())
               if r["machine_type"]][0]
        self.assertEqual(row["price_hr"], 0.75)
        self.assertEqual(row["price_status"], "verified")


class ReapIsWiredAndScopedToWhatBills(unittest.TestCase):
    """CLAUDE.md -> "On Conversation Start", and fleet.md -> "The two questions, and
    why one list cannot answer both".

    `reap` is the only conversation-start check that leaves the machine, and the only
    one whose local record is untrustworthy by construction: the session that opened the
    fleet is the one that died. What earns it the network call is money accruing right
    now -- and what keeps that from becoming step 4's rejected `census.py scan` is
    `--billing-only`. Without the flag `provider_ssh` self-registers off `servers` and
    the automatic check ssh's every owned box before the user has said a word.
    """

    RES = os.path.join(REPO_ROOT, "scripts", "lease", "lease.py")

    def _reap(self, compute, servers=None, *flags):
        with tempfile.TemporaryDirectory() as d:
            res = os.path.join(d, "resources.json")
            with open(res, "w", encoding="utf-8") as fh:
                json.dump({"compute": compute, "servers": servers or {}}, fh)
            rc, out, err = run_script(os.path.join("lease", "lease.py"),
                                      "--resources", res, "reap", *flags)
            self.assertIsInstance(out, dict, f"reap did not emit JSON: {out!r} {err}")
            return out

    def test_billing_only_skips_owned_hardware_and_says_that_it_did(self):
        """Silently narrowing the sweep would make `orphans: []` mean less than it reads."""
        out = self._reap({}, {"box": {"host": "h"}}, "--billing-only")
        self.assertEqual(out["skipped_non_billing"], ["ssh"])
        self.assertEqual(out["orphans"], [])

    def test_without_the_flag_owned_hardware_is_in_scope(self):
        """The unfiltered verb still answers "any forgotten boxes" -- a held ssh claim
        is a real problem, just not a money one. Asserted on the declared scope rather
        than by letting the check ssh anywhere."""
        mod = load_script(os.path.join("lease", "lease.py"))
        self.assertIn("ssh", mod.NON_BILLING)
        self.assertNotIn("nebius", mod.NON_BILLING)
        self.assertNotIn("lambda", mod.NON_BILLING)

    def test_a_compute_key_with_no_adapter_is_named_not_dropped(self):
        """`providers()` intersects, so a typo disappears and the caller keeps believing
        the provider is registered. Unattended, that reads as "nothing is billing" every
        session, forever, about an account that is."""
        out = self._reap({"nebius_prod": {}, "_comment": "x"}, None, "--billing-only")
        self.assertEqual(out["compute_without_adapter"], ["nebius_prod"],
                         "a mistyped compute key must be named, never silently dropped")

    def test_claude_md_still_calls_it_with_the_flag(self):
        """The wiring is the contract; it regressed to prose once already."""
        with open(os.path.join(REPO_ROOT, "CLAUDE.md"), encoding="utf-8") as fh:
            claude = fh.read()
        self.assertIn("reap --billing-only", claude,
                      "conversation-start step 5 must call reap, and with the flag")
        self.assertNotIn("still not wired", claude)


if __name__ == "__main__":
    unittest.main()
