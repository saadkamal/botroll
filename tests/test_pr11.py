"""Close per row, escaped tab origins, node flag lists, and browsers that share a name."""
import os
import pathlib
import shutil
import tempfile
import time
import unittest

from botroll import TIME, health, local
from botroll.board import health_panel


class Panel(unittest.TestCase):
    def panel(self, browsers, **kw):
        data = pathlib.Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, data)
        health.append(str(data), {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
                                  "mem": {"total_mb": 1000, "available_mb": 500}, "browsers": browsers,
                                  "helpers": [], "jobs": [], "alerts": []})
        out = health_panel({"generated": local().strftime(TIME), "health": health.load(str(data))}, env={}, **kw)
        self.assertIn("Box health", out)
        return out

    @staticmethod
    def g(profile, bot, pid, started=1, tabs=None):
        return {"profile": profile, "bot": bot, "procs": 1, "rss_mb": 1, "cpu": 0.0, "port": None,
                "main_pid": pid, "started_at": started, "closable": True, "tabs": tabs or []}

    def rows(self, out):
        return out.split('<div class="h-br">')[1:]

    def test_t1_each_close_carries_its_own_pid(self):
        out = self.panel([self.g("Fork-3", "Ann", 4101, 11), self.g("Fork-9", "Max", 4202, 22)],
                         csrf="tok", control=True)
        ann, max_ = self.rows(out)
        for row, pid, started, profile in ((ann, 4101, 11, "Fork-3"), (max_, 4202, 22, "Fork-9")):
            self.assertEqual(row.count("Close browser"), 1)
            self.assertIn(f'name="pid" value="{pid}"', row)
            self.assertIn(f'name="started_at" value="{started}"', row)
            self.assertIn(f'name="profile" value="{profile}"', row)
        self.assertNotIn("4202", ann)
        self.assertNotIn("4101", max_)

    def test_t2_tab_origins_escaped(self):
        out = self.panel([self.g("Fork-3", "Ann", 4101, tabs=[{"title": "t", "origin": '"><img src=x onerror=1>'}])])
        self.assertNotIn("<img", out)
        self.assertIn("&quot;&gt;&lt;img src=x onerror=1&gt;", out)

    def test_shared_name_shows_profile_in_header_and_confirm(self):
        out = self.panel([self.g("Fork-3", "Ann", 4101), self.g("Fork-9", "Ann", 4202), self.g("Fork-5", "Kai", 4303)],
                         csrf="tok", control=True)
        a3, a9, bob = self.rows(out)
        self.assertIn('<span class="h-br-name">Ann <span class="muted">Fork-3</span>', a3)
        self.assertIn('<span class="h-br-name">Ann <span class="muted">Fork-9</span>', a9)
        self.assertIn('type="submit">Close Fork-3</button>', a3)
        self.assertIn('type="submit">Close Fork-9</button>', a9)
        self.assertIn('<span class="h-br-name">Kai</span>', bob)  # unique name: unchanged
        self.assertIn('type="submit">Confirm</button>', bob)

    def test_shared_name_profile_escaped(self):
        out = self.panel([self.g('a"<b>', "Ann", 4101), self.g("Fork-9", "Ann", 4202)], csrf="tok", control=True)
        self.assertNotIn("<b>", out)
        self.assertIn("Close a&quot;&lt;b&gt;</button>", out)

    def test_same_name_and_folder_get_distinct_labels(self):
        a = {**self.g("Fork-3", "Ann", 4101), "profile_id": health.profile_id("/home/a/Fork-3")}
        b = {**self.g("Fork-3", "Ann", 4202), "profile_id": health.profile_id("/home/b/Fork-3")}
        self.assertNotEqual(a["profile_id"], b["profile_id"])
        out = self.panel([a, b], csrf="tok", control=True)
        ra, rb = self.rows(out)
        self.assertIn(f'Ann <span class="muted">Fork-3 #{a["profile_id"]}</span>', ra)
        self.assertIn(f'Ann <span class="muted">Fork-3 #{b["profile_id"]}</span>', rb)
        self.assertIn(f'type="submit">Close Fork-3 #{a["profile_id"]}</button>', ra)
        self.assertIn(f'type="submit">Close Fork-3 #{b["profile_id"]}</button>', rb)

    def test_same_folder_without_id_falls_back_to_pid(self):
        out = self.panel([self.g("Fork-3", None, 4101), self.g("Fork-3", None, 4202)], csrf="tok", control=True)
        ra, rb = self.rows(out)
        self.assertIn("Fork-3 pid 4101 · unmapped", ra)
        self.assertIn("Fork-3 pid 4202 · unmapped", rb)

    def test_profile_id_from_sampling_and_clean(self):
        rows = [{"pid": p, "ppid": 1, "rss_kb": 1024, "elapsed_s": 5, "cpu_s": 0, "args": a} for p, a in (
            (10, "/usr/bin/chromium --user-data-dir=/home/a/Fork-3/"), (11, "/usr/bin/chromium --user-data-dir=/home/b/Fork-3"))]
        groups = health.group_browsers(rows, {}, {**health.load_config(None), "display_names_dir": None}, at=100)
        self.assertEqual({g["profile_id"] for g in groups}, {health.profile_id("/home/a/Fork-3"), health.profile_id("/home/b/Fork-3")})
        self.assertEqual(health.profile_id("/home/a/Fork-3/"), health.profile_id("/home/a/Fork-3"))
        snap = {"v": 1, "ts": 1, "host": "h", "cores": 1, "load": [0, 0, 0], "mem": {"total_mb": 1, "available_mb": 1},
                "browsers": [{"profile": "x", "bot": None, "procs": 1, "rss_mb": 1, "cpu": 0, "profile_id": pid}
                             for pid in ("0123abcd", "<script>", "ABCDEF12", 5)], "helpers": [], "jobs": [], "alerts": []}
        self.assertEqual([b["profile_id"] for b in health.clean(snap)["browsers"]], ["0123abcd", None, None, None])

    def clean_ids(self, ids):
        snap = {"v": 1, "ts": 1, "host": "h", "cores": 1, "load": [0, 0, 0], "mem": {"total_mb": 1, "available_mb": 1},
                "browsers": [{"profile": "x", "bot": None, "procs": 1, "rss_mb": 1, "cpu": 0, "profile_id": i} for i in ids],
                "helpers": [], "jobs": [], "alerts": []}
        return [b["profile_id"] for b in health.clean(snap)["browsers"]]

    def test_t4_clean_profile_id_exact_eight_hex(self):
        self.assertEqual(self.clean_ids(["0123abc", "0123abcde", "0123abcd", "<b>0123abcd</b>", "0123abcd<b>",
                                         "<b>c23f2ced", "0123abc\n", "0123abcd\n", "", "0123abcg"]),
                         [None, None, "0123abcd", None, None, None, None, None, None, None])

    def test_m1_copied_id_still_gets_distinct_labels(self):
        pid = health.profile_id("/home/a/Fork-3")
        a = {**self.g("Fork-3", "Ann", 4101), "profile_id": pid}
        b = {**self.g("Fork-3", "Ann", 4202), "profile_id": pid}  # a sample copying another browser's id
        out = self.panel([a, b], csrf="tok", control=True)
        ra, rb = self.rows(out)
        self.assertIn(f'Ann <span class="muted">Fork-3 #{pid} pid 4101</span>', ra)
        self.assertIn(f'Ann <span class="muted">Fork-3 #{pid} pid 4202</span>', rb)
        self.assertIn(f'type="submit">Close Fork-3 #{pid} pid 4101</button>', ra)
        self.assertIn(f'type="submit">Close Fork-3 #{pid} pid 4202</button>', rb)

    def test_m1_folder_named_like_a_label(self):
        a = {**self.g("Fork-3", "Zoe", 4101), "profile_id": "c23f2ced"}
        b = {**self.g("Fork-3", "Zoe", 4202), "profile_id": "0123abcd"}
        c = {**self.g("Fork-3 #c23f2ced", "Zoe", 4303), "profile_id": "99999999"}
        ra, rb, rc = self.rows(self.panel([a, b, c], csrf="tok", control=True))
        self.assertIn('Zoe <span class="muted">Fork-3 #c23f2ced pid 4101</span>', ra)
        self.assertIn('Zoe <span class="muted">Fork-3 #0123abcd</span>', rb)
        self.assertIn('Zoe <span class="muted">Fork-3 #c23f2ced pid 4303</span>', rc)
        self.assertIn('type="submit">Close Fork-3 #c23f2ced pid 4101</button>', ra)
        self.assertIn('type="submit">Close Fork-3 #c23f2ced pid 4303</button>', rc)

    def test_m1_unique_labels_unchanged(self):
        out = self.panel([self.g("Fork-3", "Ann", 4101), self.g("Fork-9", "Max", 4202)], csrf="tok", control=True)
        self.assertNotIn(" pid ", out.replace('name="pid"', ""))

    def test_norm_path_normalises(self):
        self.assertEqual(health.norm_path("/home/a/./x/../Fork-3//"), "/home/a/Fork-3")
        self.assertEqual(health.norm_path("/"), "/")
        self.assertEqual(health.norm_path(""), "")
        self.assertEqual(health.profile_id("/home/a/x/../Fork-3"), health.profile_id("/home/a/Fork-3"))

    def test_profile_id_is_keyed(self):
        import hashlib
        self.assertNotEqual(health.profile_id("/home/a/Fork-3"),
                            hashlib.sha256(b"/home/a/Fork-3").hexdigest()[:8])


class NodeFlagLists(unittest.TestCase):
    # Node options that take a separate value argument (from `node --help`). None may be listed as boolean,
    # or `node --flag VALUE script` would treat VALUE as the script.
    VALUE_TAKING = ("-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file",
                    "--env-file-if-exists", "--title", "--inspect-port", "--debug-port", "--conditions", "-C",
                    "--input-type", "--watch-path", "--stack-size", "--max-old-space-size", "--max-http-header-size",
                    "--unhandled-rejections", "--disable-proto", "--experimental-default-type", "--diagnostic-dir",
                    "--report-dir", "--report-directory", "--report-filename", "--report-signal", "--cpu-prof-dir",
                    "--cpu-prof-name", "--heap-prof-dir", "--heap-prof-name", "--redirect-warnings", "--icu-data-dir",
                    "--openssl-config", "--tls-cipher-list", "--tls-keylog", "--tls-min-v1.0", "--secure-heap",
                    "--secure-heap-min", "--test-reporter", "--test-reporter-destination", "--test-name-pattern",
                    "--test-concurrency", "--test-timeout", "--localstorage-file", "--allow-fs-read", "--allow-fs-write",
                    "--experimental-config-file", "--dns-result-order", "--trace-event-categories",
                    "--trace-event-file-pattern", "--snapshot-blob", "--build-snapshot-config", "--policy-integrity",
                    "--experimental-policy", "--experimental-sea-config", "--run", "-e", "--eval", "-p", "--print",
                    "--experimental-specifier-resolution", "--disable-warning", "--cpu-prof-interval",
                    "--heap-prof-interval", "--heapsnapshot-near-heap-limit", "--heapsnapshot-signal",
                    "--inspect-publish-uid", "--test-shard", "--use-largepages", "--v8-pool-size",
                    "--network-family-autoselection-attempt-timeout")

    def test_t3_no_value_taking_flag_is_boolean(self):
        self.assertEqual(sorted(set(health.NODE_BOOL_FLAGS) & set(self.VALUE_TAKING)), [])
        self.assertFalse(set(health.NODE_BOOL_FLAGS) & set(health.NODE_VALUE_FLAGS + health.NODE_NO_SCRIPT))
        self.assertTrue(all(f.startswith("--") and "=" not in f for f in health.NODE_BOOL_FLAGS))

    def test_t3_value_taking_flags_never_expose_the_script_slot(self):
        for flag in self.VALUE_TAKING:
            self.assertIsNone(health._node_script([flag, "vitest"]), flag)  # the value is never the script
            self.assertFalse(health.stoppable_job(["node", flag, "vitest", "app.js"], {"stop_jobs": [["vitest"]]}), flag)


if __name__ == "__main__":
    unittest.main()
