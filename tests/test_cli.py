import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from botroll.cli import main


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"BOTROLL_NOW": "2026-10-10 15:30", "BOTROLL_DIR": self.dir,
                                           "BOTROLL_HUMAN": "Sam", "NO_COLOR": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_three_part_ask(self):
        self.assertEqual(run("add", "social", "Draft", "the", "thread"), (0, "social/T-1\n", ""))
        self.assertEqual(run("add", "social", "Schedule it", "--state", "todo")[1], "social/T-2\n")
        self.assertEqual(run("add", "social", "Report back", "--state", "todo")[1], "social/T-3\n")
        self.assertEqual(run("set", "social", "T-1", "waiting", "--note", "Approve the copy?")[1], "social/T-1 [waiting-sam]\n")
        self.assertEqual(run("note", "social", "T-2", "slot", "booked")[1], "social/T-2 [todo]\n")
        self.assertEqual(run("done", "social", "T-3", "--note", "sent")[1], "social/T-3 [done]\n")
        code, out, _ = run("list")
        self.assertEqual(out.splitlines(), [
            "social",
            "  social/T-1 [waiting-sam] Draft the thread · asked 2026-10-10 15:30 · updated 2026-10-10 15:30 · note: Approve the copy?",
            "  social/T-2 [todo] Schedule it · asked 2026-10-10 15:30 · updated 2026-10-10 15:30 · note: slot booked",
        ])
        self.assertEqual(len(run("list", "--all", "social")[1].splitlines()), 4)

    def test_list_names_each_bot(self):
        run("add", "assistant", "Book the call")
        run("add", "qa", "Smoke test")
        path = pathlib.Path(self.dir, "assistant.md")
        path.write_text(path.read_text().replace("name: assistant\nrole:", "name: Ann\nrole: Chief of Staff"))
        out = run("list")[1].splitlines()
        self.assertEqual([out[0], out[2], out[3]], ["Ann · Chief of Staff", "", "qa"])
        self.assertTrue(out[1].startswith("  assistant/T-1 [doing] Book the call"))

    def test_dir_flag_works_before_or_after_the_command(self):
        other = tempfile.mkdtemp()
        run("--dir", other, "add", "qa", "a")
        run("add", "qa", "b", "--dir", other)
        self.assertEqual(sorted(os.listdir(other)), [".lock", "qa.md"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_watch_exit_codes(self):
        self.assertEqual(run("watch")[0:2], (0, ""))
        with mock.patch.dict(os.environ, {"BOTROLL_NOW": "2026-10-10 08:00"}):
            run("add", "qa", "old work")
        code, out, _ = run("watch")
        self.assertEqual(code, 1)
        self.assertIn("stale    qa/T-1", out)
        self.assertIn("no update for 7h 30m", out)

    def test_board_outputs(self):
        run("add", "qa", "work", "--pid", "1")
        snap = json.loads(run("board", "--json")[1])
        self.assertEqual(snap["bots"][0]["items"][0]["pids"], [1])
        self.assertIn("1 bot is working", run("board")[1])
        path = os.path.join(self.dir, "board.html")
        self.assertEqual(run("board", "--html", path)[1], path + "\n")
        self.assertIn("<!doctype html>", pathlib.Path(path).read_text())
        self.assertTrue(run("board", "--html", "-")[1].startswith("<!doctype html>"))

    def test_errors_exit_2_with_a_message(self):
        self.assertEqual(run("set", "qa", "T-1", "done"), (2, "", f"botroll: no T-1 in {self.dir}/qa.md\n"))
        self.assertEqual(run("add", "QA!", "x")[0], 2)
        self.assertIn("state must be one of", run("add", "qa", "x", "--state", "nope")[2])

    def test_push_url_alias(self):
        with mock.patch.dict(os.environ, {"BOTROLL_PUSH_URL": "https://push.example",
                                         "BOTROLL_URL": "https://board.example",
                                         "BOTROLL_PUSH_TOKEN": "t" * 40}):
            from botroll.cli import main
            with mock.patch("botroll.serve.push", return_value=(1, 0, 0)) as push, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["push"]), 0)
            self.assertEqual(push.call_args[0][0], "https://push.example")
        with mock.patch.dict(os.environ, {"BOTROLL_URL": "https://board.example",
                                         "BOTROLL_PUSH_TOKEN": "t" * 40}):
            from botroll.cli import main
            with mock.patch("botroll.serve.push", return_value=(1, 0, 0)) as push, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["push"]), 0)
            self.assertEqual(push.call_args[0][0], "https://board.example")
        with mock.patch.dict(os.environ, {"BOTROLL_PUSH_URL": "https://push.example",
                                         "BOTROLL_PUSH_TOKEN": "t" * 40}):
            from botroll.cli import main
            with mock.patch("botroll.serve.push", return_value=(1, 0, 1)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["push"]), 1)


    def test_prune_is_a_dry_run_unless_yes(self):
        def fake(url, token, d, prune=False, removed=None, confirm=False):
            removed.extend(["old"])
            return (1, 0, 0)
        env = {"BOTROLL_PUSH_URL": "https://push.example", "BOTROLL_PUSH_TOKEN": "t" * 40}
        from botroll.cli import main
        for args, confirm, said in [(["push", "--prune"], False, "would prune (dry run; add --yes to delete) 1: old"),
                                    (["push", "--prune", "--yes"], True, "pruned 1: old")]:
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), mock.patch("botroll.serve.push", side_effect=fake) as push, \
                    contextlib.redirect_stdout(out):
                self.assertEqual(main(args), 0)
            self.assertEqual(push.call_args.kwargs["confirm"], confirm)
            self.assertIn(said, out.getvalue())
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["push", "--yes"]), 2)


if __name__ == "__main__":
    unittest.main()
