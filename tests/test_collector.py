import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import contextlib
import http.client
import http.server
import io
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from botroll import health

TOKEN = "t" * 40
STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d "
SNAP = {"ts": 1, "host": "box"}


class Clock:
    """A fake wall and monotonic clock. sleep() moves both; suspend() moves only the wall clock,
    the way a paused machine looks to a process when it wakes up."""

    def __init__(self):
        self.wall, self.mono, self.naps, self.jumps = 1_000_000.0, 100.0, [], {}

    def sleep(self, seconds):
        self.naps.append(seconds)
        self.wall += seconds + self.jumps.pop(len(self.naps), 0)
        self.mono += seconds


def collect(clock, *, iterations=3, **kw):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        health.collect({"control": False}, kw.pop("directory", "/nonexistent"), kw.pop("url", "https://board.example"),
                       TOKEN, kw.pop("every", 60), iterations=iterations,
                       wall=lambda: clock.wall, mono=lambda: clock.mono, sleep=clock.sleep, **kw)
    return err.getvalue()


class Survives(unittest.TestCase):
    """One bad iteration is logged; the loop goes on."""

    def setUp(self):
        self.clock = Clock()
        self.fetch = mock.patch.object(health, "fetch_commands", return_value=[]).start()
        self.push = mock.patch.object(health, "push").start()
        self.addCleanup(mock.patch.stopall)

    def test_sample_errors_do_not_stop_the_loop(self):
        timeout = subprocess.TimeoutExpired(["ps"], 20)
        with mock.patch.object(health, "sample", side_effect=[timeout, RuntimeError("boom"), SNAP]):
            err = collect(self.clock)
        self.push.assert_called_once_with("https://board.example", TOKEN, SNAP)
        self.assertIn("botroll: sample failed: TimeoutExpired: Command '['ps']' timed out after 20 seconds", err)
        self.assertIn("botroll: sample failed: RuntimeError: boom", err)
        self.assertEqual(self.clock.naps, [60, 60])

    def test_push_errors_of_any_kind_do_not_stop_the_loop(self):
        self.push.side_effect = [http.client.RemoteDisconnected("gone"), ConnectionResetError("reset"),
                                 ValueError("/api/health: 500 nope"), None]
        with mock.patch.object(health, "sample", return_value=SNAP):
            err = collect(self.clock, iterations=4)
        self.assertEqual(self.push.call_count, 4)
        self.assertIn("botroll: push failed: RemoteDisconnected: gone", err)
        self.assertIn("botroll: push failed: ConnectionResetError: reset", err)
        self.assertIn("botroll: push failed: /api/health: 500 nope", err)

    def test_store_errors_do_not_stop_the_loop(self):
        with mock.patch.object(health, "sample", return_value=SNAP), \
                mock.patch.object(health, "append", side_effect=[OSError("disk full"), 1]) as append:
            err = collect(self.clock, url=None, iterations=2)
        self.assertEqual(append.call_count, 2)
        self.assertIn("botroll: store failed: OSError: disk full", err)

    def test_startup_command_fetch_errors_are_logged(self):
        self.fetch.side_effect = http.client.RemoteDisconnected("gone")
        with mock.patch.object(health, "sample", return_value=SNAP):
            err = collect(self.clock, iterations=1)
        self.assertIn("botroll: control: couldn't refuse queued commands: RemoteDisconnected: gone", err)
        self.push.assert_called_once()

    def test_startup_404_means_control_is_off_and_is_quiet(self):
        self.fetch.side_effect = ValueError('/api/health/commands: 404 {"error": "health control is off"}')
        with mock.patch.object(health, "sample", return_value=SNAP):
            self.assertEqual(collect(self.clock, iterations=1), "")

    def test_control_poll_errors_are_logged(self):
        self.fetch.side_effect = [http.client.RemoteDisconnected("gone")] + [[]] * 10
        err = io.StringIO()
        with mock.patch.object(health, "sample", return_value=SNAP), contextlib.redirect_stderr(err):
            health.collect({"control": True}, "/nonexistent", "https://board.example", TOKEN, 60, iterations=2,
                           wall=lambda: self.clock.wall, mono=lambda: self.clock.mono, sleep=self.clock.sleep)
        self.assertIn("botroll: control poll: RemoteDisconnected: gone", err.getvalue())
        self.assertEqual(self.push.call_count, 2)

    def test_interrupts_still_end_it(self):
        with mock.patch.object(health, "sample", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            collect(self.clock)

    def test_interrupts_in_the_control_poll_still_end_it(self):
        report = mock.patch.object(health, "report_command").start()
        cmd = [{"id": "c1", "nonce": "n1"}]
        for fetch, rep in ((KeyboardInterrupt, None), (SystemExit(3), None), ([cmd], KeyboardInterrupt)):
            with self.subTest(fetch=fetch, report=rep):
                self.fetch.side_effect, report.side_effect = fetch, rep
                with mock.patch.object(health, "sample", return_value=SNAP), \
                        mock.patch.object(health, "run_commands", return_value=[{"id": "c1", "nonce": "n1",
                                                                               "status": "ok", "detail": "x"}]), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(BaseException) as cm:
                    health.collect({"control": True}, "/nonexistent", "https://board.example", TOKEN, 60,
                                   iterations=3, wall=lambda: self.clock.wall, mono=lambda: self.clock.mono,
                                   sleep=self.clock.sleep)
                self.assertIsInstance(cm.exception, (KeyboardInterrupt, SystemExit))

    def test_once_still_raises(self):
        with mock.patch.object(health, "sample", side_effect=subprocess.TimeoutExpired(["ps"], 20)), \
                self.assertRaises(subprocess.TimeoutExpired):
            collect(self.clock, every=0)
        self.push.side_effect = ValueError("/api/health: 500 nope")
        with mock.patch.object(health, "sample", return_value=SNAP), self.assertRaisesRegex(ValueError, "500"):
            collect(self.clock, every=0)


class Stall(unittest.TestCase):
    def run_with(self, jumps, iterations=3):
        clock = Clock()
        clock.jumps = jumps
        with mock.patch.object(health, "fetch_commands", return_value=[]), mock.patch.object(health, "push"), \
                mock.patch.object(health, "sample", return_value=SNAP):
            return collect(clock, iterations=iterations)

    def test_logs_a_late_iteration(self):
        err = self.run_with({1: 300})  # the machine slept 300 s during the first wait
        self.assertEqual(err.count("stalled"), 1)
        self.assertIn("botroll: stalled 300 s: this sample started 300 s after it was due "
                      "(the monotonic clock moved 60 s since the last one)", err)

    def test_quiet_when_on_time_or_only_a_little_late(self):
        self.assertEqual(self.run_with({}), "")
        self.assertEqual(self.run_with({1: 120}), "")  # exactly 2 x every is not a stall

    def test_a_slow_iteration_counts_too(self):
        clock = Clock()

        def slow(cfg):
            clock.wall += 200
            clock.mono += 200
            return SNAP
        with mock.patch.object(health, "fetch_commands", return_value=[]), mock.patch.object(health, "push"), \
                mock.patch.object(health, "sample", side_effect=slow):
            err = collect(clock, iterations=2)
        self.assertIn("botroll: stalled 141 s", err)  # 200 s sample + 1 s minimum wait, due after 60


class Stamps(unittest.TestCase):
    def test_every_line_is_stamped_once(self):
        out = io.StringIO()
        s = health.Stamped(out, clock=lambda: 0)
        s.write("one\ntwo ")
        s.write("still two\n")
        s.writelines(["three\n", "four\n"])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        for line, text in zip(lines, ["one", "two still two", "three", "four"]):
            self.assertRegex(line, "^" + STAMP + re.escape(text) + "$")

    def test_stamp_is_iso_with_offset(self):
        self.assertRegex(health.stamp(0) + " ", "^" + STAMP + "$")

    def test_cli_health_lines_are_stamped(self):
        from botroll.cli import main
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(err), \
                mock.patch.object(health, "sample", side_effect=subprocess.TimeoutExpired(["ps"], 20)), \
                mock.patch.object(health, "collect", wraps=health.collect) as loop, \
                mock.patch("time.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            main(["--dir", d, "health", "--every", "60"])
        loop.assert_called_once()
        lines = err.getvalue().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, "^" + STAMP + "botroll: ")
        self.assertIn("sample failed: TimeoutExpired", err.getvalue())

    def test_stderr_is_restored(self):
        import sys
        real = sys.stderr
        with health.stamped_stderr():
            self.assertIsNot(sys.stderr, real)
        self.assertIs(sys.stderr, real)


# Commands over HTTP: no redirects, and dropped connections retried ---------------------------------

class Seen(http.server.ThreadingHTTPServer):
    def __init__(self, handler, **kw):
        self.seen, self.lock = [], threading.Lock()
        self.__dict__.update(kw)
        super().__init__(("127.0.0.1", 0), handler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class Record(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def note(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with self.server.lock:
            self.server.seen.append((self.command, self.path, self.headers.get("Authorization"), body))

    def answer(self, code, body=b"{}", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Elsewhere(Record):
    def do_GET(self):
        self.note()
        self.answer(200, b'{"commands": [{"id": "stolen"}]}')
    do_POST = do_GET


class Redirect(Record):
    def do_GET(self):
        self.note()
        self.answer(self.server.code, b"moved", [("Location", self.server.target + self.path)])
    do_POST = do_GET


class SameHost(Record):
    """Redirects the command routes to another path on the same server, which would answer with a command."""

    def do_GET(self):
        self.note()
        if self.path.startswith("/api/"):
            self.answer(self.server.code, b"moved", [("Location", "/moved" + self.path)])
        else:
            self.answer(200, b'{"commands": [{"id": "stolen"}]}')
    do_POST = do_GET


class Flaky(Record):
    """Reads each request, then closes without a reply the first `drops` times."""

    def do_GET(self):
        self.note()
        with self.server.lock:
            drop = self.server.drops > 0
            self.server.drops -= drop
        if drop:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.answer(200, b'{"commands": [{"id": "c1"}]}' if self.command == "GET" else b'{"id": "c1"}')
    do_POST = do_GET


def serve(test, server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return server


class Commands(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BOTROLL_RETRIES": "3", "BOTROLL_RETRY_BACKOFF": "0"})
        env.start()
        self.addCleanup(env.stop)

    def test_redirects_are_not_followed_and_the_token_is_not_resent(self):
        other = serve(self, Seen(Elsewhere))
        for code in (301, 302, 303, 307, 308):
            board = serve(self, Seen(Redirect, code=code, target=other.url))
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(ValueError, f"/api/health/commands: {code}"):
                    health.fetch_commands(board.url, TOKEN)
                with self.assertRaisesRegex(ValueError, f"/api/health/commands/result: {code}"):
                    health.report_command(board.url, TOKEN, "c1", "ok", "done", "n1")
            self.assertEqual(len(board.seen), 2)  # one each, and a redirect reply is never retried
            self.assertTrue(all(auth == f"Bearer {TOKEN}" for _, _, auth, _ in board.seen))
        self.assertEqual(other.seen, [])  # nothing reached the redirect target, so no Authorization either

    def test_same_host_redirects_are_not_followed_either(self):
        for code in (301, 302, 303, 307, 308):
            board = serve(self, Seen(SameHost, code=code))
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(ValueError, f"/api/health/commands: {code}"):
                    health.fetch_commands(board.url, TOKEN)
                with self.assertRaisesRegex(ValueError, f"/api/health/commands/result: {code}"):
                    health.report_command(board.url, TOKEN, "c1", "ok", "done", "n1")
            self.assertEqual([path for _, path, _, _ in board.seen],
                             ["/api/health/commands", "/api/health/commands/result"])  # /moved/... never requested

    def test_fetch_retries_a_dropped_connection(self):
        board = serve(self, Seen(Flaky, drops=2))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(health.fetch_commands(board.url, TOKEN), [{"id": "c1"}])
        self.assertEqual([m for m, *_ in board.seen], ["GET"] * 3)
        self.assertEqual(err.getvalue().count("retry"), 2)

    def test_report_retries_a_dropped_connection(self):
        board = serve(self, Seen(Flaky, drops=1))
        with contextlib.redirect_stderr(io.StringIO()):
            health.report_command(board.url, TOKEN, "c1", "ok", "terminated", "n1")
        self.assertEqual(len(board.seen), 2)
        self.assertEqual({json.loads(b)["nonce"] for *_, b in board.seen}, {"n1"})  # the same report, resent

    def test_gives_up_with_a_value_error(self):
        board = serve(self, Seen(Flaky, drops=10))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, "connection dropped"):
            health.fetch_commands(board.url, TOKEN)
        self.assertEqual(len(board.seen), 4)  # the first try and 3 retries

    def test_https_only_for_remote_hosts(self):
        for call in (lambda: health.fetch_commands("http://board.example", TOKEN),
                     lambda: health.report_command("http://board.example", TOKEN, "c1", "ok")):
            with self.assertRaisesRegex(ValueError, "without https"):
                call()


class NotRunTwice(unittest.TestCase):
    """A command whose result report fails is still pending on the server and comes back on the next poll."""

    def setUp(self):
        self.clock = Clock()
        self.kills = []
        mock.patch.object(health, "sample", return_value=SNAP).start()
        mock.patch.object(health, "push").start()
        mock.patch.object(health, "allowed_target", return_value=None).start()
        mock.patch.object(health, "terminate", side_effect=lambda pid: self.kills.append(pid) or "terminated").start()
        self.report = mock.patch.object(health, "report_command").start()
        self.addCleanup(mock.patch.stopall)

    def cmd(self, cid="c1", nonce="n1", pid=4242):
        return {"id": cid, "nonce": nonce, "kind": "close", "target": {"pid": pid},
                "expires_at": time.time() + 3600}

    def run_polls(self, polls):
        fetch = mock.patch.object(health, "fetch_commands", side_effect=polls + [[]] * 50).start()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            health.collect({"control": True}, "/nonexistent", "https://board.example", TOKEN, 10,
                           iterations=len(polls) + 1, wall=lambda: self.clock.wall, mono=lambda: self.clock.mono,
                           sleep=self.clock.sleep)  # every 10 s: one poll between samples
        self.assertEqual(fetch.call_count, len(polls))
        return err.getvalue()

    def test_a_failed_report_does_not_rerun_the_command(self):
        gone = ValueError("/api/health/commands/result: connection dropped after 4 tries")
        self.report.side_effect = [gone, gone, None]
        err = self.run_polls([[self.cmd()], [self.cmd()], [self.cmd()]])
        self.assertEqual(self.kills, [4242])  # ran once, though the server returned it three times
        self.assertEqual([c.args for c in self.report.call_args_list], [("https://board.example", TOKEN, "c1",
                                                                        "ok", "terminated", "n1")] * 3)
        self.assertIn("control report: /api/health/commands/result: connection dropped after 4 tries; "
                      "it won't be run again", err)
        self.assertEqual(err.count("(reported again, not re-run)"), 2)

    def test_reported_or_new_commands_still_run(self):
        self.report.side_effect = [ValueError("drop"), None, None, None]
        self.run_polls([[self.cmd()], [self.cmd()], [self.cmd(nonce="n2")], [self.cmd(cid="c2", nonce="n3")]])
        self.assertEqual(self.kills, [4242, 4242, 4242])  # n1 once; a re-queued Close (new nonce) and c2 run

    def test_memory_is_bounded(self):
        self.report.side_effect = ValueError("drop")
        polls = [[self.cmd(cid=f"c{i}", nonce=f"n{i}")] for i in range(health.UNREPORTED_MAX + 5)]
        polls.append([self.cmd(cid="c0", nonce="n0")])  # forgotten (oldest), so it would run again
        polls.append([self.cmd(cid=f"c{health.UNREPORTED_MAX + 4}", nonce=f"n{health.UNREPORTED_MAX + 4}")])
        with mock.patch.object(health, "_say"):
            self.run_polls(polls)
        self.assertEqual(len(self.kills), health.UNREPORTED_MAX + 6)


if __name__ == "__main__":
    unittest.main()
