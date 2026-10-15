"""Browser → bot names without setup: names_file, per-display files, then the profile's own label."""
import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from botroll import TIME, health, local
from botroll.board import health_panel


class Names(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.displays = self.tmp / "displays"
        self.displays.mkdir()
        self.cfg = {**health.load_config(None), "display_names_dir": str(self.displays)}
        p = mock.patch.object(health, "_display_of", return_value=None)  # no real /proc lookups
        self.display_of = p.start()
        self.addCleanup(p.stop)

    def test_names_file(self):
        f = self.tmp / "names.tsv"
        f.write_text("# profile<TAB>name\n\nFork-5\tMax\n/srv/p/Main\tAnn\nbroken line\nFork-6\t\nX\t" + "y" * 99 + "\n")
        self.cfg["names_file"] = str(f)
        self.assertEqual(health.names_from_file(str(f)), {"Fork-5": "Max", "/srv/p/Main": "Ann"})
        self.assertEqual(health.resolve_profile("/home/u/chrome/Fork-5", self.cfg), "Max")
        self.assertEqual(health.resolve_profile("/srv/p/Main", self.cfg), "Ann")
        self.assertIsNone(health.resolve_profile("/other/Main", self.cfg))  # full-path keys are exact
        self.assertIsNone(health.resolve_profile("/home/u/chrome/Fork-50", self.cfg))
        self.cfg["names_file"] = str(self.tmp / "missing.tsv")
        self.assertIsNone(health.resolve_profile("/home/u/chrome/Fork-5", self.cfg))

    def test_display_file_from_profile_number(self):
        (self.displays / "3").write_text("Kai\nignored\n")
        self.assertEqual(health.resolve_profile("/home/u/chrome/Fork-3", self.cfg), "Kai")
        self.assertIsNone(health.resolve_profile("/home/u/chrome/Fork-4", self.cfg))
        self.assertIsNone(health.resolve_profile("/home/u/chrome/Default", self.cfg))

    def test_display_file_from_browser_display_beats_folder(self):
        (self.displays / "3").write_text("Kai")
        (self.displays / "7").write_text("Vera")
        self.display_of.side_effect = lambda pid: "7" if pid == 99 else None
        self.assertEqual(health.resolve_profile("/home/u/chrome/Fork-3", self.cfg, pid=99), "Vera")
        self.assertEqual(health.resolve_profile("/home/u/any-name", self.cfg, pid=99), "Vera")

    def test_display_file_rejects_junk(self):
        (self.displays / "3").write_text("bad\x07na\u202eme\n")
        self.assertEqual(health.resolve_profile("/p/Fork-3", self.cfg), "badname")  # stripped, not shown
        (self.displays / "3").write_text("\u200b\ufeff\x07\n")
        self.assertIsNone(health.resolve_profile("/p/Fork-3", self.cfg))
        (self.displays / "3").write_text("")
        self.assertIsNone(health.resolve_profile("/p/Fork-3", self.cfg))
        self.cfg["display_names_dir"] = None
        (self.displays / "3").write_text("Kai")
        self.assertIsNone(health.resolve_profile("/p/Fork-3", self.cfg))

    def test_precedence(self):
        (self.displays / "3").write_text("FromDisplay")
        f = self.tmp / "names.tsv"
        f.write_text("Fork-3\tFromFile\n")
        self.cfg["names_file"] = str(f)
        self.cfg["browsers"] = {"Fork-3": "FromMap"}
        self.assertEqual(health.resolve_profile("/p/Fork-3", self.cfg), "FromMap")
        self.cfg["browsers"] = {}
        self.assertEqual(health.resolve_profile("/p/Fork-3", self.cfg), "FromFile")
        self.cfg["names_file"] = None
        self.assertEqual(health.resolve_profile("/p/Fork-3", self.cfg), "FromDisplay")

    def test_display_of_parses_environ(self):
        self.display_of.side_effect = None
        mock.patch.stopall()
        env = b"HOME=/h\0DISPLAY=:12.0\0X=1\0"
        with mock.patch("builtins.open", mock.mock_open(read_data=env)):
            self.assertEqual(health._display_of(5), "12")
        with mock.patch("builtins.open", mock.mock_open(read_data=b"DISPLAY=host:abc\0")):
            self.assertIsNone(health._display_of(5))
        self.assertIsNone(health._display_of(None))

    def test_profile_label_and_group(self):
        ud = self.tmp / "chrome-a"
        ud.mkdir()
        (ud / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Default": {"name": "Work"}}}}))
        self.assertEqual(health.profile_label(str(ud)), "Work")
        rows = [{"pid": 10, "ppid": 1, "rss_kb": 1024, "elapsed_s": 5, "cpu_s": 0,
                 "args": f"/usr/bin/chromium --user-data-dir={ud}"},
                {"pid": 11, "ppid": 1, "rss_kb": 1024, "elapsed_s": 5, "cpu_s": 0,
                 "args": f"/usr/bin/chromium --user-data-dir={self.tmp}/Fork-2"}]
        (self.displays / "2").write_text("Sasha")
        got = {g["profile"]: (g["bot"], g["label"]) for g in health.group_browsers(rows, {}, self.cfg, at=100)}
        self.assertEqual(got, {"chrome-a": (None, "Work"), "Fork-2": ("Sasha", None)})
        for placeholder in ("Your Chrome", "Person 1"):
            (ud / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Default": {"name": placeholder}}}}))
            self.assertIsNone(health.profile_label(str(ud)))
        (ud / "Local State").write_text("{not json")
        self.assertIsNone(health.profile_label(str(ud)))
        self.assertIsNone(health.profile_label(str(self.tmp / "nope")))

    def test_config_validation(self):
        f = self.tmp / "c.json"
        f.write_text('{"names_file": 3}')
        with self.assertRaises(ValueError):
            health.load_config(str(f))
        f.write_text('{"names_file": "", "display_names_dir": ""}')
        c = health.load_config(str(f))
        self.assertEqual((c["names_file"], c["display_names_dir"]), (None, None))
        self.assertEqual(health.load_config(None)["display_names_dir"], "~/.botroll/displays")

    def test_board_shows_label(self):
        import time
        data = self.tmp / "data"
        data.mkdir()
        health.append(str(data), {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
                                  "mem": {"total_mb": 1000, "available_mb": 500},
                                  "browsers": [{"profile": "chrome-a", "bot": None, "label": "Work", "procs": 1,
                                                "rss_mb": 1, "cpu": 0.0, "port": None},
                                               {"profile": "chrome-b", "bot": None, "procs": 1, "rss_mb": 1,
                                                "cpu": 0.0, "port": None}],
                                  "helpers": [], "jobs": [], "alerts": []})
        out = health_panel({"generated": local().strftime(TIME), "health": health.load(str(data))}, env={})
        self.assertIn("Work <span", out)
        self.assertIn("profile name", out)
        self.assertIn("chrome-b <span class=\"muted\">unmapped", out)


if __name__ == "__main__":
    unittest.main()
