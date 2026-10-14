"""Behaviour of the collector's kill guards (owner Close / Stop), with a fake process table.

Each guard has a test that goes red when the guard is removed or loosened; see the mutation
notes in the PR description for the self-check.
"""
import logging
import os
import pathlib
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from botroll import health, local
from botroll.board import health_panel, snapshot

NOW = 1_800_000_000
CHROME = "/opt/google/chrome/chrome"
PROFILE = "/home/u/profiles/Fork-3"


class Procs:
    """pid → (kernel start time, argv); patched over /proc reads."""

    def __init__(self, table):
        self.table = table

    def started(self, pid):
        return self.table[pid][0] if pid in self.table else None

    def argv(self, pid):
        return list(self.table[pid][1]) if pid in self.table else None


class Guards(unittest.TestCase):
    def setUp(self):
        self.cfg = {**health.load_config(None), "control": True, "browsers": {PROFILE: "ann"},
                    "helper_match": "playwright-runtime"}
        self.procs = Procs({
            100: (NOW - 500, [CHROME, f"--user-data-dir={PROFILE}", "--remote-debugging-port=9222"]),
            101: (NOW - 500, [CHROME, "--type=renderer", f"--user-data-dir={PROFILE}"]),
            102: (NOW - 500, ["/usr/bin/python3", "fake.py", f"--user-data-dir={PROFILE}"]),
            103: (NOW - 500, [CHROME, "--user-data-dir=/home/u/profiles/Fork-30"]),
            104: (NOW - 500, [CHROME, "--user-data-dir=/home/u/profiles/Other"]),
            200: (NOW - 50, ["node", "/x/playwright-runtime/cli.js", "--port=1"]),
            201: (NOW - 50, ["/usr/bin/python3", "playwright-runtime.py"]),
            202: (NOW - 50, ["node", "/x/other-server.js"]),
            300: (NOW - 60, ["ffmpeg", "-i", "a.mp4", "b.mp4"]),
            301: (NOW - 60, ["python3", "notes.py", "--msg", "about", "vitest"]),
            302: (NOW - 60, ["bash", "runner.sh", "npm", "run", "later"]),
            303: (NOW - 60, ["editor", "next.config.js", "build-notes"]),
            304: (NOW - 60, ["node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "run", "build"]),
            305: (NOW - 60, ["node", "--max-old-space-size=4096", "/app/node_modules/.bin/vitest", "run"]),
            306: (NOW - 60, ["/app/node_modules/.bin/next", "build"]),
            307: (NOW - 60, ["npm", "exec", "vitest"]),
        })
        for name, fn in (("_proc_started_at", self.procs.started), ("_argv", self.procs.argv)):
            p = mock.patch.object(health, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        self.sample = {
            "browsers": [{"profile": os.path.basename(self.procs.table[p][1][-1].split("=")[-1]) if p != 100 else "Fork-3",
                          "main_pid": p, "started_at": NOW - 500, "closable": True} for p in (100, 101, 102, 103, 104)],
            "helpers": [{"pid": p, "started_at": NOW - 50} for p in (200, 201, 202)],
            "jobs": [{"pid": p, "started_at": NOW - 60} for p in range(300, 308)],
        }
        for g in self.sample["browsers"]:  # every row claims the configured profile, so only the live check differs
            g["profile"] = "Fork-3"

    def close(self, pid=100, started=NOW - 500, profile="Fork-3", sample=None, cfg=None):
        return health.allowed_target("close-browser", {"pid": pid, "started_at": started, "profile": profile},
                                     cfg or self.cfg, self.sample if sample is None else sample)

    def stop(self, kind, pid, started):
        return health.allowed_target(kind, {"pid": pid, "started_at": started}, self.cfg, self.sample)

    # Start time / PID reuse

    def test_close_happy_path(self):
        self.assertIsNone(self.close())

    def test_start_time_must_match(self):
        self.procs.table[100] = (NOW - 400, self.procs.table[100][1])  # same pid, new process
        self.assertEqual(self.close(), "pid gone or reused")

    def test_start_time_tolerance_is_one_second(self):
        self.procs.table[100] = (NOW - 499, self.procs.table[100][1])
        self.assertIsNone(self.close())
        self.procs.table[100] = (NOW - 498, self.procs.table[100][1])
        self.assertEqual(self.close(), "pid gone or reused")
        self.assertEqual(health.START_SKEW, 1)

    def test_gone_process(self):
        del self.procs.table[100]
        self.assertEqual(self.close(), "pid gone or reused")

    # Browser identity

    def test_non_browser_refused(self):
        self.assertEqual(self.close(pid=102), "not a browser main process")

    def test_renderer_refused(self):
        self.assertEqual(self.close(pid=101), "not a browser main process")

    def test_profile_must_match_exactly(self):
        self.assertEqual(self.close(pid=104), "profile mismatch")
        self.assertEqual(self.close(pid=103), "profile mismatch")  # Fork-3 is a substring of Fork-30

    def test_profile_must_be_listed_in_config(self):
        cfg = {**self.cfg, "browsers": {}}
        self.assertEqual(self.close(cfg=cfg), "browser not listed in config")
        cfg = {**self.cfg, "browsers": {"Fork-3": "ann"}}  # N4: a last-segment key names it, never closes it
        self.assertEqual(self.close(cfg=cfg), "browser not listed in config")
        cfg = {**self.cfg, "browsers": {PROFILE + "/": "ann"}}  # full path, trailing slash tolerated
        self.assertIsNone(self.close(cfg=cfg))
        cfg = {**self.cfg, "browsers": {"/elsewhere/Fork-3": "ann"}}  # same folder name, other path
        self.assertEqual(self.close(cfg=cfg), "browser not listed in config")
        cfg = {**self.cfg, "browsers": {"Fork": "ann", "/home/u/profiles": "x"}}  # no prefix/substring keys
        self.assertEqual(self.close(cfg=cfg), "browser not listed in config")

    # Helpers

    def test_helper_rules(self):
        self.assertIsNone(self.stop("stop-helper", 200, NOW - 50))
        self.assertEqual(self.stop("stop-helper", 201, NOW - 50), "not an allowed helper")  # not node
        self.assertEqual(self.stop("stop-helper", 202, NOW - 50), "not an allowed helper")  # no helper_match
        self.cfg["helper_match"] = ""
        self.assertEqual(self.stop("stop-helper", 200, NOW - 50), "not an allowed helper")

    # Jobs (M1): only argv[0]'s program, never a keyword anywhere in the command line

    def test_job_keyword_elsewhere_refused(self):
        for pid in (301, 302, 303, 307):
            self.assertEqual(self.stop("stop-job", pid, NOW - 60), "not an allowed heavy job", pid)

    def test_job_allowlist_accepts(self):
        for pid in (300, 304, 305, 306):
            self.assertIsNone(self.stop("stop-job", pid, NOW - 60), pid)

    def test_stoppable_job_pairs(self):
        cfg = self.cfg
        self.assertTrue(health.stoppable_job(["npm", "ci"], cfg))
        self.assertFalse(health.stoppable_job(["npm", "publish"], cfg))
        self.assertFalse(health.stoppable_job(["next", "dev"], cfg))
        self.assertFalse(health.stoppable_job(["/bin/sh", "-c", "ffmpeg -i x"], cfg))
        self.assertFalse(health.stoppable_job(["node"], cfg))
        self.assertFalse(health.stoppable_job([], cfg))
        self.assertTrue(health.stoppable_job(["make", "all"], {**cfg, "stop_jobs": [["make"]]}))
        self.assertFalse(health.stoppable_job(["ffmpeg"], {**cfg, "stop_jobs": [["make"]]}))
        with self.assertRaises(ValueError):
            path = tempfile.mktemp(suffix=".json")
            pathlib.Path(path).write_text('{"stop_jobs": ["ffmpeg"]}')
            self.addCleanup(os.unlink, path)
            health.load_config(path)

    def test_job_kind_cannot_borrow_helper_or_browser(self):
        self.assertEqual(self.stop("stop-job", 200, NOW - 50), "not on the latest local sample")
        self.assertEqual(self.stop("stop-helper", 300, NOW - 60), "not on the latest local sample")

    # Collector's own sample (L2) and control switch

    def test_target_must_be_on_local_sample(self):
        self.assertEqual(self.close(sample={"browsers": [], "helpers": [], "jobs": []}),
                         "not on the latest local sample")
        self.assertEqual(self.close(sample=None or {}), "not on the latest local sample")
        stale = {**self.sample, "browsers": [{**self.sample["browsers"][0], "started_at": NOW - 900}]}
        self.assertEqual(self.close(sample=stale), "not on the latest local sample")
        unlisted = {**self.sample, "browsers": [{**self.sample["browsers"][0], "closable": False}]}
        self.assertEqual(self.close(sample=unlisted), "not on the latest local sample")
        self.assertEqual(self.stop("stop-job", 300, NOW - 61), "not on the latest local sample")

    def test_none_sample_refused(self):
        self.assertEqual(health.allowed_target("stop-job", {"pid": 300, "started_at": NOW - 60}, self.cfg, None),
                         "not on the latest local sample")

    def test_control_off_refuses(self):
        self.assertEqual(self.close(cfg={**self.cfg, "control": False}), "control off")
        self.assertEqual(self.close(cfg={**self.cfg, "control": "yes"}), "control off")

    def test_bad_pids(self):
        self.assertEqual(self.close(pid=1), "bad pid")
        self.assertEqual(self.close(pid=True), "bad pid")
        self.assertEqual(self.close(started="x"), "missing started_at")
        with mock.patch.object(health.os, "getpid", return_value=100):
            self.assertEqual(self.close(), "refusing collector pid")


class RunCommands(Guards):
    def cmd(self, **kw):
        return {"id": "c1", "nonce": "n1", "kind": "stop-job", "target": {"pid": 300, "started_at": NOW - 60},
                "expires_at": NOW + 60, **kw}

    def run_(self, cmds, cfg=None):
        killed = []
        out = health.run_commands(cfg or self.cfg, cmds, self.sample, at=NOW,
                                  kill=lambda pid: killed.append(pid) or "terminated")
        return out, killed

    def test_runs_allowed_and_echoes_nonce(self):
        out, killed = self.run_([self.cmd()])
        self.assertEqual(killed, [300])
        self.assertEqual(out, [{"id": "c1", "nonce": "n1", "status": "ok", "detail": "terminated"}])

    def test_expired_refused(self):
        out, killed = self.run_([self.cmd(expires_at=NOW - 1), self.cmd(expires_at=None), self.cmd(expires_at=True)])
        self.assertEqual(killed, [])
        self.assertEqual({o["detail"] for o in out}, {"expired"})

    def test_control_off_refuses_all(self):
        out, killed = self.run_([self.cmd()], cfg={**self.cfg, "control": False})
        self.assertEqual((killed, out[0]["detail"]), ([], "control off"))
        # checked before anything else, without consulting the per-target guard
        with mock.patch.object(health, "allowed_target", return_value=None) as guard:
            out, killed = self.run_([self.cmd(expires_at=NOW - 5)], cfg={**self.cfg, "control": False})
        self.assertEqual((killed, out[0]["detail"], guard.called), ([], "control off", False))

    def test_control_config_is_strictly_opt_in(self):
        self.assertIs(health.DEFAULTS["control"], False)
        for text, want in (("{}", False), ('{"control": "yes"}', False), ('{"control": 1}', False),
                           ('{"control": null}', False), ('{"control": true}', True)):
            path = tempfile.mktemp(suffix=".json")
            pathlib.Path(path).write_text(text)
            self.addCleanup(os.unlink, path)
            self.assertIs(health.load_config(path)["control"], want, text)

    def test_refused_never_kills(self):
        out, killed = self.run_([self.cmd(target={"pid": 301, "started_at": NOW - 60}), "junk", self.cmd(kind="rm")])
        self.assertEqual(killed, [])
        self.assertEqual([o["status"] for o in out], ["refused"] * 3)


class Sampling(unittest.TestCase):
    def test_closable_only_for_configured_profiles(self):
        cfg = {**health.load_config(None), "browsers": {"Fork-3": "ann", "/p/Exact": "bob"}}
        rows = [{"pid": p, "ppid": 1, "rss_kb": 1024, "elapsed_s": 10, "cpu_s": 0, "args": a} for p, a in (
            (10, "/usr/bin/chromium --user-data-dir=/x/Fork-3"),
            (11, "/usr/bin/chromium --user-data-dir=/x/Fork-30"),
            (12, "/usr/bin/chromium --user-data-dir=/p/Exact"),
            (13, "/usr/bin/chromium"))]
        got = {g["profile"]: g["closable"] for g in health.group_browsers(rows, {}, cfg, at=NOW)}
        self.assertEqual(got, {"Fork-3": False, "Fork-30": False, "Exact": True, "(default profile)": False})


class Panel(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)
        import time
        self.snap = {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
                     "mem": {"total_mb": 1000, "available_mb": 500},
                     "browsers": [{"profile": "Fork-3", "bot": "ann", "procs": 1, "rss_mb": 100, "cpu": 1.0, "port": None,
                                   "main_pid": 4242, "started_at": 1000, "closable": True},
                                  {"profile": "Stray", "bot": None, "procs": 1, "rss_mb": 90, "cpu": 1.0, "port": None,
                                   "main_pid": 4300, "started_at": 1000, "closable": False}],
                     "helpers": [{"pid": 4243, "rss_mb": 50, "cpu": 0.0, "elapsed_s": 10, "started_at": 1001,
                                  "stale": False, "port": None, "cmd": "node"}],
                     "jobs": [{"pid": 4244, "kind": "ffmpeg", "expect_s": 60, "elapsed_s": 5, "started_at": 1002,
                               "rss_mb": 1, "cpu": 1.0, "cmd": "ffmpeg"}],
                     "alerts": []}
        health.append(self.data, self.snap)

    def html(self, **kw):
        from botroll import TIME
        out = health_panel({"generated": local().strftime(TIME), "health": health.load(self.data)}, env={}, **kw)
        self.assertIn("Box health", out)  # a render error would return "" and pass the negative checks
        return out

    def test_no_buttons_when_control_off(self):
        body = self.html(csrf="tok", control=False)
        self.assertNotIn("/health/control", body)
        self.assertNotIn("Close browser", body)
        self.assertNotIn(">Stop<", body)

    def test_no_buttons_without_csrf(self):
        self.assertNotIn("/health/control", self.html(csrf=None, control=True))

    def test_buttons_when_on_and_close_only_for_configured(self):
        body = self.html(csrf="tok", control=True)
        self.assertEqual(body.count("Close browser"), 1)
        self.assertIn('value="4242"', body)
        self.assertNotIn('value="4300"', body)
        self.assertEqual(body.count(">Stop<"), 2)
        self.assertIn('class="h-act"', body)

    def test_closable_survives_clean(self):
        self.assertTrue(health.load(self.data)[-1]["browsers"][0]["closable"])
        self.assertFalse(health.load(self.data)[-1]["browsers"][1]["closable"])


class Nonce(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)

    def test_result_bound_to_id_and_nonce(self):
        a = health.enqueue_command(self.data, "stop-job", {"pid": 300, "started_at": 1})
        b = health.enqueue_command(self.data, "stop-job", {"pid": 301, "started_at": 1})
        self.assertNotEqual(a["nonce"], b["nonce"])
        self.assertIsNone(health.finish_command(self.data, a["id"], "ok", "", b["nonce"]))  # other command's nonce
        self.assertIsNone(health.finish_command(self.data, a["id"], "ok", "", ""))
        self.assertIsNone(health.finish_command(self.data, a["id"], "ok", "", None))
        self.assertEqual(len(health.pending_commands(self.data)), 2)
        self.assertEqual(health.finish_command(self.data, a["id"], "ok", "x", a["nonce"])["status"], "ok")
        self.assertNotIn("nonce", health.recent_results(self.data)[-1])
        audit = pathlib.Path(self.data, health.AUDIT).read_text()
        self.assertNotIn(a["nonce"], audit)

    def test_clear_pending(self):
        health.enqueue_command(self.data, "stop-job", {"pid": 300, "started_at": 1})
        self.assertEqual(health.clear_pending(self.data), 1)
        self.assertEqual(health.pending_commands(self.data), [])
        self.assertEqual(health.clear_pending(self.data), 0)


class BoardTime(unittest.TestCase):
    def test_future_skew_counts_as_recent(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        pathlib.Path(d, "bot.md").write_text(
            "---\nname: Bot\nrole: X\n---\n"
            "- [done] T-1 Clock a bit ahead · asked 2026-10-15 09:00 · updated 2026-10-15 10:05\n"
            "- [done] T-2 Clock way ahead · asked 2026-10-15 09:00 · updated 2026-10-15 10:30\n"
            "- [done] T-3 Older than a day · asked 2026-10-13 09:00 · updated 2026-10-14 09:59\n")
        s = snapshot(d, datetime(2026, 10, 15, 10, 0), {})
        self.assertEqual(s["summary"]["done_recent"], 1)
        self.assertEqual({i["id"] for i in s["bots"][0]["items"]}, {"T-1"})

    def test_invalid_tz_falls_back_to_utc(self):
        for bad in ("Mars/Base", "../etc/passwd", "Not A Zone"):
            with mock.patch.dict(os.environ, {"BOTROLL_TZ": bad}), self.assertLogs("botroll", logging.WARNING) as logs:
                import botroll
                botroll._BAD_ZONES.discard(bad)
                self.assertEqual(local(0), datetime(1970, 1, 1, 0, 0))
            self.assertIn("using UTC", logs.output[0])


if __name__ == "__main__":
    unittest.main()
