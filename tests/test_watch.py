import unittest
from datetime import datetime

from botroll import parse
from botroll.watch import Proc, check, duration, seconds

NOW = datetime(2026, 10, 10, 15, 30)


def entries(text, bot="qa"):
    return [(bot, i) for i in parse(text)]


def kinds(flags):
    return sorted(((f.kind, f.bot, f.id, f.pid) for f in flags), key=str)


class Seconds(unittest.TestCase):
    def test_linux_and_macos_formats(self):
        self.assertEqual(seconds("05:07"), 307)
        self.assertEqual(seconds("01:02:03"), 3723)
        self.assertEqual(seconds("2-01:00:00"), 2 * 86400 + 3600)
        self.assertEqual(seconds("0:00.03"), 0)
        self.assertEqual(seconds("12:34.56"), 754)

    def test_duration(self):
        self.assertEqual([duration(h) for h in (0.4, 2.25, 8.1667, 14.33, 27, 50)], ["24m", "2h 15m", "8h 10m", "14h", "27h", "2d"])


class Stale(unittest.TestCase):
    def test_limits_per_state(self):
        text = "\n".join([
            "- [doing] T-1 fresh · updated 2026-10-10 10:00",
            "- [doing] T-2 quiet · updated 2026-10-10 09:00",
            "- [blocked] T-3 blocked · updated 2026-10-10 09:00",
            "- [waiting-human] T-4 ask · updated 2026-10-10 04:00",
            "- [waiting-human] T-5 old ask · updated 2026-10-10 03:00",
            "- [todo] T-6 queued · updated 2026-10-09 16:00",
            "- [todo] T-7 never started · updated 2026-10-09 15:00",
            "- [done] T-8 done long ago · updated 2026-01-01 00:00",
            "- [doing] T-9 no times",
        ])
        flags = check(entries(text), {}, NOW)
        self.assertEqual([f.id for f in flags], ["T-2", "T-3", "T-5", "T-7", "T-9"])
        self.assertEqual(flags[0].text, "no update for 6h 30m")
        self.assertEqual(flags[3].text, "never started, queued for 24h")

    def test_unknown_state(self):
        [flag] = check(entries("- [doign] T-1 typo · updated 2026-10-10 15:00"), {}, NOW)
        self.assertEqual((flag.kind, flag.text), ("state", "unknown state [doign] on T-1 typo"))


class Processes(unittest.TestCase):
    def test_live_pid_on_closed_or_stale_items(self):
        text = "\n".join([
            "- [done] T-1 recorded · updated 2026-10-10 10:00 · pid: 100",
            "- [dropped] T-2 dropped · updated 2026-10-10 10:00 · pid: 101",
            "- [done] T-3 dead pid · updated 2026-10-10 10:00 · pid: 999",
            "- [doing] T-4 live and fresh · updated 2026-10-10 15:00 · pid: 102",
            "- [doing] T-5 live but stale · updated 2026-10-10 01:00 · pid: 103",
        ])
        procs = {p: Proc(p, 1, 60, 0, "sleep 1") for p in (100, 101, 102, 103)}
        flags = [f for f in check(entries(text), procs, NOW) if f.kind == "pid"]
        self.assertEqual([(f.id, f.pid) for f in flags], [("T-1", 100), ("T-2", 101), ("T-5", 103)])
        self.assertEqual(flags[0].text, "pid 100 (sleep) still running, but T-1 is done")

    def test_cpu_hog_is_attributed_to_its_item_through_children(self):
        text = "- [doing] T-1 build · updated 2026-10-10 15:00 · pid: 200"
        procs = {
            200: Proc(200, 1, 7200, 10, "make"),
            201: Proc(201, 200, 7200, 7000, "/usr/bin/cc big.c"),
            300: Proc(300, 1, 7200, 7100, "python3 spin.py"),
            301: Proc(301, 1, 1800, 1800, "python3 short.py"),
            302: Proc(302, 1, 7200, 3600, "python3 half.py"),
        }
        flags = check(entries(text), procs, NOW)
        self.assertEqual(kinds(flags), [("cpu", "qa", "T-1", 201), ("cpu", None, None, 300)])
        self.assertEqual(flags[0].text, "pid 201 (cc) at 97% CPU for 2h")

    def test_untracked_capture(self):
        text = "\n".join([
            "- [doing] T-1 recording · updated 2026-10-10 15:00 · pid: 400",
            "- [done] T-2 recorded · updated 2026-10-10 10:00 · pid: 500",
        ])
        procs = {
            400: Proc(400, 1, 4000, 0, "sh -c record"),
            401: Proc(401, 400, 4000, 0, "ffmpeg -f x11grab -i :0"),  # child of an open item
            500: Proc(500, 1, 4000, 0, "ffmpeg -i :0 out.mp4"),  # recorded on a closed item: a pid flag
            501: Proc(501, 500, 4000, 0, "ffmpeg child"),  # child of a closed item: untracked
            600: Proc(600, 1, 1900, 0, "/opt/bin/ffmpeg -f avfoundation"),
            601: Proc(601, 1, 1700, 0, "ffmpeg too young"),
            602: Proc(602, 1, 9000, 0, "ffmpegthumbnailer"),
        }
        flags = check(entries(text), procs, NOW)
        self.assertEqual(kinds(flags), [("capture", None, None, 501), ("capture", None, None, 600), ("pid", "qa", "T-2", 500)])
        self.assertIn("capturing for 32m, not on any open item", [f.text for f in flags if f.pid == 600][0])


if __name__ == "__main__":
    unittest.main()
