import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import multiprocessing
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

import botroll
from botroll import add, items, read, update


def _add_many(directory, bot, n):
    for i in range(n):
        add(directory, bot, f"item {os.getpid()} {i}")


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"BOTROLL_NOW": "2026-10-10 15:30"})
        env.start()
        self.addCleanup(env.stop)

    def test_add_creates_the_file_and_numbers_items(self):
        self.assertEqual(add(self.dir, "qa", "first"), "T-1")
        self.assertEqual(add(self.dir, "qa", "second", state="todo", note="later"), "T-2")
        lines = read(self.dir, "qa")
        self.assertEqual(lines[:4], ["---", "name: qa", "role:", "---"])
        first, second = items(lines)
        self.assertEqual((first.state, first.get("asked"), first.get("updated")), ("doing", "2026-10-10 15:30", "2026-10-10 15:30"))
        self.assertEqual((second.state, second.get("note")), ("todo", "later"))

    def test_update_changes_state_note_pid_and_bumps_updated(self):
        add(self.dir, "video", "record")
        with mock.patch.dict(os.environ, {"BOTROLL_NOW": "2026-10-10 16:00"}):
            item = update(self.dir, "video", "t-1", "doing", note="recording", pid="123")
        self.assertEqual((item.get("pid"), item.get("note"), item.get("updated")), ("123", "recording", "2026-10-10 16:00"))
        self.assertEqual(item.get("asked"), "2026-10-10 15:30")

    def test_hand_written_lines_survive_a_write(self):
        path = os.path.join(self.dir, "builder.md")
        with open(path, "w") as f:
            f.write("# Builder\n\nNotes stay.\n- [todo] T-4 Thing · asked 2026-10-10 09:00\n")
        update(self.dir, "builder", "T-4", "doing")
        text = pathlib.Path(path).read_text()
        self.assertTrue(text.startswith("# Builder\n\nNotes stay.\n- [doing] T-4 Thing · asked 2026-10-10 09:00 · updated"))

    def test_a_write_rewrites_only_the_line_it_changes(self):
        path = os.path.join(self.dir, "social.md")
        lines = ["# Social", "* [Waiting-Sam]  T-1 Hand written ·  note: as typed", "  - [todo] T-2 Nested · pid: 7",
                 "- [todo] T-3 Change me · asked 2026-10-10 09:00"]
        pathlib.Path(path).write_text("\n".join(lines) + "\n")
        update(self.dir, "social", "T-3", "doing")
        self.assertEqual(pathlib.Path(path).read_text().splitlines(),
                         lines[:3] + ["- [doing] T-3 Change me · asked 2026-10-10 09:00 · updated 2026-10-10 15:30"])
        self.assertEqual(add(self.dir, "social", "next"), "T-4")

    def test_errors(self):
        for slug in ("../etc", "qa\n", "QA", ""):
            with self.assertRaises(ValueError):
                add(self.dir, slug, "nope")
        add(self.dir, "qa", "x")
        with self.assertRaises(ValueError):
            update(self.dir, "qa", "T-1", "finished")
        with self.assertRaises(LookupError):
            update(self.dir, "qa", "T-9", "done")

    def test_failed_edit_leaves_the_file_alone(self):
        add(self.dir, "qa", "x")
        before = pathlib.Path(self.dir, "qa.md").read_text()
        with self.assertRaises(LookupError):
            update(self.dir, "qa", "T-9", "done")
        self.assertEqual(pathlib.Path(self.dir, "qa.md").read_text(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), [".lock", "qa.md"])

    def test_file_mode_is_kept(self):
        add(self.dir, "qa", "x")
        os.chmod(os.path.join(self.dir, "qa.md"), 0o664)
        update(self.dir, "qa", "T-1", "done")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "qa.md")).st_mode), 0o664)

    def test_concurrent_writers_lose_nothing(self):
        procs = [multiprocessing.Process(target=_add_many, args=(self.dir, "shared", 25)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        ids = [i.id for i in items(read(self.dir, "shared"))]
        self.assertEqual(len(ids), 100)
        self.assertEqual(sorted(ids, key=lambda i: int(i[2:])), [f"T-{n}" for n in range(1, 101)])

    def test_bots_lists_only_task_files(self):
        for name in ("qa.md", "README.md", "notes.txt", ".hidden.md"):
            open(os.path.join(self.dir, name), "w").close()
        self.assertEqual(botroll.bots(self.dir), ["qa"])
        self.assertEqual(botroll.bots(os.path.join(self.dir, "missing")), [])


if __name__ == "__main__":
    unittest.main()
