import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

import test_serve

from botroll import health, serve
from botroll.board import health_panel, page, snapshot

FAKE_TOKEN = "gh" + "p_" + "FAKEFAKE" * 4 + "0000"  # 40 chars, ghp_ shape, never a real token

FIX = pathlib.Path(__file__).parent / "fixtures" / "health"


def fx(name):
    return (FIX / name).read_text()


def cfg():
    return health.load_config(str(FIX / "config.json"))


def rows():
    return health.parse_ps(fx("ps_before.txt")), health.parse_ps(fx("ps_after.txt"))


class Parsers(unittest.TestCase):
    def test_meminfo(self):
        m = health.parse_meminfo(fx("meminfo.txt"))
        self.assertEqual(m, {"total_mb": 16000, "available_mb": 4400, "swap_total_mb": 0, "swap_free_mb": 0})

    def test_meminfo_without_memavailable_falls_back(self):
        self.assertEqual(health.parse_meminfo("MemTotal: 2048 kB\nMemFree: 512 kB\nCached: 512 kB\n")["available_mb"], 1)

    def test_meminfo_rejects_junk(self):
        with self.assertRaises(ValueError):
            health.parse_meminfo("nothing here")

    def test_loadavg(self):
        self.assertEqual(health.parse_loadavg(fx("loadavg.txt")), [38.12, 26.3, 19.27])

    def test_ps_skips_junk_and_keeps_args(self):
        before, _ = rows()
        self.assertEqual(len(before), 12)
        self.assertEqual(before[5], {"pid": 201, "ppid": 200, "rss_kb": 316384, "elapsed_s": 51976, "cpu_s": 50,
                                     "args": "/opt/google/chrome/chrome --type=renderer "
                                             "--user-data-dir=/home/me/chrome-profile/Fork-15"})

    def test_cpu_percent_over_interval(self):
        cpu = health.cpu_percent(*rows(), 2.0)
        self.assertEqual(cpu[101], 100.0)
        self.assertEqual(cpu[102], 0.0)
        self.assertEqual(health.cpu_percent([], [{"pid": 9, "cpu_s": 5}], 2.0), {9: 0.0})  # new pid: no baseline

    def test_flag(self):
        self.assertEqual(health.flag('x --user-data-dir="/a b" --y', "user-data-dir"), "/a b")
        self.assertIsNone(health.flag("x --other=1", "user-data-dir"))


class Grouping(unittest.TestCase):
    def setUp(self):
        before, self.after = rows()
        self.cpu = health.cpu_percent(before, self.after, 2.0)

    def test_browsers_grouped_by_profile_and_mapped(self):
        b = health.group_browsers(self.after, self.cpu, cfg())
        self.assertEqual([(g["bot"], g["procs"], g["rss_mb"], g["port"]) for g in b],
                         [("researcher", 3, 841, 9231), ("builder", 2, 538, 9245)])
        self.assertEqual(b[0]["cpu"], 150.0)

    def test_unmapped_profile_has_no_bot(self):
        b = health.group_browsers(self.after, self.cpu, health.load_config(None))
        self.assertEqual({g["bot"] for g in b}, {None})

    def test_helpers_flag_stale(self):
        h = health.helpers(self.after, self.cpu, cfg())
        self.assertEqual([(x["pid"], x["rss_mb"], x["stale"], x["port"]) for x in h],
                         [(300, 1754, True, 9231), (301, 117, False, 9245)])

    def test_helper_threshold_configurable(self):
        c = cfg() | {"helper_stale_hours": 20}
        self.assertFalse(any(x["stale"] for x in health.helpers(self.after, self.cpu, c)))

    def test_heavy_jobs_count_a_job_tree_once(self):
        j = health.heavy_jobs(self.after, self.cpu, cfg())
        self.assertEqual([(x["pid"], x["kind"]) for x in j], [(500, "ffmpeg"), (400, "npm run")])


class Alerts(unittest.TestCase):
    def test_overlap_needs_two(self):
        self.assertEqual(health.overlaps([{"kind": "ffmpeg", "pid": 1}]), [])
        o = health.overlaps([{"kind": "ffmpeg", "pid": 1}, {"kind": "vitest", "pid": 2}])
        self.assertEqual(o, [{"kind": "overlap", "text": "2 heavy jobs at once: ffmpeg (1), vitest (2)"}])

    def test_stall_only_when_load_over_cores(self):
        jobs = [{"kind": "ffmpeg", "pid": 1, "elapsed_s": 4000, "expect_s": 1800},
                {"kind": "vitest", "pid": 2, "elapsed_s": 60, "expect_s": 900}]
        self.assertEqual(health.stalls(jobs, [], 8.0, 8), [])
        s = health.stalls(jobs, [{"pid": 3, "stale": True, "elapsed_s": 9000}], 9.5, 8)
        self.assertEqual([x["pid"] for x in s], [1, 3])
        self.assertIn("ffmpeg (1) running 1h 06m, expected under 30m, with load 9.5 on 8 cores", s[0]["text"])

    def test_duration(self):
        self.assertEqual([health.duration(s) for s in (59, 3600, 36026, 90000)], ["0m", "1h 00m", "10h 00m", "1d 1h"])


class Snapshot(unittest.TestCase):
    def build(self, **kw):
        return health.build(fx("meminfo.txt"), fx("loadavg.txt"), 8, *rows(), 2.0, cfg(), ts=1_800_000_000,
                            host="test-box", **kw)

    def test_build_whole_snapshot(self):
        s = self.build(tabs=lambda port: [{"title": f"t{port}", "origin": "https://x"}])
        self.assertTrue(health.valid(s))
        self.assertEqual(s["browsers"][0]["tabs"], [{"title": "t9231", "origin": "https://x"}])
        self.assertEqual([a["kind"] for a in s["alerts"]], ["overlap", "stall", "stall", "stall"])
        json.dumps(s)

    def test_stale_helper_without_load_is_not_a_stall(self):
        s = health.build(fx("meminfo.txt"), "1.0 1.0 1.0", 8, *rows(), 2.0, cfg(), ts=1)
        self.assertEqual([(a["kind"], a.get("pid")) for a in s["alerts"]], [("overlap", None), ("stale-helper", 300)])

    def test_no_tabs_key_without_reader(self):
        self.assertNotIn("tabs", self.build()["browsers"][0])

    def test_tabs_keep_pages_and_origins_only(self):
        self.assertEqual(health.parse_tabs(fx("tabs.json").encode()),
                         [{"title": "mail.example.com", "origin": "https://mail.example.com"},  # #inbox is in the URL
                          {"title": "newtab", "origin": "chrome://newtab"}])  # not http(s): the host

    def test_config_rejects_unknown_keys(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = pathlib.Path(d, "c.json")
        p.write_text('{"browserz": {}}')
        with self.assertRaises(ValueError):
            health.load_config(str(p))

    def test_valid_rejects_bad_shapes(self):
        s = self.build()
        for bad in [None, [], s | {"v": 2}, s | {"ts": "1"}, s | {"load": [1, 2]}, s | {"cores": 0},
                    s | {"browsers": ["x"]}, {k: v for k, v in s.items() if k != "mem"}]:
            self.assertFalse(health.valid(bad), bad)


class Store(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.base = health.build(fx("meminfo.txt"), fx("loadavg.txt"), 8, *rows(), 2.0, cfg(), ts=0)

    def test_append_prunes_to_24h(self):
        t = 1_800_000_000
        for i in reversed(range(30)):  # one sample an hour for 30 hours, time moving forward
            health.append(self.d, self.base | {"ts": t - 3600 * i}, at=t - 3600 * i)
        health.append(self.d, self.base | {"ts": t + 1}, at=t + 1)
        kept = health.load(self.d)
        self.assertEqual(len(kept), 25)
        self.assertEqual(kept[-1]["ts"], t + 1)
        self.assertEqual(kept[0]["ts"], t - 23 * 3600)

    def test_append_refuses_invalid(self):
        with self.assertRaises(ValueError):
            health.append(self.d, {"v": 1})

    def test_load_missing_or_corrupt(self):
        self.assertEqual(health.load(self.d), [])
        pathlib.Path(self.d, health.HEALTH).write_text("{nope")
        self.assertEqual(health.load(self.d), [])

    def test_hourly_peaks_attribute_bot(self):
        t = 1_800_000_000
        samples = [self.base | {"ts": t - 60, "load": [4.0, 1, 1]}, self.base | {"ts": t - 120}]
        peaks = health.hourly_peaks(samples, t)
        self.assertEqual(len(peaks), 24)
        self.assertEqual((peaks[-1]["load"], peaks[-1]["bot"]), (38.12, "researcher"))
        self.assertIsNone(peaks[0])

    def test_thresholds_and_levels(self):
        th = health.thresholds({"BOTROLL_HEALTH_MEM_AMBER": "30", "BOTROLL_HEALTH_LOAD_RED": "x"})
        self.assertEqual((th["mem_amber"], th["load_red"]), (30.0, 2.0))
        self.assertEqual(health.level(27.5, 30, 10, lower_is_worse=True), "amber")
        self.assertEqual(health.level(4.8, 1, 2), "red")
        self.assertEqual(health.level(0.5, 1, 2), "ok")



EXAMPLES = pathlib.Path(__file__).parent.parent / "examples" / "tasks"


def sample_at(ts, **over):
    s = health.build(fx("meminfo.txt"), fx("loadavg.txt"), 8, *rows(), 2.0, cfg(), ts=ts, host="test-box",
                     tabs=lambda port: [{"title": "<b>x</b>", "origin": "https://ex"}])
    return s | over


class Panel(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        for f in EXAMPLES.glob("*.md"):
            shutil.copy(f, self.d)
        self.at = test_serve.NOW

    def test_off_by_default(self):
        snap = snapshot(self.d, self.at, {})
        self.assertNotIn("health", snap)
        self.assertEqual(health_panel(snap), "")
        self.assertNotIn("Box health", page(snap))

    def test_panel_shows_latest_sample(self):
        ts = int(self.at.timestamp()) - 60
        health.append(self.d, sample_at(ts - 3600, load=[2.0, 1, 1]), at=ts)
        health.append(self.d, sample_at(ts), at=ts)
        html = page(snapshot(self.d, self.at, {}))
        self.assertIn("Box health", html)
        self.assertIn("4.3 GB free", html)
        self.assertIn("38.1 on 8 cores", html)
        self.assertIn('class="gauge ok"', html)  # 27% free is above the default amber line of 25%
        self.assertIn('class="gauge red"', html)  # load 4.8x
        self.assertIn("CPU-bound, not out of memory", html)
        self.assertIn("researcher", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("busiest: researcher", html)
        self.assertNotIn("style=", html)  # the CSP allows no inline styles

    def test_thresholds_from_env(self):
        ts = int(self.at.timestamp())
        health.append(self.d, sample_at(ts), at=ts)
        snap = snapshot(self.d, self.at, {})
        self.assertIn('class="gauge ok"', health_panel(snap, {"BOTROLL_HEALTH_LOAD_AMBER": "10",
                                                               "BOTROLL_HEALTH_LOAD_RED": "20",
                                                               "BOTROLL_HEALTH_MEM_AMBER": "5",
                                                               "BOTROLL_HEALTH_MEM_RED": "1"}))

    def test_stale_banner(self):
        ts = int(self.at.timestamp()) - 3600
        health.append(self.d, sample_at(ts), at=ts)
        self.assertIn("No box-health sample for 1h", health_panel(snapshot(self.d, self.at, {})))


class Api(test_serve.Server):
    def setUp(self):
        super().setUp()
        self.cfg.health = True

    def test_push_and_render(self):
        snap = sample_at(int(test_serve.NOW.timestamp()))
        with mock.patch("time.time", return_value=test_serve.NOW.timestamp()):
            r = self.push("/api/health", json.dumps(snap).encode(), Content_Type="application/json")
        self.assertEqual((r.status, json.loads(r.body)), (200, {"samples": 1}))
        self.assertIn("Box health", self.request("GET", "/", cookies=self.session()).body)

    def test_needs_token_and_valid_body(self):
        self.assertEqual(self.request("POST", "/api/health", b"{}").status, 401)
        for body in [b"{}", b"nope", b'{"v": 1}']:
            self.assertEqual(self.push("/api/health", body).status, 400)
        self.assertEqual(self.push("/api/health", b"x" * (health.MAX_SNAPSHOT + 1)).status, 413)
        self.assertFalse(os.path.exists(os.path.join(self.data, health.HEALTH)))
        self.assertNotIn("Box health", self.request("GET", "/", cookies=self.session()).body)


# Kai's post-release findings on #5 ------------------------------------------------

def good(ts=1_800_000_000):
    return sample_at(ts)


class Hardening(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.now = 1_800_000_000

    def put(self, snap):
        return health.append(self.d, snap, at=self.now)

    def render(self, samples):
        snap = {"generated": "2027-01-15 19:00", "health": samples}
        return health_panel(snap)

    # H1
    def test_h1_pid_injection_is_rejected_at_ingest(self):
        s = good(self.now)
        s["helpers"][0]["pid"] = '<a href="https://evil.example">x</a>'
        with self.assertRaises(ValueError):
            self.put(s)
        for key, where in [("procs", "browsers"), ("pid", "jobs")]:
            bad = good(self.now)
            bad[where][0][key] = "<a href=x>"
            with self.assertRaises(ValueError):
                self.put(bad)

    def test_h1_renderer_escapes_even_if_a_string_got_stored(self):
        s = health.clean(good(self.now))
        with mock.patch.object(health, "clean", side_effect=lambda x: x):
            s["helpers"][0]["pid"] = '<a href="https://evil.example">x</a>'
            s["jobs"][0]["pid"] = "<a href=j>"
            s["browsers"][0]["procs"] = "<a href=p>"
            html = self.render([s])
        self.assertNotIn("<a href", html)
        self.assertIn("&lt;a href=&quot;https://evil.example&quot;&gt;", html)

    # H2
    def test_h2_string_numbers_and_text_tabs_rejected(self):
        for path in [("browsers", 0, "cpu"), ("browsers", 0, "rss_mb"), ("helpers", 0, "elapsed_s"),
                     ("browsers", 0, "tabs")]:
            s = good(self.now)
            s[path[0]][path[1]][path[2]] = "lots"
            with self.assertRaises(ValueError, msg=path):
                self.put(s)

    def test_h2_bad_stored_sample_is_skipped_not_fatal(self):
        bad, ok = good(self.now), good(self.now - 60)
        bad["browsers"][0]["cpu"] = "99"
        pathlib.Path(self.d, health.HEALTH).write_text(json.dumps([ok, bad]))
        self.assertEqual([x["ts"] for x in health.load(self.d)], [self.now - 60])
        self.assertIn("Box health", self.render([ok, bad]))
        self.assertEqual(self.render([bad]), "")

    def test_h2_renderer_falls_back_when_drawing_fails(self):
        ok, bad = health.clean(good(self.now - 60)), health.clean(good(self.now))
        with mock.patch.object(health, "clean", side_effect=lambda x: x):
            bad["helpers"][0]["elapsed_s"] = "x"
            self.assertIn("Box health", self.render([ok, bad]))

    def test_h2_future_samples_rejected_422(self):
        with self.assertRaises(health.Rejected) as c:
            self.put(good(self.now + 61))
        self.assertEqual(c.exception.status, 422)
        self.put(good(self.now + 60))

    # M1 (token assembled from pieces so secret scanners do not flag a fake)
    def test_m1_userinfo_stripped_from_tab_origins(self):
        raw = json.dumps([{"type": "page", "title": "t",
                           "url": "https://x-access-token:" + FAKE_TOKEN + "@github.com:8443/a?b"}])
        tabs = health.parse_tabs(raw.encode())
        self.assertEqual(tabs, [{"title": "t", "origin": "https://github.com:8443"}])
        self.assertNotIn("ghp_", json.dumps(tabs))
        self.assertEqual(health.origin("http://user:pw@[::1]:9/x"), "http://[::1]:9")

    # L1
    def test_l1_store_total_size_is_capped(self):
        with mock.patch.object(health, "MAX_STORE", 3 * len(json.dumps(health.clean(good())))):
            for i in range(6):
                self.put(good(self.now - 600 + i * 60))
            kept = health.load(self.d)
        self.assertLessEqual(len(kept), 3)
        self.assertEqual(kept[-1]["ts"], self.now - 300)

    def test_l1_oversize_sample_rejected(self):
        s = good(self.now)
        s["alerts"] = [{"kind": "stall", "text": "x" * 300}] * 64
        s["browsers"] = [s["browsers"][0] | {"tabs": [{"title": "t" * 60, "origin": "o" * 120}] * 64}] * 8
        with self.assertRaises(ValueError):
            self.put(s)

    # L2
    def test_l2_unknown_fields_dropped(self):
        s = good(self.now) | {"evil": 1}
        s["browsers"][0]["script"] = "<script>"
        self.put(s)
        kept = health.load(self.d)[0]
        self.assertNotIn("evil", kept)
        self.assertNotIn("script", kept["browsers"][0])

    def test_l2_nan_infinity_and_bools_are_not_numbers(self):
        for v in [float("nan"), float("inf"), float("-inf"), True, False]:
            s = good(self.now)
            s["browsers"][0]["cpu"] = v
            with self.assertRaises(ValueError, msg=v):
                self.put(s)
        s = good(self.now)
        s["cores"] = True
        with self.assertRaises(ValueError):
            self.put(s)

    def test_l2_out_of_window_and_replay(self):
        with self.assertRaises(health.Rejected) as c:
            self.put(good(self.now - 25 * 3600))
        self.assertEqual(c.exception.status, 422)
        self.put(good(self.now))
        for ts in (self.now, self.now - 60):
            with self.assertRaises(health.Rejected) as c:
                self.put(good(ts))
            self.assertEqual(c.exception.status, 409)

    # L3
    def test_l3_command_lines_redacted(self):
        before, after = rows()
        after = after + [{"pid": 900, "ppid": 1, "rss_kb": 1, "elapsed_s": 1, "cpu_s": 0,
                          "args": "/usr/bin/ffmpeg -i https://u:ghp_secret@h/x?token=abc out.mp4"}]
        s = health.build(fx("meminfo.txt"), fx("loadavg.txt"), 8, before, after, 2.0, cfg(), ts=1)
        cmds = [x["cmd"] for x in s["helpers"] + s["jobs"]]
        self.assertIn("ffmpeg", cmds)
        self.assertIn("node (helper)", cmds)
        self.assertIn("npm (npm run)", cmds)
        self.assertNotIn("ghp_", json.dumps(s))
        self.assertNotIn("/var/tmp", json.dumps(s))
        self.assertEqual({b["profile"] for b in s["browsers"]}, {"Fork-9", "Fork-15"})

    def test_l3_titles_truncated_and_optional(self):
        raw = json.dumps([{"type": "page", "title": "T" * 100, "url": "https://a.example/"}]).encode()
        self.assertEqual(len(health.parse_tabs(raw)[0]["title"]), 60)
        self.assertEqual(health.parse_tabs(raw, titles=False)[0]["title"], "")
        self.assertFalse(health.load_config(None) | {"tab_titles": False} == health.load_config(None))

    # N1
    def test_n1_url_like_titles_become_the_host(self):
        def tab(title, url):
            raw = json.dumps([{"type": "page", "title": title, "url": url}]).encode()
            return health.parse_tabs(raw)[0]["title"]
        url = "https://app.example.com/callback?token=s3cret"
        for title in (url, "app.example.com/callback?token=s3cret", "app.example.com/callback"):
            with self.subTest(title=title):
                self.assertEqual(tab(title, url), "app.example.com")
        self.assertEqual(tab("data:text/html,?x", "data:text/html,?x"), "Untitled")
        self.assertEqual(tab("Inbox (3)", url), "Inbox (3)")
        self.assertNotIn("s3cret", json.dumps(health.parse_tabs(json.dumps([{"type": "page", "title": url,
                                                                               "url": url}]).encode())))

    # N1, round 2: safe by construction
    def tab(self, title, url):
        return health.parse_tabs(json.dumps([{"type": "page", "title": title, "url": url}]).encode())[0]["title"]

    def test_n1_repros(self):
        cases = [
            ("127.0.0.1:8786/a b/SECRET_SP.html", "http://127.0.0.1:8786/a%20b/SECRET_SP.html", "127.0.0.1"),
            ("127.0.0.1:8786/SECRET%2541.html", "http://127.0.0.1:8786/SECRET%2541.html", "127.0.0.1"),
            ("127.0.0.1:8786/SECRETA.html", "http://127.0.0.1:8786/SECRET%2541.html", "127.0.0.1"),
            ("data:text/html,<p>DATA_SECRET</p>", "data:text/html,<p>DATA_SECRET</p>", "Untitled"),
        ]
        for title, url, want in cases:
            with self.subTest(title=title):
                self.assertEqual(self.tab(title, url), want)
                self.assertNotIn("SECRET", self.tab(title, url))

    def test_n1_each_rule(self):
        self.assertEqual(health._decode("SECRET%252541"), "SECRETA")
        for title, url, want in [
            ("blob:https://a.example/x", "https://a.example/page", "a.example"),          # (a) scheme
            ("about:blank", "https://a.example/", "a.example"),                          # (a)
            ("Note: hi", "https://a.example/", "a.example"),                              # (a): "word:" too, by design
            ("A.Example/whatever", "https://a.example/", "a.example"),                    # (b) host, any case
            ("a.example:8443/x", "https://a.example:8443/", "a.example"),                 # (b) host:port/
            ("Order 99 · ToKeN1234 here", "https://a.example/o?id=token1234", "a.example"),  # (c) query value
            ("frag Section-Two", "https://a.example/#section-two", "a.example"),          # (c) fragment
            ("Docs", "file:///home/me/secret.html", "Untitled"),                         # (d) not http(s)
            ("Settings", "chrome://settings/", "settings"),                               # (d): host only
            ("", "https://a.example/x", "a.example"),
        ]:
            with self.subTest(title=title, url=url):
                self.assertEqual(self.tab(title, url), want)

    def test_n1_ipv6_hosts(self):
        url = "http://[::1]:8786/SECRET_V6.html"
        for title in ("[::1]:8786/SECRET_V6.html", "[::1]/x", "::1:8786/x", "[::1]"):
            with self.subTest(title=title):
                self.assertEqual(self.tab(title, url), "::1")
        self.assertEqual(self.tab("Local dashboard", url), "Local dashboard")

    def test_n1_normal_titles(self):
        self.assertEqual(self.tab("GitHub - saadkamal/botroll", "https://github.com/"), "GitHub - saadkamal/botroll")
        self.assertEqual(self.tab("Inbox (3) - Gmail", "https://mail.google.com/u/0/"), "Inbox (3) - Gmail")
        # (c) is a plain substring check, so a word that is also a path segment drops the title: /mail/ vs "Gmail".
        self.assertEqual(self.tab("Inbox (3) - Gmail", "https://mail.google.com/mail/u/0/"), "mail.google.com")
        # Documented trade-off: a title that repeats a 4+ character path segment is dropped (safety first).
        self.assertEqual(self.tab("GitHub - saadkamal/botroll", "https://github.com/saadkamal/botroll"), "github.com")

    # N5
    def test_n5_replays_keyed_on_host_and_time(self):
        self.put(good(self.now) | {"host": "a"})
        self.put(good(self.now) | {"host": "b"})  # same time, another box: fine
        with self.assertRaises(health.Rejected):
            self.put(good(self.now) | {"host": "a"})
        self.put(good(self.now + 1) | {"host": "a"})
        self.assertEqual(len(health.load(self.d)), 3)

    # N6
    def test_n6_collector_applies_server_caps(self):
        before, after = rows()
        many = [{"pid": 1000 + i, "ppid": 1, "rss_kb": 1000, "elapsed_s": 9000, "cpu_s": 0,
                 "args": f"node /x/playwright-runtime/{'y' * 300}"} for i in range(200)]
        weird = "\x07" + "z" * 200
        many += [{"pid": 5000 + i, "ppid": 1, "rss_kb": 1000, "elapsed_s": 1, "cpu_s": 0,
                  "args": f"/opt/google/chrome/chrome --user-data-dir=/p/{weird}{i} "
                          "--remote-debugging-port=9999"} for i in range(200)]
        tabs = [{"title": "\x01" + "T" * 200, "origin": "https://" + "o" * 300}] * 200
        s = health.build(fx("meminfo.txt"), fx("loadavg.txt"), 8, before, after + many, 2.0, cfg(), ts=self.now,
                         host="h" * 500, tabs=lambda port: tabs)
        self.assertLessEqual(len(json.dumps(s)), health.MAX_SNAPSHOT)
        self.assertTrue(health.valid(s))
        self.assertEqual(self.put(s), 1)

    # I1
    def test_i1_chart_includes_the_newest_sample(self):
        peaks = health.hourly_peaks([health.clean(good(self.now)) | {"load": [30.0, 1, 1]}], self.now)
        self.assertIsNotNone(peaks[-1])
        self.assertEqual(peaks[-1]["load"], 30.0)


class ApiHardening(test_serve.Server):
    def snap(self, ts):
        return json.dumps(sample_at(ts)).encode()

    def test_i2_off_unless_botroll_health_is_set(self):
        r = self.push("/api/health", self.snap(int(time.time())))
        self.assertEqual(r.status, 404)
        self.assertIn("BOTROLL_HEALTH", r.body)
        self.assertFalse(serve.configure(self.data, {"BOTROLL_PASSWORD_HASH": test_serve.HASH,
                                                     "BOTROLL_SECRET": test_serve.SECRET}).health)
        self.assertTrue(serve.configure(self.data, {"BOTROLL_PASSWORD_HASH": test_serve.HASH,
                                                    "BOTROLL_SECRET": test_serve.SECRET,
                                                    "BOTROLL_HEALTH": "1"}).health)

    def test_status_codes_and_rate_limit(self):
        self.cfg.health = True
        now = int(time.time()) - 30
        bad = sample_at(now)
        bad["browsers"][0]["cpu"] = "lots"
        self.assertEqual(self.push("/api/health", json.dumps(bad).encode()).status, 400)
        self.assertEqual(self.push("/api/health", self.snap(now + 3600)).status, 422)
        self.assertEqual(self.push("/api/health", self.snap(now)).status, 200)
        self.assertEqual(self.push("/api/health", self.snap(now)).status, 409)
        for i in range(1, 6):  # N4: the 3 rejected ones above don't use up the 6 accepted slots
            self.assertEqual(self.push("/api/health", self.snap(now + i)).status, 200, i)
        r = self.push("/api/health", self.snap(now + 6))
        self.assertEqual((r.status, r.headers["Retry-After"]), (429, "60"))

    def test_n4_rejected_pushes_never_block_accepted_ones(self):
        self.cfg.health = True
        now = int(time.time()) - 30
        codes = [self.push("/api/health", b"nope").status for _ in range(32)]
        self.assertEqual(codes, [400] * serve.HEALTH_BAD_PER_MINUTE + [429] * (32 - serve.HEALTH_BAD_PER_MINUTE))
        self.assertEqual([self.push("/api/health", self.snap(now + i)).status for i in range(6)], [200] * 6)
        self.assertEqual(self.push("/api/health", self.snap(now + 6)).status, 429)

    def test_n3_no_panel_when_health_is_off(self):
        now = int(time.time())
        health.append(self.data, sample_at(now), at=now)  # stored while it was on
        self.assertNotIn("Box health", self.request("GET", "/", cookies=self.session()).body)
        self.cfg.health = True
        self.assertIn("Box health", self.request("GET", "/", cookies=self.session()).body)


if __name__ == "__main__":
    unittest.main()


class ProfileResolve(unittest.TestCase):
    def test_map_beats_command(self):
        cfg = health.load_config(None)
        cfg["browsers"] = {"Fork-9": "builder"}
        cfg["profile_resolve"] = "echo should-not-run"
        self.assertEqual(health.resolve_profile("/x/Fork-9", cfg), "builder")

    def test_command_hook_and_failure(self):
        cfg = health.load_config(None)
        cfg["profile_resolve"] = "printf 'researcher\\n'"
        self.assertEqual(health.resolve_profile("/tmp/Fork-3", cfg), "researcher")
        cfg["profile_resolve"] = "exit 1"
        self.assertIsNone(health.resolve_profile("/tmp/Fork-3", cfg))
        cfg["profile_resolve"] = "sleep 5"
        self.assertIsNone(health.resolve_profile("/tmp/Fork-3", cfg))  # timeout

    def test_never_guesses(self):
        cfg = health.load_config(None)
        cfg["display_names_dir"] = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg["display_names_dir"])
        self.assertIsNone(health.resolve_profile("/home/me/chrome-profile/Fork-9", cfg))


class BucketPeaks(unittest.TestCase):
    def test_bucket_min_and_peaks(self):
        at = 1_000_000.0
        samples = []
        for i in range(0, 3600, 60):
            samples.append({"ts": at - 3600 + i, "cores": 2, "load": [2.0 if i == 1800 else 0.5, 0, 0],
                            "mem": {"available_mb": 1000, "total_mb": 2000},
                            "browsers": [{"profile": "Fork-1", "bot": "ann", "cpu": 10, "rss_mb": 100}],
                            "helpers": [], "jobs": [], "alerts": [], "host": "b", "v": 1})
        peaks = health.bucket_peaks(samples, at, bucket_min=30, hours=1)
        self.assertEqual(len(peaks), 2)
        self.assertEqual(health.thresholds({"BOTROLL_HEALTH_BUCKET_MIN": "15"})["bucket_min"], 15)
        self.assertEqual(health.thresholds({"BOTROLL_HEALTH_BUCKET_MIN": "7"})["bucket_min"], 30)


class ControlUnit(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)
        self.cfg = health.load_config(None)

    def test_enqueue_pending_expire_rate(self):
        t = {"pid": 4242, "started_at": 100, "profile": "Fork-3"}
        row = health.enqueue_command(self.data, "close-browser", t)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(len(health.pending_commands(self.data, at=row["created_at"])), 1)
        with self.assertRaises(ValueError):
            health.enqueue_command(self.data, "close-browser", t)  # rate limit
        # expire
        self.assertEqual(health.pending_commands(self.data, at=row["created_at"] + health.COMMAND_TTL + 1), [])

    def test_allowed_target_classes(self):
        # Full guard behaviour lives in test_control.py; these are the cheap early refusals.
        on = {**self.cfg, "control": True}
        self.assertEqual(health.allowed_target("close-browser", {"pid": 1, "started_at": 0, "profile": "x"}, on, None),
                         "bad pid")
        self.assertEqual(health.allowed_target("stop-helper", {"pid": 999999, "started_at": 0}, on, None),
                         "not on the latest local sample")
        self.assertEqual(health.allowed_target("stop-job", {"pid": os.getpid(), "started_at": int(time.time())},
                                               on, None), "refusing collector pid")
        self.assertEqual(health.allowed_target("stop-job", {"pid": 4242, "started_at": 1}, self.cfg, None),
                         "control off")

    def test_finish_and_results(self):
        t = {"pid": 55, "started_at": 1}
        row = health.enqueue_command(self.data, "stop-helper", t)
        self.assertIsNone(health.finish_command(self.data, row["id"], "ok", "terminated"))  # no nonce
        self.assertEqual(len(health.pending_commands(self.data)), 1)
        health.finish_command(self.data, row["id"], "ok", "terminated", row["nonce"])
        self.assertEqual(health.pending_commands(self.data), [])
        self.assertEqual(health.recent_results(self.data)[-1]["status"], "ok")


class ControlLive(test_serve.Server):
    def setUp(self):
        super().setUp()
        self.cfg.health = True
        self.cfg.health_control = True
        snap = {
            "v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
            "mem": {"total_mb": 1000, "available_mb": 500, "swap_total_mb": 0, "swap_free_mb": 0},
            "browsers": [{"profile": "Fork-3", "bot": "ann", "procs": 1, "rss_mb": 100, "cpu": 1.0,
                          "port": None, "main_pid": 4242, "started_at": 1000, "closable": True, "tabs": []}],
            "helpers": [{"pid": 4243, "rss_mb": 50, "cpu": 0.0, "elapsed_s": 10, "started_at": 1001,
                         "stale": False, "port": None, "cmd": "node (helper)"}],
            "jobs": [], "alerts": [],
        }
        health.append(self.data, snap)

    def _session(self):
        r = self.request("GET", "/login")
        cookies = r.cookies()
        import re
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.body)[1]
        r = self.request("POST", "/login", {"password": test_serve.PASSWORD, "csrf": csrf}, cookies=cookies)
        return {**cookies, **r.cookies()}

    def test_buttons_csrf_enqueue_and_api(self):
        import json
        import re
        cookies = self._session()
        board = self.request("GET", "/", cookies=cookies)
        self.assertIn("Close browser", board.body)
        self.assertIn('action="/health/control"', board.body)
        csrf = re.search(r'name="csrf" value="([^"]+)"', board.body)[1]
        self.assertEqual(self.request("POST", "/health/control", {
            "csrf": "nope", "kind": "close-browser", "profile": "Fork-3", "pid": "4242", "started_at": "1000"
        }, cookies=cookies).status, 403)
        r = self.request("POST", "/health/control", {
            "csrf": csrf, "kind": "close-browser", "profile": "Fork-3", "pid": "4242", "started_at": "1000"
        }, cookies=cookies)
        self.assertEqual(r.status, 303)
        pending = health.pending_commands(self.data)
        self.assertEqual(len(pending), 1)
        api = self.request("GET", "/api/health/commands",
                           headers={"Authorization": f"Bearer {test_serve.TOKEN}"})
        self.assertEqual(api.status, 200)
        self.assertEqual(len(json.loads(api.body)["commands"]), 1)
        cid, nonce = pending[0]["id"], pending[0]["nonce"]
        forged = self.request("POST", "/api/health/commands/result",
                              json.dumps({"id": cid, "nonce": "0" * 32, "status": "ok"}).encode(),
                              headers={"Authorization": f"Bearer {test_serve.TOKEN}",
                                       "Content-Type": "application/json"})
        self.assertEqual(forged.status, 404)
        self.assertEqual(len(health.pending_commands(self.data)), 1)
        done = self.request("POST", "/api/health/commands/result",
                            json.dumps({"id": cid, "nonce": nonce, "status": "ok", "detail": "terminated"}).encode(),
                            headers={"Authorization": f"Bearer {test_serve.TOKEN}",
                                     "Content-Type": "application/json"})
        self.assertEqual(done.status, 200)
        self.assertEqual(health.pending_commands(self.data), [])

    def test_disabled_without_flag(self):
        import re
        health.enqueue_command(self.data, "stop-helper", {"pid": 4243, "started_at": 1001})
        self.cfg.health_control = False
        cookies = self._session()
        board = self.request("GET", "/", cookies=cookies)
        self.assertNotIn("Close browser", board.body)
        csrf = re.search(r'name="csrf" value="([^"]+)"', board.body)[1]
        self.assertEqual(self.request("POST", "/health/control", {
            "csrf": csrf, "kind": "stop-helper", "pid": "4243", "started_at": "1001"
        }, cookies=cookies).status, 404)
        self.assertEqual(self.request("GET", "/api/health/commands",
                                      headers={"Authorization": f"Bearer {test_serve.TOKEN}"}).status, 404)
        self.assertEqual(len(health.pending_commands(self.data)), 1)  # a GET changes nothing
        self.assertEqual(serve.clear_stale(self.cfg), 1)  # what startup does with control off
        self.assertEqual(health.pending_commands(self.data), [])
        self.assertEqual(health.recent_results(self.data)[-1]["status"], "cancelled")

    def test_close_needs_a_configured_browser(self):
        import re
        samples = health.load(self.data)
        samples[-1]["browsers"][0]["closable"] = False
        health.append(self.data, {**samples[-1], "ts": samples[-1]["ts"] + 1})
        cookies = self._session()
        board = self.request("GET", "/", cookies=cookies)
        self.assertNotIn("Close browser", board.body)
        csrf = re.search(r'name="csrf" value="([^"]+)"', board.body)[1]
        self.assertEqual(self.request("POST", "/health/control", {
            "csrf": csrf, "kind": "close-browser", "profile": "Fork-3", "pid": "4242", "started_at": "1000"
        }, cookies=cookies).status, 400)

    def test_control_defaults_off(self):
        cfg = serve.configure(self.data, {"BOTROLL_PASSWORD_HASH": test_serve.HASH, "BOTROLL_SECRET": test_serve.SECRET,
                                          "BOTROLL_HEALTH": "1"})
        self.assertFalse(cfg.health_control)
        self.assertFalse(health.load_config(None)["control"])

    def test_rejects_target_not_on_latest_sample(self):
        import re
        cookies = self._session()
        csrf = re.search(r'name="csrf" value="([^"]+)"',
                         self.request("GET", "/", cookies=cookies).body)[1]
        self.assertEqual(self.request("POST", "/health/control", {
            "csrf": csrf, "kind": "close-browser", "profile": "Fork-99", "pid": "1", "started_at": "1"
        }, cookies=cookies).status, 400)
