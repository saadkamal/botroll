import hashlib
import hmac
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime
from unittest import mock
from urllib.parse import urlencode

from botroll import actions
from botroll.board import page, snapshot
from botroll.serve import configure, make_server

from tests.test_serve import HASH, PASSWORD, SECRET, TOKEN, Response

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples" / "tasks"
NOW = datetime(2026, 10, 10, 15, 30)
HOOK_SECRET = "hook-secret-for-tests-xxxxxxxxxxx"
LONG = "s" * 32


class Hook(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        delay = getattr(self.server, "delay", 0)
        if delay:
            time.sleep(delay)
        self.server.inbox.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(self.server.status)
        self.send_header("Content-Length", "7")
        self.end_headers()
        self.wfile.write(b"ignored")

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "/elsewhere")
        self.end_headers()


class Drip(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        try:
            for ch in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                self.wfile.write(bytes([ch]))
                self.wfile.flush()
                time.sleep(0.75)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def start_hook(status=200, handler=Hook):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.status = status
    server.inbox = []
    server.delay = 0
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server


class Unit(unittest.TestCase):
    def test_no_config_is_empty_and_invisible(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        self.assertEqual(actions.load(data, {}), {})
        html = page(snapshot(str(EXAMPLES), NOW, {}))
        self.assertNotIn('action="/action"', html)
        self.assertNotIn(">Approve</button>", html)
        self.assertNotIn(">Nudge</button>", html)

    def test_signature_roundtrip_and_rejects_tampering(self):
        body = b'{"event":"botroll.action","action":"nudge"}'
        ts = int(time.time())
        header = actions.sign(HOOK_SECRET, body, ts)
        self.assertTrue(header.startswith(f"t={ts},v1="))
        self.assertTrue(actions.verify(HOOK_SECRET, body, header))
        self.assertFalse(actions.verify("wrong", body, header))
        self.assertFalse(actions.verify(HOOK_SECRET, b'{"event":"nope"}', header))
        self.assertFalse(actions.verify(HOOK_SECRET, body, header.replace("v1=", "v1=00")))
        old = actions.sign(HOOK_SECRET, body, ts - 400)
        self.assertFalse(actions.verify(HOOK_SECRET, body, old))
        digest = hmac.new(HOOK_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(header, f"t={ts},v1={digest}")

    def test_ssrf_blocks_private_cgnat_and_plain_http(self):
        for url in ("http://127.0.0.1/", "https://10.0.0.1/h", "https://192.168.1.8/h",
                    "https://169.254.169.254/", "https://[::1]/", "https://0.0.0.0/",
                    "https://100.64.0.1/h", "https://[::ffff:100.64.0.1]/h",
                    "file:///etc/passwd", "http://example.com/h"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                actions.check_url(url)
        with self.assertRaises(ValueError):
            actions.check_url("http://127.0.0.1/", allow_http=True)
        actions.check_url("http://127.0.0.1/", allow_http=True, allow_private=True)
        ep = actions.Endpoint("https://127.0.0.1:1/x", LONG)
        with self.assertRaises(ValueError):
            actions.deliver(ep, b"{}", 1)

    def test_load_file_env_and_header(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        spec = {"assistant": {"url": "http://127.0.0.1/hook", "secret": LONG,
                              "allow_http": True, "allow_private": True,
                              "header": "Authorization: Bearer abc"}}
        with open(os.path.join(data, "actions.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(spec))
        found = actions.load(data, {})
        self.assertEqual(found["assistant"].url, "http://127.0.0.1/hook")
        self.assertEqual(found["assistant"].header, ("Authorization", "Bearer abc"))
        found = actions.load(data, {"BOTROLL_ACTION_ASSISTANT_URL": "http://127.0.0.1/other",
                                    "BOTROLL_ACTION_ASSISTANT_SECRET": "t" * 32,
                                    "BOTROLL_ACTION_ASSISTANT_HEADER": "X-Key: k",
                                    "BOTROLL_ACTIONS_ALLOW_HTTP": "1",
                                    "BOTROLL_ACTIONS_ALLOW_PRIVATE": "1"})
        self.assertEqual(found["assistant"].url, "http://127.0.0.1/other")
        self.assertEqual(found["assistant"].secret, "t" * 32)
        self.assertEqual(found["assistant"].header, ("X-Key", "k"))
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other)
        path = os.path.join(other, "hooks.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "qa": {"url": "http://127.0.0.1/qa", "header": {"name": "X-Api-Key", "value": "k"},
                       "allow_http": True, "allow_private": True}}))
        found = actions.load(data, {"BOTROLL_ACTIONS": path})
        self.assertIn("qa", found)
        self.assertEqual(found["qa"].header, ("X-Api-Key", "k"))
        self.assertNotIn("assistant", found)

    def test_load_needs_secret_or_header(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        with self.assertRaises(ValueError):
            actions.load(data, {"BOTROLL_ACTION_X_URL": "https://example.com/h"})
        with self.assertRaisesRegex(ValueError, "private"):
            configure(data, {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET,
                             "BOTROLL_ACTION_ASSISTANT_URL": "https://127.0.0.1/h",
                             "BOTROLL_ACTION_ASSISTANT_SECRET": LONG})

    def test_secret_must_be_32_and_names_must_be_well_formed(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        with self.assertRaisesRegex(ValueError, "32"):
            actions.load(data, {"BOTROLL_ACTION_X_URL": "https://1.1.1.1/h",
                                "BOTROLL_ACTION_X_SECRET": "short"})
        with self.assertRaisesRegex(ValueError, "BOTROLL_ACTION_"):
            actions.load(data, {"BOTROLL_ACTION_FOO": "x"})
        with self.assertRaisesRegex(ValueError, "BOTROLL_ACTION_"):
            actions.load(data, {"BOTROLL_ACTION_assistant_URL": "https://1.1.1.1/h"})
        with self.assertRaisesRegex(ValueError, "_URL"):
            actions.load(data, {"BOTROLL_ACTION_X_SECRET": LONG})

    def test_strict_booleans(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        with self.assertRaisesRegex(ValueError, "BOTROLL_ACTIONS_ALLOW_HTTP"):
            actions.load(data, {"BOTROLL_ACTIONS_ALLOW_HTTP": "maybe"})
        with self.assertRaisesRegex(ValueError, "allow_http"):
            with open(os.path.join(data, "actions.json"), "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "x": {"url": "https://1.1.1.1/h", "secret": LONG, "allow_http": "maybe"}}))
            actions.load(data, {})
        os.remove(os.path.join(data, "actions.json"))
        for on in ("1", "true", "YES", "on"):
            found = actions.load(data, {"BOTROLL_ACTION_X_URL": "http://127.0.0.1/h",
                                        "BOTROLL_ACTION_X_SECRET": LONG,
                                        "BOTROLL_ACTIONS_ALLOW_HTTP": on,
                                        "BOTROLL_ACTIONS_ALLOW_PRIVATE": "yes"})
            self.assertTrue(found["x"].allow_http)
        for off in ("0", "false", "NO", "off", ""):
            with self.assertRaises(ValueError):
                actions.load(data, {"BOTROLL_ACTION_X_URL": "http://127.0.0.1/h",
                                    "BOTROLL_ACTION_X_SECRET": LONG,
                                    "BOTROLL_ACTIONS_ALLOW_HTTP": off})

    def test_port_policy(self):
        actions.check_url("https://1.1.1.1/")
        with self.assertRaisesRegex(ValueError, "port"):
            actions.check_url("https://1.1.1.1:9000/")
        with self.assertRaisesRegex(ValueError, "port"):
            actions.check_url("https://1.1.1.1:8443/")
        actions.check_url("https://1.1.1.1:8443/", allow_http=True)
        actions.check_url("https://1.1.1.1:9000/", allow_ports=True)
        actions.check_url("http://1.1.1.1:8080/", allow_http=True)

    def test_allowed_and_payload(self):
        self.assertTrue(actions.allowed("approve", "waiting-human"))
        self.assertFalse(actions.allowed("approve", "doing"))
        self.assertTrue(actions.allowed("nudge", "doing"))
        self.assertFalse(actions.allowed("nudge", "done"))
        item = {"id": "T-1", "title": "Hi", "state": "waiting-human", "note": "?"}
        body = actions.payload("approve", "assistant", item, "", "https://board.example", 100, "rid")
        self.assertEqual(body["event"], "botroll.action")
        self.assertEqual(body["note"], "?")
        self.assertEqual(body["actor"], "owner")
        self.assertEqual(body["id"], "rid")

    def test_claim_is_durable_and_dedupes_on_id(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        entry = {"id": "rid-1", "bot": "a", "item_id": "T-1", "action": "nudge",
                 "note": "", "sent_at": int(time.time()), "actor": "owner"}
        self.assertIsNone(actions.claim(data, entry))
        self.assertIsNone(actions.records(data)["rid-1"]["status"])
        self.assertEqual(actions.claim(data, entry), "dup")
        other = {**entry, "id": "rid-2"}
        self.assertEqual(actions.claim(data, other), "rate")
        actions.finish(data, "rid-1", 200)
        self.assertEqual(actions.records(data)["rid-1"]["status"], 200)

    def test_concurrent_claims_of_one_id_succeed_once(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        entry = {"id": "same", "bot": "a", "item_id": "T-1", "action": "nudge",
                 "note": "", "sent_at": int(time.time()), "actor": "owner"}
        hits = []

        def go():
            hits.append(actions.claim(data, entry))

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(hits.count(None), 1)
        self.assertEqual(hits.count("dup"), 7)
        self.assertEqual(len(actions.records(data)), 1)

    def test_rebinding_connects_to_the_validated_ip(self):
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with mock.patch("botroll.actions.socket.getaddrinfo", return_value=info):
            target = actions.resolve("https://evil.example/hook")
            self.assertEqual((target.ip, target.host, target.host_header),
                             ("8.8.8.8", "evil.example", "evil.example"))
            created = []

            def fake_connect(addr, timeout=None):
                created.append(addr)
                raise OSError("nope")

            ep = actions.Endpoint("https://evil.example/hook", LONG)
            with mock.patch("botroll.actions.socket.create_connection", fake_connect):
                self.assertEqual(actions.deliver(ep, b"{}", 1), 0)
            self.assertEqual(created, [("8.8.8.8", 443)])

            sock, wrapped = mock.MagicMock(), mock.MagicMock()
            wrapped.send.side_effect = len
            wrapped.recv.side_effect = [b"HTTP/1.1 200 OK\r\n\r\n", b""]
            ctx = mock.MagicMock()
            ctx.wrap_socket.return_value = wrapped
            with mock.patch("botroll.actions.socket.create_connection", return_value=sock), \
                    mock.patch("botroll.actions.ssl.create_default_context", return_value=ctx):
                self.assertEqual(actions.deliver(ep, b"{}", int(time.time())), 200)
            self.assertEqual(ctx.wrap_socket.call_args[1]["server_hostname"], "evil.example")
            sent = b"".join(c.args[0] for c in wrapped.send.call_args_list)
            self.assertIn(b"Host: evil.example\r\n", sent)

    def test_slow_drip_hits_the_absolute_deadline(self):
        drip = start_hook(handler=Drip)
        self.addCleanup(drip.shutdown)
        self.addCleanup(drip.server_close)
        port = drip.server_address[1]
        ep = actions.Endpoint(f"http://127.0.0.1:{port}/hook", LONG,
                              allow_http=True, allow_private=True, allow_ports=True)
        t0 = time.monotonic()
        status = actions.deliver(ep, b"{}", int(time.time()))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 12)
        self.assertGreaterEqual(elapsed, 9)
        self.assertIn(status, (0, 200))


class Live(unittest.TestCase):
    status = 200

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)
        for f in EXAMPLES.glob("*.md"):
            shutil.copy(f, self.data)
        self.hook = start_hook(self.status)
        self.addCleanup(self.hook.shutdown)
        self.addCleanup(self.hook.server_close)
        env = {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET, "BOTROLL_PUSH_TOKEN": TOKEN,
               "BOTROLL_URL": "https://board.example",
               "BOTROLL_ACTION_ASSISTANT_URL": f"http://127.0.0.1:{self.hook.server_address[1]}/hook",
               "BOTROLL_ACTION_ASSISTANT_SECRET": HOOK_SECRET,
               "BOTROLL_ACTION_ASSISTANT_HEADER": "Authorization: Bearer sink-key",
               "BOTROLL_ACTIONS_ALLOW_HTTP": "1", "BOTROLL_ACTIONS_ALLOW_PRIVATE": "1",
               "BOTROLL_ACTIONS_ALLOW_PORTS": "1"}
        self.cfg = configure(self.data, env)
        self.server = make_server(self.cfg, 0, "127.0.0.1")
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, body=None, headers=None, cookies=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = dict(headers or {})
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if isinstance(body, dict):
            body = urlencode(body)
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        try:
            conn.request(method, path, body=body, headers=headers)
            return Response(conn.getresponse())
        finally:
            conn.close()

    def form_token(self, cookies=None):
        r = self.request("GET", "/login", cookies=cookies)
        cookies = {**(cookies or {}), **r.cookies()}
        return re.search(r'name="csrf" value="([^"]+)"', r.body)[1], cookies

    def session(self):
        csrf, cookies = self.form_token()
        r = self.request("POST", "/login", {"password": PASSWORD, "csrf": csrf}, cookies=cookies)
        self.assertEqual(r.status, 303)
        return {**cookies, **r.cookies()}

    def board(self, cookies=None):
        cookies = cookies or self.session()
        r = self.request("GET", "/", cookies=cookies)
        self.assertEqual(r.status, 200)
        return r, cookies

    def click(self, cookies, action="approve", item="T-7", bot="assistant", note="", rid=None, csrf=None):
        if csrf is None:
            r = self.request("GET", "/", cookies=cookies)
            csrf = re.search(r'name="csrf" value="([^"]+)"', r.body)[1]
        form = {"csrf": csrf, "bot": bot, "item": item, "action": action,
                "rid": rid or str(uuid.uuid4())}
        if note:
            form["note"] = note
        return self.request("POST", "/action", form, cookies=cookies), form["rid"]


class Off(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)
        for f in EXAMPLES.glob("*.md"):
            shutil.copy(f, self.data)
        self.cfg = configure(self.data, {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET,
                                         "BOTROLL_PUSH_TOKEN": TOKEN})
        self.server = make_server(self.cfg, 0, "127.0.0.1")
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, body=None, headers=None, cookies=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = dict(headers or {})
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if isinstance(body, dict):
            body = urlencode(body)
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        try:
            conn.request(method, path, body=body, headers=headers)
            return Response(conn.getresponse())
        finally:
            conn.close()

    def session(self):
        r = self.request("GET", "/login")
        cookies = r.cookies()
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.body)[1]
        r = self.request("POST", "/login", {"password": PASSWORD, "csrf": csrf}, cookies=cookies)
        return {**cookies, **r.cookies()}

    def test_no_config_invisible_csrf_and_unauth(self):
        self.assertEqual(self.request("POST", "/action", {"csrf": "x", "action": "nudge"}).status, 401)
        self.assertEqual(self.request("GET", "/action").status, 401)
        cookies = self.session()
        board = self.request("GET", "/", cookies=cookies)
        self.assertEqual(board.status, 200)
        self.assertNotIn('action="/action"', board.body)
        self.assertNotIn(">Approve</button>", board.body)
        csrf = re.search(r'name="csrf" value="([^"]+)"', board.body)[1]
        self.assertEqual(self.request("POST", "/action", {
            "csrf": "forged", "bot": "assistant", "item": "T-7", "action": "approve",
            "rid": str(uuid.uuid4())}, cookies=cookies).status, 403)
        self.assertEqual(self.request("POST", "/action", {
            "csrf": csrf, "bot": "assistant", "item": "T-7", "action": "approve",
            "rid": str(uuid.uuid4())}, cookies=cookies).status, 404)


class Send(Live):
    def test_signed_post_and_success_display(self):
        cookies = self.session()
        r, cookies_board = self.board(cookies)
        self.assertIn(">Approve</button>", r.body)
        self.assertIn(">Nudge</button>", r.body)
        self.assertIn('name="bot" value="assistant"', r.body)
        posted, rid = self.click(cookies, "approve", "T-7")
        self.assertEqual(posted.status, 303)
        self.assertEqual(len(self.hook.inbox), 1)
        headers, raw = self.hook.inbox[0]
        self.assertTrue(actions.verify(HOOK_SECRET, raw, headers["x-botroll-signature"]))
        self.assertEqual(headers["authorization"], "Bearer sink-key")
        payload = json.loads(raw)
        self.assertEqual(payload["event"], "botroll.action")
        self.assertEqual(payload["action"], "approve")
        self.assertEqual(payload["bot"], "assistant")
        self.assertEqual(payload["item_id"], "T-7")
        self.assertEqual(payload["actor"], "owner")
        self.assertEqual(payload["board_url"], "https://board.example")
        self.assertEqual(payload["id"], rid)
        again = self.request("GET", "/", cookies=cookies)
        self.assertIn("Sent ✓", again.body)
        self.assertIn("pending action", again.body)

    def test_csrf_and_unauth(self):
        self.assertEqual(self.request("POST", "/action", {
            "csrf": "x", "bot": "assistant", "item": "T-7", "action": "approve",
            "rid": str(uuid.uuid4())}).status, 401)
        cookies = self.session()
        self.assertEqual(self.request("POST", "/action", {
            "csrf": "forged", "bot": "assistant", "item": "T-7", "action": "approve",
            "rid": str(uuid.uuid4())}, cookies=cookies).status, 403)

    def test_dedupe_same_rid(self):
        cookies = self.session()
        rid = str(uuid.uuid4())
        a, _ = self.click(cookies, rid=rid)
        b, _ = self.click(cookies, rid=rid)
        self.assertEqual(a.status, 303)
        self.assertEqual(b.status, 303)
        self.assertEqual(len(self.hook.inbox), 1)

    def test_concurrent_identical_clicks_are_delivered_once(self):
        cookies = self.session()
        r = self.request("GET", "/", cookies=cookies)
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.body)[1]
        rid = str(uuid.uuid4())
        self.hook.delay = 0.25
        statuses = []

        def go():
            posted, _ = self.click(cookies, rid=rid, csrf=csrf)
            statuses.append(posted.status)

        threads = [threading.Thread(target=go) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.hook.inbox), 1)
        self.assertEqual(statuses.count(303), 5)

    def test_in_flight_across_restart_is_not_resent(self):
        rid = str(uuid.uuid4())
        actions.claim(self.data, {"id": rid, "bot": "assistant", "item_id": "T-7",
                                  "action": "approve", "note": "", "sent_at": int(time.time()),
                                  "actor": "owner"})
        cookies = self.session()
        posted, _ = self.click(cookies, rid=rid)
        self.assertEqual(posted.status, 303)
        self.assertEqual(len(self.hook.inbox), 0)
        self.assertIsNone(actions.records(self.data)[rid]["status"])

    def test_rate_limit_per_item(self):
        cookies = self.session()
        a, _ = self.click(cookies)
        self.assertEqual(a.status, 303)
        b, _ = self.click(cookies)
        self.assertEqual(b.status, 429)
        self.assertEqual(len(self.hook.inbox), 1)

    def test_nudge_on_doing_and_reject_approve(self):
        cookies = self.session()
        bad, _ = self.click(cookies, "approve", "T-6")
        self.assertEqual(bad.status, 400)
        ok, _ = self.click(cookies, "nudge", "T-6")
        self.assertEqual(ok.status, 303)
        self.assertEqual(json.loads(self.hook.inbox[0][1])["action"], "nudge")


class Fail(Live):
    status = 500

    def test_failure_display(self):
        cookies = self.session()
        posted, _ = self.click(cookies)
        self.assertEqual(posted.status, 303)
        board = self.request("GET", "/", cookies=cookies)
        self.assertIn("Failed", board.body)
        self.assertNotIn("Sent ✓", board.body)


if __name__ == "__main__":
    unittest.main()


class SyncUnit(unittest.TestCase):
    def test_board_endpoint_from_file_and_env(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        with open(os.path.join(data, "actions.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"*": {"url": "http://127.0.0.1/sync", "secret": LONG,
                                      "allow_http": True, "allow_private": True}}))
        found = actions.load(data, {})
        self.assertEqual(found[actions.BOARD].url, "http://127.0.0.1/sync")
        self.assertEqual(actions.bots_only(found), {})
        found = actions.load(tempfile.gettempdir() + "/nope", {
            "BOTROLL_SYNC_URL": "http://127.0.0.1/s2", "BOTROLL_SYNC_HEADER": "X-Key: k",
            "BOTROLL_ACTIONS_ALLOW_HTTP": "1", "BOTROLL_ACTIONS_ALLOW_PRIVATE": "1"})
        self.assertEqual(found[actions.BOARD].header, ("X-Key", "k"))

    def test_sync_env_is_validated(self):
        nope = tempfile.gettempdir() + "/nope"
        with mock.patch("sys.stderr") as err:
            self.assertEqual(actions.load(nope, {"BOTROLL_SYNC_SECRET": LONG}), {})
            self.assertEqual(actions.load(nope, {"BOTROLL_SYNC_URL": "  "}), {})
        self.assertIn("Sync now button is off", "".join(str(c) for c in err.mock_calls))
        with mock.patch("sys.stderr") as err:
            found = actions.load(nope, {"BOTROLL_SYNC_URLS": "https://x.example",
                                        "BOTROLL_SYNC_URL": "http://127.0.0.1/s", "BOTROLL_SYNC_HEADER": "X: y",
                                        "BOTROLL_ACTIONS_ALLOW_HTTP": "1", "BOTROLL_ACTIONS_ALLOW_PRIVATE": "1"})
        self.assertIn(actions.BOARD, found)
        self.assertIn("ignoring BOTROLL_SYNC_URLS", "".join(str(c) for c in err.mock_calls))
        with self.assertRaises(ValueError):
            actions.load(tempfile.gettempdir() + "/nope", {"BOTROLL_SYNC_URL": "https://x.example",
                                                           "BOTROLL_SYNC_SECRET": "short"})

    def test_file_and_env_both_set_warns_and_env_wins(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        with open(os.path.join(data, "actions.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"*": {"url": "http://127.0.0.1/file", "header": "X: y"}}))
        with mock.patch("sys.stderr") as err:
            found = actions.load(data, {"BOTROLL_SYNC_URL": "http://127.0.0.1/env", "BOTROLL_SYNC_HEADER": "X: z",
                                        "BOTROLL_ACTIONS_ALLOW_HTTP": "1", "BOTROLL_ACTIONS_ALLOW_PRIVATE": "1"})
        self.assertEqual(found[actions.BOARD].url, "http://127.0.0.1/env")
        self.assertIn("using BOTROLL_SYNC_URL", "".join(str(c) for c in err.mock_calls))

    def test_sync_result_shows_time_then_resets(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data)
        actions.claim(data, {"id": "a", "bot": actions.BOARD, "item_id": actions.SYNC_ITEM, "action": "sync",
                             "sent_at": 1_000_000, "actor": "owner"})
        actions.finish(data, "a", 204)
        r = actions.sync_result(data, at=1_000_000 + 5)
        self.assertEqual((r["ok"], r["sent_at"]), (True, 1_000_000))
        self.assertIsNone(actions.sync_result(data, at=1_000_000 + actions.SYNC_SHOW + 1))
        from botroll.board import sync_button
        html_ = sync_button("c", r)
        self.assertRegex(html_, r"Sync sent ✓ <time>\d\d:\d\d</time>")
        self.assertIn("Sync failed <time>", sync_button("c", {**r, "ok": False}))

    def test_payload_has_no_bot_or_item(self):
        p = actions.sync_payload("https://b.example", 5, "rid")
        self.assertEqual((p["event"], p["action"], p["bot"], p["item_id"]), ("botroll.action", "sync", None, None))

    def test_button_only_with_board_endpoint(self):
        snap = snapshot(str(EXAMPLES), NOW, {})
        self.assertNotIn("Sync now", page(snap, csrf="c", endpoints={"assistant": object()}))
        self.assertIn(">Sync now</button>", page(snap, csrf="c", endpoints={"*": object()}))
        self.assertNotIn("Sync now", page(snap, endpoints={"*": object()}))  # static: no csrf, no buttons


class SyncLive(Live):
    def setUp(self):
        super().setUp()
        self.server.shutdown()
        self.server.server_close()
        url = f"http://127.0.0.1:{self.hook.server_address[1]}/sync"
        env = {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET, "BOTROLL_PUSH_TOKEN": TOKEN,
               "BOTROLL_URL": "https://board.example", "BOTROLL_SYNC_URL": url,
               "BOTROLL_SYNC_SECRET": HOOK_SECRET, "BOTROLL_ACTIONS_ALLOW_HTTP": "1",
               "BOTROLL_ACTIONS_ALLOW_PRIVATE": "1", "BOTROLL_ACTIONS_ALLOW_PORTS": "1"}
        self.cfg = configure(self.data, env)
        self.server = make_server(self.cfg, 0, "127.0.0.1")
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def sync(self, cookies, rid=None, csrf=None):
        if csrf is None:
            csrf = re.search(r'name="csrf" value="([^"]+)"', self.request("GET", "/", cookies=cookies).body)[1]
        rid = str(uuid.uuid4()) if rid is None else rid
        return self.request("POST", "/action", {"csrf": csrf, "action": "sync", "rid": rid}, cookies=cookies), rid

    def test_signed_sync_and_status(self):
        r, cookies = self.board()
        self.assertIn(">Sync now</button>", r.body)
        self.assertNotIn(">Approve</button>", r.body)  # no per-bot endpoints configured
        posted, rid = self.sync(cookies)
        self.assertEqual(posted.status, 303)
        self.assertEqual(len(self.hook.inbox), 1)
        headers, raw = self.hook.inbox[0]
        self.assertTrue(actions.verify(HOOK_SECRET, raw, headers["x-botroll-signature"]))
        payload = json.loads(raw)
        self.assertEqual((payload["action"], payload["bot"], payload["id"]), ("sync", None, rid))
        self.assertEqual(payload["board_url"], "https://board.example")
        self.assertIn("Sync sent ✓", self.request("GET", "/", cookies=cookies).body)

    def test_dedupe_rate_limit_csrf_and_unauth(self):
        self.assertEqual(self.request("POST", "/action", {"csrf": "x", "action": "sync",
                                                          "rid": str(uuid.uuid4())}).status, 401)
        _, cookies = self.board()
        self.assertEqual(self.sync(cookies, csrf="forged")[0].status, 403)
        self.assertEqual(self.sync(cookies, rid="not-a-uuid")[0].status, 400)
        _, rid = self.sync(cookies)
        self.assertEqual(self.sync(cookies, rid=rid)[0].status, 303)  # same click: not resent
        self.assertEqual(self.sync(cookies)[0].status, 429)
        self.assertEqual(len(self.hook.inbox), 1)

    def test_request_id_must_be_a_uuid(self):
        _, cookies = self.board()
        for rid in ("", "not-a-uuid", "../x", "1" * 33):
            with self.subTest(rid=rid):
                self.assertEqual(self.sync(cookies, rid=rid)[0].status, 400)
        self.assertEqual(self.hook.inbox, [])
        self.assertIsNone(actions.last_for(self.data, actions.BOARD, actions.SYNC_ITEM))

    def test_no_board_endpoint_rejects_sync(self):
        self.cfg.actions.pop(actions.BOARD)
        self.cfg.actions["assistant"] = actions.Endpoint("http://127.0.0.1:9/x", HOOK_SECRET, None, True, True, True)
        _, cookies = self.board()
        self.assertEqual(self.sync(cookies)[0].status, 404)
