import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import os
import unittest
from unittest import mock

from botroll import Item, SLUG, about, clean, items, parse, path, title

LINE = "- [doing] T-12 Backorder a domain · asked 2026-10-10 21:05 · updated 2026-10-11 06:40 · note: card form · pid: 4821"


class Format(unittest.TestCase):
    def test_round_trip(self):
        [item] = parse(LINE)
        self.assertEqual((item.state, item.id, item.title), ("doing", "T-12", "Backorder a domain"))
        self.assertEqual(item.get("note"), "card form")
        self.assertEqual(item.pids, [4821])
        self.assertEqual(item.line(), LINE)

    def test_unrecognised_lines_are_kept(self):
        text = "# Builder\n\nSome prose.\n<!-- a comment -->\n- a plain bullet\n" + LINE
        lines = parse(text)
        self.assertEqual(lines[:5], ["# Builder", "", "Some prose.", "<!-- a comment -->", "- a plain bullet"])
        self.assertIsInstance(lines[5], Item)

    def test_hand_edits_are_tolerated(self):
        [item] = parse("  *  [ Doing ]  t-3   Loose spacing · updated 2026-10-10 09:00 · free text here · Owner: QA")
        self.assertEqual((item.state, item.id, item.title), ("doing", "T-3", "Loose spacing"))
        self.assertEqual(item.get("owner"), "QA")
        self.assertIn("free text here", item.line())

    def test_waiting_states_normalise(self):
        for word in ("waiting", "waiting-human", "waiting-saad", "Waiting-Alex"):
            self.assertEqual(parse(f"- [{word}] T-1 Ask")[0].state, "waiting-human")

    def test_human_name_is_written(self):
        item = Item("waiting-human", "T-1", "Ask")
        with mock.patch.dict(os.environ):
            os.environ.pop("BOTROLL_HUMAN", None)
            self.assertTrue(item.line().startswith("- [waiting-human] T-1"))
        with mock.patch.dict(os.environ, {"BOTROLL_HUMAN": "Saad"}):
            self.assertTrue(item.line().startswith("- [waiting-saad] T-1"))

    def test_fields_keep_their_order(self):
        [item] = parse("- [todo] T-1 x · note: n · updated 2026-01-01 10:00 · pid: 1")
        item.fields["note"] = "m"
        self.assertEqual(item.line(), "- [todo] T-1 x · note: m · updated 2026-01-01 10:00 · pid: 1")

    def test_a_repeated_key_is_kept_as_free_text(self):
        [item] = parse("- [todo] T-1 x · note: first · note: second")
        self.assertEqual(item.get("note"), "first")
        self.assertEqual(item.line(), "- [todo] T-1 x · note: first · note: second")

    def test_pids_are_the_leading_numbers(self):
        for field, pids in [("4821", [4821]), ("4821, 4822", [4821, 4822]), ("4821 4822", [4821, 4822]),
                            ("4821 (ffmpeg, 2 hrs)", [4821]), ("none", []), ("", [])]:
            self.assertEqual(Item("doing", "T-1", "x", {"pid": field}).pids, pids, field)

    def test_an_item_without_a_title_is_still_an_item(self):
        [item] = parse("- [doing] T-5")
        self.assertEqual((item.id, item.title), ("T-5", ""))
        self.assertEqual(parse("- [doing] T-50x")[0], "- [doing] T-50x")

    def test_name_and_role_come_from_front_matter(self):
        def of(text):
            return about(parse(text), "assistant")
        self.assertEqual(of("---\nname: Ann\nRole:  Chief of Staff \n---\n- [doing] T-1 x"), ("Ann", "Chief of Staff"))
        self.assertEqual(of("---\nrole: Chief of Staff\n---"), ("assistant", "Chief of Staff"))
        self.assertEqual(of("---\nname:\n---"), ("assistant", ""))
        self.assertEqual(of("# Ann\n---\nname: Ann\n---"), ("assistant", ""))
        self.assertEqual(of(""), ("assistant", ""))
        self.assertEqual((title("Ann", "Chief of Staff"), title("Ann", "")), ("Ann · Chief of Staff", "Ann"))

    def test_clean_keeps_one_line_and_the_separator_free(self):
        self.assertEqual(clean(" two\nlines · and   a dot "), "two lines - and a dot")

    def test_bom_does_not_hide_front_matter(self):
        self.assertEqual(about(parse("\ufeff---\nname: Ann\nrole: Staff\n---\n"), "x"), ("Ann", "Staff"))

    def test_duplicate_ids_are_kept_once(self):
        shown = items(parse("- [doing] T-1 first\n- [doing] T-1 second\n- [todo] T-2 other\n"))
        self.assertEqual([(i.id, i.title) for i in shown], [("T-1", "first"), ("T-2", "other")])

    def test_future_updated_is_just_now(self):
        from datetime import datetime
        item = parse("- [doing] T-1 x · updated 2026-10-10 16:00")[0]
        self.assertEqual(item.age(datetime(2026, 10, 10, 15, 30)), 0)

    def test_slug_length_is_capped(self):
        self.assertTrue(SLUG.fullmatch("a" * 64))
        self.assertFalse(SLUG.fullmatch("a" * 65))
        with self.assertRaisesRegex(ValueError, "64"):
            path("/tmp", "a" * 65)


if __name__ == "__main__":
    unittest.main()
