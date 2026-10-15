import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import json
import os
import pathlib
import re
import tempfile
import unittest
from datetime import datetime

from botroll.board import cards, needs_you, page, snapshot, text
from botroll.watch import Proc

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples", "tasks")
NOW = datetime(2026, 10, 10, 15, 30)


class Board(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot(EXAMPLES, NOW, {})

    def test_summary(self):
        s = self.snap["summary"]
        self.assertEqual({k: s[k] for k in ("needs_you", "working", "quiet", "stuck", "done_today")},
                         {"needs_you": 4, "working": 5, "quiet": 1, "stuck": 3, "done_today": 7})
        self.assertEqual(" ".join(s["sentence"]), "4 things need you. 5 bots are working (Max has gone quiet), "
                                                  "3 items are stuck, and 7 got done in the last 24h.")

    def test_sentence_edge_cases(self):
        empty = snapshot(tempfile.mkdtemp(), NOW, {})
        self.assertEqual(" ".join(empty["summary"]["sentence"]),
                         "Nothing needs you. No bots are working, nothing is stuck, and nothing got done in the last 24h.")

    def test_quiet_bots_are_named(self):
        d = tempfile.mkdtemp()
        for slug, name in (("a", "Ann"), ("b", "Bo"), ("c", "")):
            with open(os.path.join(d, f"{slug}.md"), "w") as f:
                f.write(f"---\nname: {name}\n---\n- [doing] T-1 x · updated 2026-10-10 01:00\n")
        self.assertIn("3 bots are working (Ann, Bo and c have gone quiet)", snapshot(d, NOW, {})["summary"]["sentence"][1])

    def test_only_open_and_recent_closed_items_are_shown(self):
        shown = {f"{b['bot']}/{i['id']}" for b in self.snap["bots"] for i in b["items"]}
        self.assertEqual(len(shown), 21)
        self.assertIn("builder/T-37", shown)  # done yesterday evening, still in last 24h
        self.assertIn("builder/T-39", shown)  # dropped today

    def test_needs_you_is_oldest_first(self):
        self.assertEqual([f"{b['bot']}/{i['id']}" for b, i in needs_you(self.snap)],
                         ["social/T-31", "researcher/T-15", "assistant/T-8", "assistant/T-7"])

    def test_cards_put_attention_first_and_say_what_each_bot_is_doing(self):
        cs = {c["name"]: c for c in cards(self.snap)}
        self.assertEqual([c["title"] for c in cards(self.snap)][:3], ["Ann · Chief of Staff", "Sasha · Social", "Ravi · Research"])
        self.assertEqual(cs["Max"]["status"], ("quiet", "Quiet 8h 10m"))
        self.assertEqual(cs["Max"]["now"], ("Migrate settings page to the new form library", "T-40 · no update for 8h 10m"))
        self.assertEqual(cs["Sasha"]["status"], ("you", "Waiting on you"))
        self.assertEqual(cs["Kai"]["now"][1], "T-22 · 40m · pid 48213")
        self.assertIn(("quiet", "Stale", "Smoke-test checkout on Safari", "queued 27h, never started"), cs["Kai"]["rows"])
        self.assertIn(("stuck", "Blocked", "Deploy preview build for review", "needs qa/T-22 (Kai's test fixtures)"), cs["Max"]["rows"])
        self.assertEqual([k for k, _, _ in cs["Max"]["done"]], ["done", "drop", "done"])

    def test_process_flags_land_on_cards_or_no_owner(self):
        procs = {50112: Proc(50112, 1, 20000, 5, "ffmpeg -f x11grab"), 777: Proc(777, 1, 3000, 0, "ffmpeg -i x")}
        snap = snapshot(EXAMPLES, NOW, procs)
        video = next(c for c in cards(snap) if c["name"] == "Vera")
        self.assertIn(("stuck", "PID", "pid 50112 (ffmpeg) still running, but T-2 is done", ""), video["rows"])
        html = page(snap)
        self.assertIn("No owner", html)
        self.assertIn("pid 777 (ffmpeg) capturing for 50m, not on any open item", html)

    def test_a_just_updated_ask_sorts_newest(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "x.md"), "w") as f:
            f.write("- [waiting-human] T-1 now · updated 2026-10-10 15:30\n"
                    "- [waiting-human] T-2 earlier · updated 2026-10-10 15:00\n"
                    "- [waiting-human] T-3 no time\n")
        self.assertEqual([i["id"] for _, i in needs_you(snapshot(d, NOW, {}))], ["T-3", "T-2", "T-1"])

    def test_html_is_self_contained_and_escaped(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "x.md"), "w") as f:
            f.write("---\nname: <b>X</b>\nrole: <i>Y</i>\n---\n- [waiting-human] T-1 <script>alert(1)</script> · updated 2026-10-10 15:00\n")
        html = page(snapshot(d, NOW, {}))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn('&lt;b&gt;X&lt;/b&gt; <span class="role">· &lt;i&gt;Y&lt;/i&gt;</span>', html)
        self.assertEqual(re.findall(r"(?:src|href)=", html), [])
        self.assertNotIn("http", html.replace('http-equiv="refresh"', ""))

    def test_html_has_the_design(self):
        html = page(self.snap)
        for needle in ('<h1 class="summary">4 things need you.</h1><p class="lede">5 bots are working', 'class="strip"',
                       '<span class="who">Sasha · T-31</span><span class="pill you"><span class="sr">waiting </span>14h</span>',
                       '<span class="mark hue1" aria-hidden="true">A</span><span class="name">Ann <span class="role">· Chief of Staff</span>',
                       'class="pill quiet">Quiet 8h 10m', '<details class="done" open>', "<title>(4) BotRoll</title>"):
            self.assertIn(needle, html)
        self.assertLess(html.index('class="strip"'), html.index('class="grid"'))

    def test_avatar_colours_follow_the_name(self):
        html = page(self.snap)
        hue = lambda name: re.search(rf'class="mark (hue\d)" aria-hidden="true">{name[0]}</span><span class="name">{name}', html)[1]
        self.assertEqual(hue("Ann"), hue("Kai"))  # same hash bucket, so the same colour every time
        self.assertNotEqual(hue("Ann"), hue("Max"))

    def test_static_board_follows_the_device_theme(self):
        html = page(self.snap)
        self.assertIn('<html lang="en">', html)
        self.assertIn('<meta name="color-scheme" content="light dark">', html)
        self.assertNotIn('action="/theme"', html)
        dark = page(self.snap, live=True, csrf="x", theme="dark")
        self.assertIn('<html lang="en" data-theme="dark">', dark)
        self.assertIn('<meta name="color-scheme" content="dark">', dark)
        self.assertIn('value="dark" aria-pressed="true"', dark)
        self.assertIn('value="system" aria-pressed="false"', dark)

    def test_future_updated_reads_as_just_now(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "x.md"), "w") as f:
            f.write("---\nname: Nia\n---\n- [doing] T-1 Hello · updated 2026-10-10 16:00\n"
                    "- [waiting-human] T-1 also · updated 2026-10-10 15:00\n")
        snap = snapshot(d, NOW, {})
        self.assertEqual(snap["bots"][0]["items"][0]["age_minutes"], 0)
        self.assertEqual(len(snap["bots"][0]["items"]), 1)  # duplicate id dropped
        html = page(snap)
        self.assertIn("updated just now", html)
        self.assertNotIn("-3m", html)

    def test_terminal_and_json(self):
        out = text(self.snap, color=False)
        self.assertTrue(out.splitlines()[2].startswith("4 things need you."))
        self.assertIn("  14h      Sasha · T-31  Approve the copy", out)
        self.assertIn("Max · Engineering  Quiet 8h 10m", out)
        self.assertNotIn("\033[", out)
        self.assertIn("\033[", text(self.snap, color=True))
        self.assertEqual(json.loads(json.dumps(self.snap))["summary"]["needs_you"], 4)

    def test_action_buttons_stay_off_until_a_bot_has_an_endpoint(self):
        html = page(self.snap)
        self.assertNotIn('action="/action"', html)
        self.assertNotIn(">Approve</button>", html)
        live = page(self.snap, live=True, csrf="tok")
        self.assertNotIn('action="/action"', live)
        on = page(self.snap, live=True, csrf="tok", endpoints={"social": True})
        self.assertIn(">Approve</button>", on)
        self.assertIn(">Decline</button>", on)
        self.assertIn("Reply…", on)
        self.assertIn(">Nudge</button>", on)
        self.assertIn('name="bot" value="social"', on)
        self.assertIn('name="item" value="T-31"', on)
        self.assertNotIn('name="bot" value="assistant"', on)
        self.assertNotIn('name="bot" value="researcher"', on)


def _luminance(colour):
    rgb = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)] if len(colour) == 7 else \
          [int(c * 2, 16) / 255 for c in colour[1:]]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Contrast(unittest.TestCase):
    """WCAG AA, 4.5:1, for every text colour on every surface it's used on, in both themes."""

    def test_text_meets_aa_in_both_themes(self):
        css = (pathlib.Path(__file__).parent.parent / "botroll" / "board.css").read_text()
        tokens = dict(re.findall(r"--([\w-]+): light-dark\((#\w+, #\w+)\)", css))
        pairs = [("fg", s) for s in ("bg", "panel", "raised", "knob")] + [("muted", s) for s in ("bg", "panel", "raised")]
        pairs += [("bg", "fg")]  # the primary button
        pairs += [(k, f"{k}-soft") for k in ("you", "doing", "stuck")]
        pairs += [(k, s) for k in ("you", "doing", "stuck") for s in ("bg", "panel")]
        for theme in (0, 1):
            for text_, surface in pairs:
                a, b = (tokens[t].split(", ")[theme] for t in (text_, surface))
                with self.subTest(theme=("light", "dark")[theme], text=text_, on=surface):
                    self.assertGreaterEqual(contrast(a, b), 4.5)


if __name__ == "__main__":
    unittest.main()


class RecentWindow(unittest.TestCase):
    def test_recent_not_calendar_midnight(self):
        """Just after midnight local, work from yesterday evening still counts as recent."""
        import pathlib
        import shutil
        import tempfile
        from datetime import datetime

        from botroll.board import snapshot
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d)
        pathlib.Path(d, "bot.md").write_text(
            "---\nname: Bot\nrole: X\n---\n"
            "- [done] T-1 Shipped yesterday evening · asked 2026-10-14 18:00 · updated 2026-10-14 22:30\n"
            "- [done] T-2 Older · asked 2026-10-13 10:00 · updated 2026-10-13 10:00\n"
            "- [doing] T-3 Active · asked 2026-10-14 20:00 · updated 2026-10-15 01:00\n")
        # 02:00 on the 15th: calendar today has no dones; rolling 24h includes T-1
        at = datetime(2026, 10, 15, 2, 0)
        s = snapshot(d, at, {})["summary"]
        self.assertEqual(s["done_recent"], 1)
        self.assertIn("1 got done in the last 24h", s["sentence"][1])
        self.assertEqual(s["working"], 1)
        self.assertIn("1 bot is working", s["sentence"][1])
        # T-1 shown, T-2 not
        items = snapshot(d, at, {})["bots"][0]["items"]
        self.assertEqual({i["id"] for i in items}, {"T-1", "T-3"})


class ParseTime(unittest.TestCase):
    def test_variants(self):
        from datetime import datetime

        from botroll import parse_time
        self.assertEqual(parse_time("2026-10-14 22:30"), datetime(2026, 10, 14, 22, 30))
        self.assertEqual(parse_time("2026-10-14 22:30:45"), datetime(2026, 10, 14, 22, 30))
        self.assertEqual(parse_time("2026-10-14T22:30:45Z"), datetime(2026, 10, 14, 22, 30))
        self.assertEqual(parse_time("2026-10-14"), datetime(2026, 10, 14, 0, 0))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time("not a date"))
