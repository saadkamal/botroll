import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import http.server
import io
import json
import os
import shutil
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from unittest import mock

from botroll import health, serve

TOKEN = "t" * 40


class Dropper(http.server.ThreadingHTTPServer):
    """Answers every POST with 200, except that drops[path] times it reads the whole request and then
    misbehaves: "close" closes without a reply (RemoteDisconnected), "short" sends headers and part of the
    body then closes (IncompleteRead), "slow" waits past the client's timeout. An int n means n closes."""

    def __init__(self, drops=None, status=200):
        drops = {k: ["close"] * v if isinstance(v, int) else list(v) for k, v in (drops or {}).items()}
        self.drops, self.status, self.seen, self.lock = drops, status, [], threading.Lock()
        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def count(self, route):
        return sum(1 for p in self.seen if p == route)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        s = self.server
        with s.lock:
            s.seen.append(self.path)
            pending = s.drops.get(self.path)
            drop = pending.pop(0) if pending else None
        if drop == "slow":
            time.sleep(0.5)
        if drop == "short":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"{")
            self.wfile.flush()
        if drop:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        body = json.dumps({"would_remove": []} if self.path == "/api/prune" else {}).encode()
        self.send_response(s.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Refusing:
    """An opener that fails before sending (as urllib reports a refused or cut handshake) n times."""

    def __init__(self, inner, n):
        self.inner, self.n, self.calls = inner, n, 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self.n:
            raise urllib.error.URLError(ConnectionResetError("reset during TLS handshake"))
        return self.inner.open(req, timeout=timeout)


class Raising:
    """An opener that raises exc (as from reading the reply, after sending) n times."""

    def __init__(self, inner, n, exc):
        self.inner, self.n, self.exc, self.calls = inner, n, exc, 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self.n:
            raise self.exc
        return self.inner.open(req, timeout=timeout)


class Retry(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BOTROLL_RETRY_BACKOFF": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        for bot in ("ann", "max"):
            with open(os.path.join(self.dir, bot + ".md"), "w") as f:
                f.write(f"# {bot}\n")
        err = redirect_stderr(io.StringIO())
        self.stderr = err.__enter__()
        self.addCleanup(err.__exit__, None, None, None)

    def serve(self, **kw):
        s = Dropper(**kw)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        self.addCleanup(s.server_close)
        self.addCleanup(s.shutdown)
        return s

    def test_roll_and_watch_retry_after_sending(self):
        s = self.serve(drops={"/api/roll/ann": 2, "/api/watch": 1})
        self.assertEqual(serve.push(s.url, TOKEN, self.dir), (2, 0, 0))
        self.assertEqual(s.count("/api/roll/ann"), 3)
        self.assertEqual(s.count("/api/watch"), 2)
        self.assertIn("retry 2/3", self.stderr.getvalue())

    def test_prune_is_not_retried_after_sending(self):
        s = self.serve(drops={"/api/prune": 1})
        with self.assertRaisesRegex(ValueError, "/api/prune: connection dropped after sending.*not retried"):
            serve.push(s.url, TOKEN, self.dir, prune=True, confirm=True)
        self.assertEqual(s.count("/api/prune"), 1)

    def test_prune_succeeds_without_drops(self):
        s = self.serve()
        removed = []
        serve.push(s.url, TOKEN, self.dir, prune=True, removed=removed)
        self.assertEqual(s.count("/api/prune"), 1)

    def test_drop_before_sending_retries_any_route(self):
        s = self.serve()
        opener = Refusing(urllib.request.build_opener(), 2)
        req = urllib.request.Request(s.url + "/api/prune", data=b"{}", method="POST")
        self.assertEqual(json.loads(serve.send(opener, req, idempotent=False)), {"would_remove": []})
        self.assertEqual((opener.calls, s.count("/api/prune")), (3, 1))

    def test_gives_up_after_retries(self):
        s = self.serve(drops={"/api/roll/ann": 9})
        with mock.patch.dict(os.environ, {"BOTROLL_RETRIES": "2"}):
            with self.assertRaisesRegex(ValueError, "/api/roll/ann: connection dropped after sending"):
                serve.push(s.url, TOKEN, self.dir)
        self.assertEqual(s.count("/api/roll/ann"), 3)

    def test_zero_retries_turns_it_off(self):
        opener = Refusing(urllib.request.build_opener(), 1)
        req = urllib.request.Request("http://127.0.0.1:9/api/watch", data=b"[]", method="POST")
        with self.assertRaisesRegex(ValueError, "can't reach http://127.0.0.1:9"):
            serve.send(opener, req, idempotent=True, retries=0)
        self.assertEqual(opener.calls, 1)

    def test_backoff_doubles_and_caps(self):
        opener = Refusing(urllib.request.build_opener(), 9)
        req = urllib.request.Request("http://127.0.0.1:9/api/watch", data=b"[]", method="POST")
        with mock.patch.object(serve.time, "sleep") as sleep, self.assertRaises(ValueError):
            serve.send(opener, req, idempotent=True, retries=7, backoff=1)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4, 8, 16, 30, 30])

    def test_a_reply_is_never_retried(self):
        s = self.serve(status=401)
        with self.assertRaisesRegex(ValueError, "/api/roll/ann: 401"):
            serve.push(s.url, TOKEN, self.dir)
        self.assertEqual(s.count("/api/roll/ann"), 1)

    def test_health_push_drop_after_sending_is_a_value_error(self):
        # The collector loop only survives ValueError; the server keeps every sample, so no resend.
        s = self.serve(drops={"/api/health": 1})
        with self.assertRaisesRegex(ValueError, "/api/health: connection dropped after sending"):
            health.push(s.url, TOKEN, {"ts": 1})
        self.assertEqual(s.count("/api/health"), 1)

    def test_health_push_retries_before_sending(self):
        s = self.serve()
        real = urllib.request.build_opener
        opener = Refusing(real(), 2)
        with mock.patch.object(health.urllib.request, "build_opener", lambda *a: opener):
            health.push(s.url, TOKEN, {"ts": 1})
        self.assertEqual((opener.calls, s.count("/api/health")), (3, 1))


    def test_after_send_drops_of_every_kind(self):
        for drop, name in (("short", "IncompleteRead"), ("slow", "timed out")):
            with self.subTest(drop):
                s = self.serve(drops={"/api/watch": [drop], "/api/prune": [drop]})
                req = urllib.request.Request(s.url + "/api/watch", data=b"[]", method="POST")
                self.assertEqual(serve.send(urllib.request.build_opener(), req, idempotent=True, timeout=0.2), b"{}")
                self.assertEqual(s.count("/api/watch"), 2)
                req = urllib.request.Request(s.url + "/api/prune", data=b"{}", method="POST")
                with self.assertRaisesRegex(ValueError, f"dropped after sending.*{name}.*not retried"):
                    serve.send(urllib.request.build_opener(), req, idempotent=False, timeout=0.2)
                self.assertEqual(s.count("/api/prune"), 1)

    def test_ssl_error_after_sending(self):
        s = self.serve()
        for idempotent, calls in ((True, 2), (False, 1)):
            with self.subTest(idempotent=idempotent):
                opener = Raising(urllib.request.build_opener(), 1, ssl.SSLEOFError(8, "EOF in violation of protocol"))
                req = urllib.request.Request(s.url + "/api/watch", data=b"[]", method="POST")
                if idempotent:
                    self.assertEqual(serve.send(opener, req, idempotent=True), b"{}")
                else:
                    with self.assertRaisesRegex(ValueError, "SSLEOFError.*not retried"):
                        serve.send(opener, req, idempotent=False)
                self.assertEqual(opener.calls, calls)

    def test_negative_settings_clamp_to_zero(self):
        opener = Refusing(urllib.request.build_opener(), 9)
        req = urllib.request.Request("http://127.0.0.1:9/api/watch", data=b"[]", method="POST")
        env = {"BOTROLL_RETRIES": "-2", "BOTROLL_RETRY_BACKOFF": "-5"}
        with mock.patch.dict(os.environ, env), self.assertRaises(ValueError):
            serve.send(opener, req, idempotent=True)
        self.assertEqual(opener.calls, 1)
        opener = Refusing(urllib.request.build_opener(), 9)
        with mock.patch.dict(os.environ, {"BOTROLL_RETRIES": "2", "BOTROLL_RETRY_BACKOFF": "-5"}), \
                mock.patch.object(serve.time, "sleep") as sleep, self.assertRaises(ValueError):
            serve.send(opener, req, idempotent=True)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0, 0])

    def test_bad_setting_warns_and_uses_default(self):
        opener = Refusing(urllib.request.build_opener(), 9)
        req = urllib.request.Request("http://127.0.0.1:9/api/watch", data=b"[]", method="POST")
        with mock.patch.dict(os.environ, {"BOTROLL_RETRIES": "2.0", "BOTROLL_RETRY_BACKOFF": "soon"}), \
                mock.patch.object(serve.time, "sleep") as sleep, self.assertRaises(ValueError):
            serve.send(opener, req, idempotent=True)
        self.assertEqual(opener.calls, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0, 4.0])
        err = self.stderr.getvalue()
        self.assertIn("BOTROLL_RETRIES='2.0' is not a whole number; using 3", err)
        self.assertIn("BOTROLL_RETRY_BACKOFF='soon' is not a number; using 1.0", err)


if __name__ == "__main__":
    unittest.main()
