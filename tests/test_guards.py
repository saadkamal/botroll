"""Tests that pin the server, collector and name-file guards."""
import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_serve

from botroll import TIME, health, local, serve
from botroll.board import health_panel


def realtmp(test):
    d = pathlib.Path(os.path.realpath(tempfile.mkdtemp()))
    test.addCleanup(shutil.rmtree, d)
    return d


class Stop(BaseException):  # escapes the collector loop, which logs and survives any Exception
    pass


class ServerControl(test_serve.Server):
    def setUp(self):
        super().setUp()
        self.cfg.health = True
        self.cfg.health_control = True
        health.append(self.data, {
            "v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
            "mem": {"total_mb": 1000, "available_mb": 500},
            "browsers": [{"profile": "Fork-3", "bot": "ann", "procs": 1, "rss_mb": 1, "cpu": 0.0, "port": None,
                          "main_pid": 4242, "started_at": 1000, "closable": True}],
            "helpers": [{"pid": 4243, "rss_mb": 1, "cpu": 0.0, "elapsed_s": 1, "started_at": 1001, "stale": False,
                         "port": None, "cmd": "node"}],
            "jobs": [{"pid": 4244, "kind": "ffmpeg", "expect_s": 60, "elapsed_s": 1, "started_at": 1002,
                      "rss_mb": 1, "cpu": 0.0, "cmd": "ffmpeg"}],
            "alerts": []})

    def post(self, **form):
        import re
        r = self.request("GET", "/login")
        cookies = r.cookies()
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.body)[1]
        cookies.update(self.request("POST", "/login", {"password": test_serve.PASSWORD, "csrf": csrf},
                                    cookies=cookies).cookies())
        csrf = re.search(r'name="csrf" value="([^"]+)"', self.request("GET", "/", cookies=cookies).body)[1]
        return self.request("POST", "/health/control", {"csrf": csrf, **form}, cookies=cookies).status

    def test_server_checks_start_time_for_every_kind(self):
        self.assertEqual(self.post(kind="close-browser", profile="Fork-3", pid="4242", started_at="999"), 400)
        self.assertEqual(self.post(kind="stop-helper", pid="4243", started_at="1000"), 400)
        self.assertEqual(self.post(kind="stop-job", pid="4244", started_at="1003"), 400)
        self.assertEqual(health.pending_commands(self.data), [])
        self.assertEqual(self.post(kind="close-browser", profile="Fork-3", pid="4242", started_at="1000"), 303)
        self.assertEqual(self.post(kind="stop-job", pid="4244", started_at="1002"), 303)


class ServeStart(unittest.TestCase):
    def run_serve(self, control):
        data = str(realtmp(self))
        health.enqueue_command(data, "stop-job", {"pid": 300, "started_at": 1})
        cfg = serve.configure(data, {"BOTROLL_PASSWORD_HASH": test_serve.HASH, "BOTROLL_SECRET": test_serve.SECRET,
                                     "BOTROLL_HEALTH": "1", "BOTROLL_HEALTH_CONTROL": "1" if control else ""})
        fake = mock.Mock(server_address=("127.0.0.1", 0))
        with mock.patch.object(serve, "make_server", return_value=fake), contextlib.redirect_stderr(io.StringIO()):
            serve.serve(cfg, 0)
        fake.serve_forever.assert_called_once()
        return health.pending_commands(data)

    def test_start_clears_queue_when_control_off(self):
        self.assertEqual(self.run_serve(control=False), [])

    def test_start_keeps_queue_when_control_on(self):
        self.assertEqual(len(self.run_serve(control=True)), 1)


class FinishedStaysFinished(unittest.TestCase):
    def test_no_reopen(self):
        data = str(realtmp(self))
        row = health.enqueue_command(data, "stop-job", {"pid": 300, "started_at": 1})
        self.assertEqual(health.finish_command(data, row["id"], "ok", "terminated", row["nonce"])["status"], "ok")
        again = health.finish_command(data, row["id"], "failed", "late", row["nonce"])
        self.assertEqual(again["status"], "ok")
        self.assertEqual(again["result"]["detail"], "terminated")
        stored = [r for r in health.load_commands(data) if r["id"] == row["id"]][0]
        self.assertEqual((stored["status"], stored["result"]["detail"]), ("ok", "terminated"))
        self.assertEqual(health.pending_commands(data), [])


class CollectorControlOff(unittest.TestCase):
    def run_cli(self, config):
        d = realtmp(self)
        cfgfile = d / "c.json"
        cfgfile.write_text(json.dumps(config))
        cmd = {"id": "c1", "nonce": "n1", "kind": "stop-job", "target": {"pid": 300, "started_at": 1},
               "expires_at": time.time() + 60}
        from botroll.cli import main
        with mock.patch.object(health, "fetch_commands", return_value=[cmd]) as fetch, \
                mock.patch.object(health, "report_command") as report, \
                mock.patch.object(health, "terminate") as kill, \
                mock.patch.object(health, "sample", side_effect=Stop), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Stop):
            main(["--dir", str(d), "health", "--config", str(cfgfile), "--url", "https://board.example",
                  "--token", "t" * 40, "--every", "60"])
        return fetch, report, kill

    def test_refuses_queued_commands_at_start(self):
        fetch, report, kill = self.run_cli({"control": False})
        fetch.assert_called_once()
        report.assert_called_once_with("https://board.example", "t" * 40, "c1", "refused", "control off", "n1")
        kill.assert_not_called()


class ProfileNumber(unittest.TestCase):
    def setUp(self):
        self.dir = realtmp(self)
        for n in ("3", "12", "0003"):
            (self.dir / n).write_text(f"bot{n}")
        self.cfg = {**health.load_config(None), "display_names_dir": str(self.dir)}

    def name(self, profile):
        with mock.patch.object(health, "_display_of", return_value=None):
            return health.display_name(profile, self.cfg)

    def test_strict_folder_pattern(self):
        self.assertEqual(self.name("/p/Fork-3"), "bot3")
        self.assertEqual(self.name("/p/Fork-3/"), "bot3")
        self.assertEqual(self.name("/p/chrome-profile-12"), "bot12")
        for bad in ("/p/Fork3", "/p/3", "/p/-3", "/p/Fork-3x", "/p/Fork-3.bak", "/p/v2", "/p/Fork-3-old",
                    "/p/Fork 3", "/p/Fork-123456", "/p/12-3"):
            self.assertIsNone(self.name(bad), bad)


class ReadCap(unittest.TestCase):
    def test_names_file_is_capped(self):
        f = realtmp(self) / "names.tsv"
        f.write_text("Fork-1\tAnn\n" + "#" * health.NAME_FILE_MAX + "\nFork-2\tBob\n")
        self.assertEqual(health.names_from_file(str(f)), {"Fork-1": "Ann"})

    def test_read_small_limit(self):
        f = realtmp(self) / "x"
        f.write_bytes(b"a" * 5000)
        self.assertEqual(len(health._read_small(str(f), 1024)), 1024)
        self.assertEqual(len(health._read_small(str(f))), 5000)

    def test_display_file_is_capped(self):
        d = realtmp(self)
        (d / "3").write_text("x" * 2000 + "\n")
        cfg = {**health.load_config(None), "display_names_dir": str(d)}
        with mock.patch.object(health, "_read_small", wraps=health._read_small) as rs:
            self.assertIsNone(health.display_name("/p/Fork-3", cfg))
        self.assertEqual(rs.call_args[0][1], 1024)


@unittest.skipUnless(sys.platform.startswith("linux"), "sample() reads /proc")
class KernelStartTime(unittest.TestCase):
    def test_sample_uses_kernel_start_time(self):
        rows = health.parse_ps(
            "  100     1 102400 50 1 /usr/bin/chromium --user-data-dir=/p/Fork-3\n"
            "  200     1 10240 40 1 node /x/pw-helper.js\n"
            "  300     1 10240 30 1 ffmpeg -i a b\n")
        cfg = {**health.load_config(None), "tabs": False, "helper_match": "pw-helper"}
        starts = {100: 1_700_000_001, 200: 1_700_000_002, 300: 1_700_000_003}
        with mock.patch.object(health, "_ps", return_value=rows), mock.patch.object(health.time, "sleep"), \
                mock.patch.object(health, "_proc_started_at", side_effect=starts.get):
            snap = health.sample(cfg, interval=0)
        self.assertEqual(snap["browsers"][0]["started_at"], 1_700_000_001)
        self.assertEqual(snap["helpers"][0]["started_at"], 1_700_000_002)
        self.assertEqual(snap["jobs"][0]["started_at"], 1_700_000_003)


class HintEscaping(unittest.TestCase):
    def test_profile_hint_escaped(self):
        data = str(realtmp(self))
        evil = '"><img src=x onerror=alert(1)>'
        health.append(data, {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
                             "mem": {"total_mb": 1000, "available_mb": 500},
                             "browsers": [{"profile": evil, "bot": None, "label": "Work", "procs": 1, "rss_mb": 1,
                                           "cpu": 0.0, "port": None}],
                             "helpers": [], "jobs": [], "alerts": []})
        out = health_panel({"generated": local().strftime(TIME), "health": health.load(data)}, env={})
        self.assertIn("profile name", out)
        self.assertNotIn("<img", out)
        self.assertIn("&quot;&gt;&lt;img src=x onerror=alert(1)&gt; · profile name", out)


class Invisible(unittest.TestCase):
    def test_soft_hyphen_and_friends(self):
        self.assertEqual(health.plain_text("Fork\u00ad-9"), "Fork-9")
        for ch in ("\u00ad", "\u034f", "\u115f", "\u1160", "\u3164", "\uffa0", "\ufe0f", "\ufe00",
                   "\U000e0100", "\U000e01ef", "\u180b", "\u180e", "\u180f", "\U000e0001", "\U000e0041",
                   "\U000e007f", "\u2061", "\u200d", "\u202e", "\ufeff", "\x1b", "\u0600", "\U000110bd"):
            self.assertEqual(health.plain_text(f"A{ch}B"), "AB", repr(ch))
        self.assertEqual(health.plain_text("Zoë 日本 émoji 🙂"), "Zoë 日本 émoji 🙂")

    def test_soft_hyphen_cleaned_everywhere(self):
        d = realtmp(self)
        f = d / "names.tsv"
        f.write_text("Fork\u00ad-9\tK\u00adai\n")
        self.assertEqual(health.names_from_file(str(f)), {"Fork-9": "Kai"})
        self.assertEqual(health.resolve_profile("/p/Fork-9", {**health.load_config(None), "names_file": str(f),
                                                              "display_names_dir": None}), "Kai")
        self.assertEqual(health.clean({"v": 1, "ts": 1, "host": "h", "cores": 1, "load": [0, 0, 0],
                                       "mem": {"total_mb": 1, "available_mb": 1},
                                       "browsers": [{"profile": "Fork\u00ad-9", "bot": "Q\u3164", "procs": 1,
                                                     "rss_mb": 1, "cpu": 0}],
                                       "helpers": [], "jobs": [], "alerts": []})["browsers"][0]["profile"], "Fork-9")


class Links(unittest.TestCase):
    def test_symlinked_parent_refused(self):
        d = realtmp(self)
        (d / "real").mkdir()
        (d / "real" / "names.tsv").write_text("Fork-1\tAnn\n")
        (d / "link").symlink_to(d / "real")
        self.assertEqual(health.names_from_file(str(d / "real" / "names.tsv")), {"Fork-1": "Ann"})
        self.assertEqual(health.names_from_file(str(d / "link" / "names.tsv")), {})
        (d / "real" / "3").write_text("Kai")
        with mock.patch.object(health, "_display_of", return_value=None):
            cfg = {**health.load_config(None), "display_names_dir": str(d / "link")}
            self.assertIsNone(health.display_name("/p/Fork-3", cfg))
            cfg["display_names_dir"] = str(d / "real")
            self.assertEqual(health.display_name("/p/Fork-3", cfg), "Kai")

    def test_hardlink_refused(self):
        d = realtmp(self)
        (d / "a").write_text("Fork-1\tAnn\n")
        os.link(d / "a", d / "b")
        self.assertEqual(health.names_from_file(str(d / "a")), {})
        os.unlink(d / "b")
        self.assertEqual(health.names_from_file(str(d / "a")), {"Fork-1": "Ann"})


class TrailingSlashes(unittest.TestCase):
    def test_both_places(self):
        mapping = {"/srv/p/Main/": "ann", "Fork-3/": "bob"}
        cfg = {**health.load_config(None), "browsers": mapping, "display_names_dir": None}
        self.assertEqual(health.resolve_profile("/srv/p/Main", cfg), "ann")
        self.assertEqual(health.resolve_profile("/srv/p/Main//", cfg), "ann")
        self.assertEqual(health.resolve_profile("/x/Fork-3/", cfg), "bob")
        self.assertTrue(health.configured_browser("/srv/p/Main//", cfg))
        rows = [{"pid": p, "ppid": 1, "rss_kb": 1024, "elapsed_s": 5, "cpu_s": 0, "args": a} for p, a in (
            (10, "/usr/bin/chromium --user-data-dir=/srv/p/Main/"),
            (11, "/usr/bin/chromium --type=renderer --user-data-dir=/srv/p/Main"))]
        groups = health.group_browsers(rows, {}, cfg, at=100)
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0]["profile"], groups[0]["procs"], groups[0]["closable"]), ("Main", 2, True))
        self.assertEqual(health.norm_path("/"), "/")
        self.assertEqual(health.norm_path("a//"), "a")


class UnknownNodeFlags(unittest.TestCase):
    cfg = {"stop_jobs": [["vitest"], ["ffmpeg"]]}

    def test_unknown_flag_means_no_match(self):
        ok = lambda argv: health.stoppable_job(argv, self.cfg)  # noqa: E731
        self.assertFalse(ok(["node", "--some-new-flag", "vitest", "app.js"]))
        self.assertFalse(ok(["node", "-x", "ffmpeg"]))
        self.assertTrue(ok(["node", "--no-warnings", "--max-old-space-size=4096", "/x/.bin/vitest"]))
        self.assertTrue(ok(["node", "--some-new-flag=1", "/x/.bin/vitest"]))



class NodeEqualsForms(unittest.TestCase):
    cfg = {"stop_jobs": [["vitest"], ["ffmpeg"]]}

    def ok(self, argv):
        return health.stoppable_job(argv, self.cfg)

    def test_l1_no_script_flags_with_equals(self):
        for argv in (["node", "--eval=CODE", "vitest.mjs"], ["node", "--print=1", "/x/.bin/vitest"],
                     ["node", "-e=", "vitest"], ["node", "-p=x", "ffmpeg"], ["node", "--check=x", "vitest"],
                     ["node", "--test=x", "vitest"], ["node", "--interactive=1", "vitest"]):
            self.assertFalse(self.ok(argv), argv)
            self.assertIsNone(health._node_script(argv[1:]), argv)

    def test_value_flags_with_equals_still_skip_one(self):
        self.assertTrue(self.ok(["node", "--require=dotenv/config", "--import=tsx", "/x/.bin/vitest"]))
        self.assertTrue(self.ok(["node", "--inspect=9229", "/x/.bin/vitest"]))

    def test_common_boolean_flags(self):
        for flag in ("--experimental-strip-types", "--experimental-transform-types", "--watch", "--cpu-prof",
                     "--enable-source-maps", "--no-warnings", "--experimental-require-module", "--jitless",
                     "--trace-uncaught", "--frozen-intrinsics"):
            self.assertTrue(self.ok(["node", flag, "/x/.bin/vitest", "run"]), flag)
        self.assertFalse(self.ok(["node", "--watch-path", "vitest", "app.js"]))  # takes a value: unknown → refuse
        self.assertFalse(set(health.NODE_BOOL_FLAGS) & set(health.NODE_VALUE_FLAGS + health.NODE_NO_SCRIPT))
        self.assertFalse([f for f in health.NODE_BOOL_FLAGS if "=" in f])


class BrailleBlankAndLetters(unittest.TestCase):
    def test_u2800_stripped(self):
        self.assertEqual(health.plain_text("A\u2800B"), "AB")
        self.assertIsNone(health._clean_name("\u2800\u2800"))

    def test_confusables_and_combining_marks_kept(self):
        # Real letters in many scripts; names are labels only, so these are left as written (see INVISIBLE_EXTRA).
        for text in ("P\u0430ypal", "Zoe\u0308", "\u0928\u093f\u0924\u093f\u0928", "\u05e9\u05c1\u05dc\u05d5\u05b9\u05dd"):
            self.assertEqual(health.plain_text(text), text)


class BoardStripsOnRender(unittest.TestCase):
    """board.name() strips again, so a sample that skipped clean() (an old store, a future code path) still
    renders without invisible characters. _health_panel is fed the raw sample directly."""

    def test_n3d_board_name_strips(self):
        from botroll import board
        raw = {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
               "mem": {"total_mb": 1000, "available_mb": 500},
               "browsers": [{"profile": "p", "bot": "Ev\u202eil\u00ad\u2800", "procs": 1, "rss_mb": 1, "cpu": 0.0,
                             "port": None},
                            {"profile": "Fo\u200brk-2", "bot": None, "procs": 1, "rss_mb": 1, "cpu": 0.0,
                             "port": None}],
               "helpers": [], "jobs": [], "alerts": []}
        out = board._health_panel({"generated": local().strftime(TIME)}, [raw], {})
        self.assertIn('<span class="h-br-name">Evil</span>', out)
        self.assertIn('<span class="h-br-name">Fork-2 <span', out)
        for ch in ("\u202e", "\u00ad", "\u2800", "\u200b"):
            self.assertNotIn(ch, out.split("Last 24 hours")[0])



class InvisibleInPaths(unittest.TestCase):
    def test_profile_with_invisibles_matches_clean_keys(self):
        d = realtmp(self)
        f = d / "names.tsv"
        f.write_text("Fork-9\tKai\n")
        cfg = {**health.load_config(None), "names_file": str(f), "display_names_dir": None,
               "browsers": {"Fo\u200brk-4": "Ann"}}
        self.assertEqual(health.resolve_profile("/p/Fork\u00ad-9", cfg), "Kai")
        self.assertEqual(health.resolve_profile("/p/Fork-4", cfg), "Ann")

    def test_display_number_from_profile_with_invisibles(self):
        d = realtmp(self)
        (d / "9").write_text("Vera")
        cfg = {**health.load_config(None), "display_names_dir": str(d)}
        with mock.patch.object(health, "_display_of", return_value=None):
            self.assertEqual(health.display_name("/p/Fork\u00ad-9", cfg), "Vera")


if __name__ == "__main__":
    unittest.main()


class BrowserTabGroups(unittest.TestCase):
    def panel(self, browsers, **kw):
        data = str(realtmp(self))
        health.append(data, {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
                             "mem": {"total_mb": 1000, "available_mb": 500}, "browsers": browsers,
                             "helpers": [], "jobs": [], "alerts": []})
        out = health_panel({"generated": local().strftime(TIME), "health": health.load(data)}, env={}, **kw)
        self.assertIn("Box health", out)
        return out

    def g(self, profile, bot=None, tabs=None, **kw):
        return {"profile": profile, "bot": bot, "procs": 3, "rss_mb": 2048, "cpu": 12.0, "port": None,
                "main_pid": 40, "started_at": 1, "closable": True, **({"tabs": tabs} if tabs is not None else {}), **kw}

    def test_tabs_nested_under_their_browser(self):
        out = self.panel([self.g("Fork-9", "Zoe", [{"title": "Docs", "origin": "a.example"},
                                                  {"title": "Mail", "origin": "b.example"}]),
                          self.g("Fork-3", "Ann", [{"title": "Board", "origin": "c.example"}], main_pid=41)],
                         csrf="tok", control=True)
        zoe, ann = out.split('<div class="h-br">')[1:3]
        self.assertIn("Zoe", zoe)
        self.assertIn("Docs", zoe)
        self.assertIn("Mail", zoe)
        self.assertNotIn("Board", zoe)
        self.assertIn("Board", ann)
        self.assertIn('<details class="h-br-tabs"><summary>', zoe)
        self.assertIn("2 tabs", zoe)
        self.assertIn("1 tab</span>", ann)
        self.assertIn("2.0 GB", zoe)
        self.assertIn("12% CPU", zoe)
        # Close is in the header row but outside <summary>, so clicking it can't toggle the group
        self.assertIn("Close browser", zoe)
        self.assertLess(zoe.index("</details>"), zoe.index("Close browser"))
        self.assertNotIn("open tabs", out)  # no separate, ungrouped tab list any more

    def test_fallback_label_and_unmapped_headers(self):
        out = self.panel([self.g("chrome-a", None, [], label="Work"), self.g("chrome-b", None)])
        a, b = out.split('<div class="h-br">')[1:3]
        self.assertIn('<span class="h-br-name">Work <span class="muted">chrome-a · profile name</span>', a)
        self.assertIn('<span class="h-br-name">chrome-b <span class="muted">unmapped</span>', b)
        self.assertIn("tabs –", b)  # tabs not read
        self.assertIn('class="h-br-tabs h-br-empty"', a)  # nothing to expand
        self.assertNotIn("Close browser", out)  # control off

    def test_close_only_when_closable(self):
        out = self.panel([self.g("Fork-9", "Zoe", [], closable=False)], csrf="tok", control=True)
        self.assertNotIn("Close browser", out)

    def test_tab_titles_escaped(self):
        out = self.panel([self.g("Fork-9", "Zoe", [{"title": "<b>x</b>", "origin": "a.example"}])])
        self.assertNotIn("<b>x", out)
        self.assertIn("&lt;b&gt;x", out)


class NoFollowAtOpen(unittest.TestCase):
    def test_o_nofollow_catches_a_swap_after_the_realpath_check(self):
        """A symlink planted between the realpath check and open() is still refused (O_NOFOLLOW)."""
        d = realtmp(self)
        (d / "real").write_text("Fork-1\tAnn\n")
        (d / "names.tsv").symlink_to(d / "real")
        with mock.patch.object(health.os.path, "realpath", side_effect=lambda p: p):  # check passes, as in a race
            self.assertIsNone(health._read_small(str(d / "names.tsv")))
            self.assertEqual(health.names_from_file(str(d / "real")), {"Fork-1": "Ann"})
