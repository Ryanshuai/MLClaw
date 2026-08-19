#!/usr/bin/env python3
"""The two probes `/resources` and every run skill's Step 1 reach for.

`parse_ssh_config.py` seeds `resources.json`, which every `${}` resolves through.
`test_connection.py` is what confirms a declared source actually resolves before
a run launches. Both answered with fewer facts than they had, in the shape
CLAUDE.md names: *a machine that did not answer, a path that is not there, and a
directory that is genuinely empty are three facts, and only the last one means
the data is gone.*
"""
import json
import os
import unittest

from helpers import TempDirCase, load_script, run_script

ssh_cfg = load_script("resources/parse_ssh_config.py")
conn = load_script("shared/test_connection.py")
SSH_SCRIPT = "resources/parse_ssh_config.py"


class TheAliasIsTheThingYouCanActuallySshTo(TempDirCase):
    """`Host gpu` + `HostName 10.0.0.4` yielded `alias: ""`, because the alias
    and the sole name were the same string until HostName overwrote the host.
    `ssh gpu` is the only spelling that carries the user's ProxyJump and
    IdentityFile, so it is the one worth recording."""

    def parse(self, text, name="config"):
        return ssh_cfg.parse_ssh_config_full(self.write(f"ssh/{name}", text))

    def test_a_host_with_a_hostname_keeps_both(self):
        r = self.parse("Host gpu\n  HostName 10.0.0.4\n  User ml\n")
        self.assertEqual(r["servers"], [dict(r["servers"][0],
                                             alias="gpu", host="10.0.0.4", username="ml")])
        self.assertEqual(r["servers"][0]["alias"], "gpu")
        self.assertEqual(r["servers"][0]["host"], "10.0.0.4")

    def test_a_bare_host_is_its_own_address(self):
        r = self.parse("Host gpu\n")
        self.assertEqual((r["servers"][0]["alias"], r["servers"][0]["host"]), ("gpu", "gpu"))

    def test_a_wildcard_stanza_names_no_machine(self):
        r = self.parse("Host *\n  User ml\n\nHost gpu\n  HostName 10.0.0.4\n")
        self.assertEqual([s["alias"] for s in r["servers"]], ["gpu"])

    def test_a_wildcard_stanzas_settings_do_not_leak_onto_the_next_host(self):
        r = self.parse("Host *\n  User leaked\n\nHost gpu\n  HostName 10.0.0.4\n")
        self.assertEqual(r["servers"][0]["username"], "")

    def test_a_match_block_does_not_inherit_the_preceding_host(self):
        """Its body applies conditionally; attributing it would record settings
        that may not apply to that machine at all."""
        r = self.parse("Host gpu\n  HostName 10.0.0.4\n\nMatch exec true\n  User other\n")
        self.assertEqual(len(r["servers"]), 1)
        self.assertEqual(r["servers"][0]["username"], "")


class OneBadLineIsOneBadLine(TempDirCase):
    """`Port` was `int(val)` with no guard, so a single malformed line lost the
    WHOLE server list to a traceback -- and `/resources`' fallback is then "read
    ~/.ssh/config by hand"."""

    def parse(self, text, name="config"):
        return ssh_cfg.parse_ssh_config_full(self.write(f"ssh/{name}", text))

    def test_a_malformed_port_does_not_cost_the_other_servers(self):
        r = self.parse("Host a\n  Port notanumber\n\nHost b\n  HostName 10.0.0.5\n")
        self.assertEqual(sorted(s["alias"] for s in r["servers"]), ["a", "b"])

    def test_and_it_says_so_rather_than_reporting_the_default_as_read(self):
        r = self.parse("Host a\n  Port notanumber\n")
        self.assertEqual(r["servers"][0]["port"], 22)
        self.assertTrue(r["warnings"])
        self.assertFalse(r["complete"])

    def test_a_good_port_is_read(self):
        r = self.parse("Host a\n  Port 2222\n")
        self.assertEqual(r["servers"][0]["port"], 2222)
        self.assertTrue(r["complete"])


class AnIncludedFileIsPartOfTheAnswer(TempDirCase):
    """A config split across `~/.ssh/config.d/*` yielded a short list presented
    as the complete one. `sources` is what makes the completeness checkable."""

    def test_an_included_file_contributes_its_hosts(self):
        self.write("ssh/config.d/work", "Host work\n  HostName 10.1.1.1\n")
        main = self.write("ssh/config", "Include config.d/*\n\nHost home\n  HostName 10.0.0.1\n")
        r = ssh_cfg.parse_ssh_config_full(main)
        self.assertEqual(sorted(s["alias"] for s in r["servers"]), ["home", "work"])

    def test_the_files_it_read_are_named(self):
        self.write("ssh/config.d/work", "Host work\n")
        main = self.write("ssh/config", "Include config.d/*\n")
        r = ssh_cfg.parse_ssh_config_full(main)
        self.assertEqual(len(r["sources"]), 2)

    def test_an_include_cycle_terminates(self):
        """Legal to write, and an infinite loop in the thing that runs before
        anything else does."""
        self.write("ssh/a", "Include b\nHost a\n")
        self.write("ssh/b", "Include a\nHost b\n")
        r = ssh_cfg.parse_ssh_config_full(self.path("ssh", "a"))
        self.assertEqual(sorted(s["alias"] for s in r["servers"]), ["a", "b"])

    def test_a_missing_config_is_an_empty_list_not_a_crash(self):
        r = ssh_cfg.parse_ssh_config_full(self.path("ssh", "nope"))
        self.assertEqual(r["servers"], [])

    def test_the_list_form_is_still_available_to_callers_that_index_it(self):
        main = self.write("ssh/config", "Host a\n")
        self.assertEqual([s["alias"] for s in ssh_cfg.parse_ssh_config(main)], ["a"])

    def test_the_cli_emits_the_full_record(self):
        main = self.write("ssh/config", "Host a\n")
        rc, out, _err = run_script(SSH_SCRIPT, main)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(out), ["complete", "servers", "sources", "warnings"])


class ReachableAndPresentAreTwoAnswers(unittest.TestCase):
    """`test -e P && echo exists || echo not_found` exits 0 either way, so a
    reachable machine that does NOT have the path reported `ok: true` exactly
    like one that does -- and the only difference was a word buried in `output`.
    Step 1 calls this to confirm a declared source resolves."""

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def _ssh(self, result, **kw):
        real_run, real_which = conn.subprocess.run, conn.shutil.which
        conn.subprocess.run = lambda *a, **k: result
        conn.shutil.which = lambda _n: "/usr/bin/ssh"
        try:
            return conn.test_ssh("h", 22, "u", "", **kw)
        finally:
            conn.subprocess.run, conn.shutil.which = real_run, real_which

    def test_a_present_path_is_exists_true(self):
        r = self._ssh(self._R(0, "exists"), remote_path="/data/val")
        self.assertTrue(r["ok"])
        self.assertIs(r["exists"], True)

    def test_a_missing_path_on_a_reachable_box_is_exists_false_not_ok(self):
        r = self._ssh(self._R(0, "not_found"), remote_path="/data/val")
        self.assertTrue(r["ok"], "the machine answered; that part is true")
        self.assertIs(r["exists"], False,
                      "and the path is gone, which is the fact that was asked for")

    def test_no_path_asked_about_is_exists_none(self):
        r = self._ssh(self._R(0, "ok"))
        self.assertIsNone(r["exists"])

    def test_an_unreachable_machine_never_says_the_path_is_missing(self):
        """Nothing looked. `False` here would report a live dataset as deleted."""
        r = self._ssh(self._R(255, "", "Connection refused"), remote_path="/data/val")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["exists"])

    def test_an_unrecognised_probe_output_leaves_exists_null(self):
        r = self._ssh(self._R(0, "banner\nsomething else"), remote_path="/data/val")
        self.assertIsNone(r["exists"])
        self.assertIn("‼️", r)

    def test_a_timeout_is_not_a_missing_path_either(self):
        real_run, real_which = conn.subprocess.run, conn.shutil.which

        def boom(*_a, **_k):
            raise conn.subprocess.TimeoutExpired(cmd="ssh", timeout=10)
        conn.subprocess.run, conn.shutil.which = boom, (lambda _n: "/usr/bin/ssh")
        try:
            r = conn.test_ssh("h", 22, "u", "", remote_path="/data/val")
        finally:
            conn.subprocess.run, conn.shutil.which = real_run, real_which
        self.assertFalse(r["ok"])
        self.assertIsNone(r["exists"])

    def test_a_path_with_a_space_is_quoted_into_the_remote_command(self):
        """Unquoted, `test -e /my data` tests `/my` and reports it present."""
        captured = {}
        real_run, real_which = conn.subprocess.run, conn.shutil.which

        def spy(cmd, *_a, **_k):
            captured["cmd"] = cmd
            return self._R(0, "not_found")
        conn.subprocess.run, conn.shutil.which = spy, (lambda _n: "/usr/bin/ssh")
        try:
            conn.test_ssh("h", 22, "u", "", remote_path="/my data/val")
        finally:
            conn.subprocess.run, conn.shutil.which = real_run, real_which
        self.assertIn("'/my data/val'", captured["cmd"][-1])


class UsageIsNotAnUnreachableMachine(unittest.TestCase):
    """CLAUDE.md -> "Script Integration". Both were exit 1, so "you typed it
    wrong" arrived as "that machine is down"."""

    def test_no_argument_is_exit_2(self):
        rc, out, _err = run_script("shared/test_connection.py")
        self.assertEqual(rc, 2)
        self.assertIn("error", out)

    def test_an_unknown_connection_type_is_exit_2(self):
        rc, out, _err = run_script("shared/test_connection.py", "carrier-pigeon", "x")
        self.assertEqual(rc, 2)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
