"""Kai's round-2 lows: safe file reads, invisible characters, full-path closability, node launcher, escaping."""
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from botroll import TIME, health, local
from botroll.board import _ctl, health_panel


class SafeReads(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.displays = self.tmp / "d"
        self.displays.mkdir()
        self.cfg = {**health.load_config(None), "display_names_dir": str(self.displays)}
        p = mock.patch.object(health, "_display_of", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def finishes(self, fn):
        """Run fn in a thread; fail if it blocks (an open() on a FIFO without O_NONBLOCK would)."""
        out = []
        t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "read blocked")
        return out[0]

    def test_n1_fifo_refused_without_blocking(self):
        fifo = self.displays / "3"
        os.mkfifo(fifo)
        self.assertIsNone(self.finishes(lambda: health.resolve_profile("/p/Fork-3", self.cfg)))
        names = self.tmp / "names.fifo"
        os.mkfifo(names)
        self.assertEqual(self.finishes(lambda: health.names_from_file(str(names))), {})

    def test_n1_device_and_directory_refused(self):
        self.assertIsNone(health._read_small("/dev/zero"))
        self.assertIsNone(health._read_small("/dev/null"))
        (self.displays / "4").mkdir()
        self.assertIsNone(health.resolve_profile("/p/Fork-4", self.cfg))

    def test_n2_symlinks_refused(self):
        real = self.tmp / "real.txt"
        real.write_text("Fork-3\tAnn\n")
        (self.tmp / "names.tsv").symlink_to(real)
        self.assertEqual(health.names_from_file(str(self.tmp / "names.tsv")), {})
        self.assertEqual(health.names_from_file(str(real)), {"Fork-3": "Ann"})
        target = self.tmp / "name"
        target.write_text("Kai")
        (self.displays / "3").symlink_to(target)
        self.assertIsNone(health.resolve_profile("/p/Fork-3", self.cfg))

    def test_regular_file_still_read(self):
        (self.displays / "3").write_text("Kai\n")
        self.assertEqual(health.resolve_profile("/p/Fork-3", self.cfg), "Kai")


class Invisible(unittest.TestCase):
    NASTY = "A\u202enn\u200b\u200d\ufeff\u2066x\u2069\x07\x1b[31m\u061c"

    def test_n3_collector_strips(self):
        self.assertEqual(health._clean_name(self.NASTY), "Annx[31m")
        self.assertEqual(health.plain_text("ok \u200e\u200fname"), "ok name")
        self.assertIsNone(health._clean_name("\u200b\u202e\ufeff"))
        f = pathlib.Path(os.path.realpath(tempfile.mkdtemp())) / "n.tsv"
        self.addCleanup(shutil.rmtree, f.parent)
        f.write_text("\ufeffFork-1\tB\u202eob\n")
        self.assertEqual(list(health.names_from_file(str(f)).values()), ["Bob"])

    def test_n3_board_strips(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        health.append(data, {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
                             "mem": {"total_mb": 1000, "available_mb": 500},
                             "browsers": [{"profile": "p\u202eA", "bot": "Ev\u202eil\u200b", "procs": 1, "rss_mb": 1,
                                           "cpu": 0.0, "port": None},
                                          {"profile": "q\u2066B", "bot": None, "label": "W\ufeffork", "procs": 1,
                                           "rss_mb": 1, "cpu": 0.0, "port": None}],
                             "helpers": [], "jobs": [], "alerts": []})
        out = health_panel({"generated": local().strftime(TIME), "health": health.load(data)}, env={})
        self.assertIn("Evil", out)
        self.assertIn("Work", out)
        self.assertIn("qB", out)
        for ch in ("\u202e", "\u200b", "\ufeff", "\u2066"):
            self.assertNotIn(ch, out)


class FullPath(unittest.TestCase):
    def test_n4_closable_needs_full_path(self):
        cfg = {"browsers": {"Fork-3": "a", "/srv/p/Main/": "b", "relative/Other": "c"}}
        self.assertFalse(health.configured_browser("/home/u/Fork-3", cfg))
        self.assertTrue(health.configured_browser("/srv/p/Main", cfg))
        self.assertFalse(health.configured_browser("/elsewhere/Main", cfg))
        self.assertFalse(health.configured_browser("relative/Other", cfg))
        self.assertFalse(health.configured_browser("", cfg))


class NodeLauncher(unittest.TestCase):
    cfg = {"stop_jobs": [["ffmpeg"], ["vitest"], ["npm", "run"]]}

    def ok(self, argv):
        return health.stoppable_job(argv, self.cfg)

    def test_n5_preloads_are_not_the_script(self):
        self.assertFalse(self.ok(["node", "-r", "ffmpeg", "server.js"]))
        self.assertFalse(self.ok(["node", "--require", "vitest", "app.js"]))
        self.assertFalse(self.ok(["node", "--import", "vitest", "app.js"]))
        self.assertTrue(self.ok(["node", "-r", "ts-node/register", "/x/.bin/vitest", "run"]))
        self.assertTrue(self.ok(["node", "--require=dotenv/config", "/x/.bin/vitest"]))

    def test_n5_no_script_means_no_job(self):
        self.assertFalse(self.ok(["node", "-e", "vitest"]))
        self.assertFalse(self.ok(["node", "--eval", "require('vitest')"]))
        self.assertFalse(self.ok(["node", "-p", "ffmpeg"]))
        self.assertFalse(self.ok(["node", "-", "vitest"]))
        self.assertFalse(self.ok(["node", "--max-old-space-size=4096"]))

    def test_n5_plain_script(self):
        self.assertTrue(self.ok(["node", "--max-old-space-size=4096", "/x/npm-cli.js", "run", "b"]))
        self.assertTrue(self.ok(["node", "--", "/x/vitest.mjs"]))
        self.assertFalse(self.ok(["node", "app.js", "vitest"]))


class Escaping(unittest.TestCase):
    EVIL = '"><script>alert(1)</script>'

    def test_t1_name_and_profile_escaped(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        health.append(data, {"v": 1, "ts": int(time.time()), "host": "b<i>x", "cores": 2, "load": [0.1, 0.1, 0.1],
                             "mem": {"total_mb": 1000, "available_mb": 500},
                             "browsers": [{"profile": "p" + self.EVIL[:40], "bot": None, "label": "L" + self.EVIL[:40],
                                           "procs": 1, "rss_mb": 1, "cpu": 0.0, "port": None, "main_pid": 50,
                                           "started_at": 1, "closable": True},
                                          {"profile": "q", "bot": "B" + self.EVIL[:40], "procs": 1, "rss_mb": 1,
                                           "cpu": 0.0, "port": None, "main_pid": 51, "started_at": 1, "closable": True}],
                             "helpers": [], "jobs": [], "alerts": []})
        out = health_panel({"generated": local().strftime(TIME), "health": health.load(data)}, env={},
                           csrf="tok", control=True)
        self.assertNotIn("<script>", out)
        self.assertNotIn("<i>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&quot;&gt;", out)

    def test_t1_form_fields_escaped(self):
        out = _ctl("close-browser", {"profile": self.EVIL, "pid": 5}, 'c"sr<f', 'Close "x"')
        self.assertNotIn("<script>", out)
        self.assertIn('value="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"', out)
        self.assertIn('value="c&quot;sr&lt;f"', out)
        self.assertIn("Close &quot;x&quot;", out)
        self.assertEqual(out.count("<input"), 4)  # csrf, kind, profile, pid: no injected fields


if __name__ == "__main__":
    unittest.main()
