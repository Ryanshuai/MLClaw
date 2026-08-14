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
  `.claude/skills/lease/references/contract.md` -> "Scope completeness"
  `.claude/skills/lease/references/contract.md` -> "Normalized enums"

What is deliberately NOT checked: anything that needs a provider to answer. These run
with no network and no credential, which is the only way they mean the same thing on
every machine.
"""
import io
import json
import os
import types
import unittest
from contextlib import redirect_stdout

from helpers import REPO_ROOT, TempDirCase, load_script


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
    """`.claude/skills/lease/references/contract.md` -> "Scope completeness".

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
        args = types.SimpleNamespace(res="unused", tag_prefix="mlclaw-")
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
    """`.claude/skills/lease/references/contract.md` -> "Scope completeness", layer 2's half.

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

    def test_failed_provider_makes_the_result_incomplete(self):
        _, errors, scope = self._collect(
            {"boom": (False, {"error": "credential_expired", "detail": "token"})})
        self.assertIn("boom", errors)
        self.assertFalse(scope["complete"],
                         "a provider that errored is an unreached corner, not just a "
                         "line in `errors` beside an empty orphan list")

    def test_bare_list_from_sweep_is_treated_as_unknown_scope(self):
        rows, _, scope = self._collect({"old": (True, [{"instance_id": "i", "tag": "t"}])})
        self.assertEqual(len(rows), 1)
        self.assertFalse(scope["complete"])

    def test_capacity_may_return_a_bare_list(self):
        """Only `sweep`/`history` carry the envelope; holding `capacity` to it would
        make every adapter incomplete for no reason."""
        _, _, scope = self._collect({"p": (True, [{"machine_type": "x"}])}, verb="capacity")
        self.assertTrue(scope["complete"])

    def test_envelope_is_unwrapped_and_merged(self):
        rows, _, scope = self._collect({"p": (True, {
            "units": [{"instance_id": "i", "tag": "t"}],
            "scope": {"complete": False, "checked": ["one"],
                      "unreached": [{"scope": "two", "why": "timeout"}]}})})
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
             [] if scope_complete else [{"provider": "p", "why": "timeout"}]})
        args = types.SimpleNamespace(res=self.res, tag_prefix="mlclaw-")
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
    """`.claude/skills/lease/references/contract.md` -> "Normalized enums".

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

    def _release(self, outcome):
        return capture(self.pool.v_release, types.SimpleNamespace(
            session=self.session, slot="slot_0", outcome=outcome))

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


class TableProvenance(unittest.TestCase):
    """`.claude/skills/lease/references/contract.md` -> "Shape resolution: requirements → machine type is a lookup, not a translation".

    `arch` decides whether a job's kernels load at all, and price is what a human is
    quoted. Both are hand-written here, and a hand-written value that reads like a
    measured one is the failure — not the hand-writing.
    """

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "lifecycle", "scripts", "lease",
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
        targets = [os.path.join(REPO_ROOT, "lifecycle", "scripts", "lease", f)
                   for f in sorted(os.listdir(
                       os.path.join(REPO_ROOT, "lifecycle", "scripts", "lease")))
                   if f.endswith((".py", ".json"))]
        targets += [os.path.join(REPO_ROOT, "lifecycle", "scripts", "shared", "pool.py"),
                    os.path.join(REPO_ROOT, "lifecycle", "resources.json"),
                    os.path.join(REPO_ROOT, "lifecycle", "references", "fleet.md")]
        for path in targets:
            rel = os.path.relpath(path, REPO_ROOT)
            with self.subTest(file=rel):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for needle in pattern:
                    self.assertNotIn(needle, text,
                                     f"{rel} pins {needle}… — discover it at call time")


if __name__ == "__main__":
    unittest.main()
