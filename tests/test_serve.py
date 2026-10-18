import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import base64
import hashlib
import hmac
import http.client
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

import botroll
import botroll._build  # noqa: F401  (patched in Version)
from botroll import serve
from botroll.serve import (ATTEMPTS, MAX_JSON_DEPTH, MAX_ROLL, MIN_PASSWORD, SCRIPT_HASH, STYLE_HASH,
                           check_password, configure, hash_password, load_json, make_server)

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples" / "tasks"
NOW = datetime(2026, 10, 10, 15, 30)
SECRET, TOKEN = "s" * 40, "t" * 40
PASSWORD = "correct horse battery staple"
HASH = hash_password(PASSWORD, n=2**4)  # cheap for tests; real hashes use n=2**14


def sign(secret, text, auth=HASH, version=0):
    key = hmac.new(secret.encode(), f"{auth}:{version}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(hmac.new(key, text.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()


class Response:
    def __init__(self, r):
        self.status, self.headers, self.body = r.status, r.headers, r.read().decode()

    def cookies(self):
        return dict(re.match(r"([^=]+)=([^;]*)", c).groups() for c in self.headers.get_all("Set-Cookie") or [])


class Server(unittest.TestCase):
    insecure_local, behind_proxy, token = False, False, TOKEN
    client_ip_header = None

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)
        for f in EXAMPLES.glob("*.md"):
            shutil.copy(f, self.data)
        pushed = NOW.timestamp() - 5 * 60
        for f in pathlib.Path(self.data).iterdir():
            os.utime(f, (pushed, pushed))
        env = mock.patch.dict(os.environ, {"BOTROLL_NOW": NOW.strftime("%Y-%m-%d %H:%M"), "BOTROLL_HUMAN": "Sam"})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = configure(self.data, {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET,
                                         "BOTROLL_PUSH_TOKEN": self.token or ""},
                             insecure_local=self.insecure_local, behind_proxy=self.behind_proxy,
                             client_ip_header=self.client_ip_header)
        self.server = make_server(self.cfg, 0, "127.0.0.1")
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def request(self, method, path, body=None, headers=None, cookies=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = dict(headers or {})
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if isinstance(body, dict):
            body = "&".join(f"{k}={v}" for k, v in body.items())
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

    def sign_in(self, password=PASSWORD, remember=False, headers=None):
        csrf, cookies = self.form_token()
        form = {"password": password.replace(" ", "+"), "csrf": csrf, **({"remember": "1"} if remember else {})}
        return self.request("POST", "/login", form, headers, cookies), cookies

    def session(self):
        r, cookies = self.sign_in()
        self.assertEqual(r.status, 303)
        return {**cookies, **r.cookies()}

    def push(self, path, body, token=TOKEN, **headers):
        return self.request("POST", path, body, {"Authorization": f"Bearer {token}", **headers})


class LargePages(Server):
    """A page bigger than the socket buffer arrives whole: Content-Length matches the bytes sent."""

    def setUp(self):
        real = serve.Handler.setup

        def small_buffer(handler):
            handler.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)  # like a proxy hop that's full
            real(handler)
        patch = mock.patch.object(serve.Handler, "setup", small_buffer)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()
        big = "".join(f"- [doing] T-{i} Überprüfung — café ☕ 日本語 🚀 naïve “quotes” · updated 2026-10-10 15:00\n"
                      for i in range(1, 400))
        pathlib.Path(self.data, "zoe.md").write_text("---\nname: Zoë\nrole: Recherche\n---\n" + big,
                                                    encoding="utf-8")

    def slow_get(self, path, cookies):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n".encode())
        time.sleep(0.3)  # let the server fill the buffers before anything is read
        raw = bytearray()
        with sock:
            while chunk := sock.recv(65536):
                raw += chunk
        head, _, body = bytes(raw).partition(b"\r\n\r\n")
        length = int(re.search(rb"(?im)^content-length: *(\d+)", head)[1])
        return head, length, body

    def test_signed_in_board_arrives_whole(self):
        head, length, body = self.slow_get("/", self.session())
        self.assertIn(b" 200 ", head.split(b"\r\n")[0])
        self.assertGreater(length, 50_000)  # well past the 4 KB buffer
        self.assertEqual(len(body), length)
        self.assertTrue(body.rstrip().endswith(b"</html>"))
        self.assertIn("Zoë", body.decode("utf-8"))

    def test_writer_sends_everything(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        got = bytearray()

        def drain():
            time.sleep(0.2)
            while len(got) < 300_000 and (chunk := b.recv(65536)):
                got.extend(chunk)
        t = threading.Thread(target=drain)
        t.start()
        w = serve._SendAll(a, time.monotonic() + 10)
        self.assertEqual(w.write(b"x" * 300_000), 300_000)
        t.join(10)
        self.assertEqual(len(got), 300_000)


class Version(unittest.TestCase):
    def commit(self, env=None, baked="$Format:%H$"):
        with mock.patch.dict(os.environ, {"BOTROLL_COMMIT": env} if env is not None else {}), \
                mock.patch("botroll._build.COMMIT", baked):
            if env is None:
                os.environ.pop("BOTROLL_COMMIT", None)
            return botroll.commit(), botroll.version_string()

    def test_unknown_in_a_plain_checkout(self):
        self.assertEqual(self.commit(), ("unknown", botroll.__version__))

    def test_baked_in_commit(self):
        self.assertEqual(self.commit(baked="0123456789abcdef0123456789abcdef01234567"),
                         ("0123456789abcdef0123456789abcdef01234567", botroll.__version__ + "+0123456789ab"))

    def test_env_overrides_and_is_normalised(self):
        self.assertEqual(self.commit(env=" ABCDEF1 ", baked="0" * 40)[0], "abcdef1")

    def test_anything_but_a_hex_id_is_ignored(self):
        for bad in ("", "abc", "x" * 40, "a" * 41, "abcdef1\r\nSet-Cookie: x=1", "deadbeef; rm", "../../etc"):
            with self.subTest(bad=bad):
                self.assertEqual(self.commit(env=bad, baked=bad)[0], "unknown")
                self.assertEqual(self.commit(env=bad, baked="b" * 40)[0], "b" * 40)

    def test_package_version(self):
        self.assertEqual(botroll.__version__, "0.1.1")
        toml = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
        self.assertIn('path = "botroll/__init__.py"', toml)  # the one place the version lives

    def test_git_archive_stamps_the_commit(self):
        root = pathlib.Path(__file__).parent.parent
        attrs = (root / ".gitattributes").read_text()
        self.assertIn("botroll/_build.py export-subst", attrs)
        try:
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                                  timeout=20, check=True).stdout.strip()
            listed = subprocess.run(["git", "-C", str(root), "ls-files", "botroll/_build.py", ".gitattributes"],
                                    capture_output=True, text=True, timeout=20, check=True).stdout.split()
            if len(listed) < 2:
                self.skipTest("_build.py or .gitattributes not committed yet")
            raw = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", "HEAD", "botroll/_build.py"],
                                 capture_output=True, timeout=20, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("needs a git checkout")
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            text = tar.extractfile("botroll/_build.py").read().decode()
        self.assertIn(f'COMMIT = "{head}"', text)

    def test_dockerfile_bakes_git_sha(self):
        docker = (pathlib.Path(__file__).parent.parent / "Dockerfile").read_text()
        self.assertIn('ARG GIT_SHA=""', docker)
        self.assertIn("botroll/_build.py", docker)
        self.assertLess(docker.index("ARG GIT_SHA"), docker.index("pip install"))


class Startup(unittest.TestCase):
    def test_refuses_to_start_open(self):
        for env in [{}, {"BOTROLL_SECRET": SECRET}, {"BOTROLL_PASSWORD": "x"},
                    {"BOTROLL_PASSWORD": "x", "BOTROLL_SECRET": "short"}]:
            with self.subTest(env=env), self.assertRaises(ValueError):
                configure("/tmp", env)

    def test_bad_hash_and_short_push_token(self):
        with self.assertRaisesRegex(ValueError, "hash-password"):
            configure("/tmp", {"BOTROLL_PASSWORD_HASH": "plaintext", "BOTROLL_SECRET": SECRET})
        with self.assertRaisesRegex(ValueError, "PUSH_TOKEN"):
            configure("/tmp", {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET, "BOTROLL_PUSH_TOKEN": "short"})

    def test_password_or_hash(self):
        cfg = configure("/tmp", {"BOTROLL_PASSWORD": PASSWORD, "BOTROLL_SECRET": SECRET})
        self.assertTrue(check_password(PASSWORD, cfg.password_hash))
        self.assertFalse(check_password(PASSWORD.upper(), cfg.password_hash))
        self.assertEqual(configure("/tmp", {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET}).password_hash, HASH)

    def test_short_password_and_reused_token(self):
        with self.assertRaisesRegex(ValueError, str(MIN_PASSWORD)):
            configure("/tmp", {"BOTROLL_PASSWORD": "short-one", "BOTROLL_SECRET": SECRET})
        with self.assertRaisesRegex(ValueError, "different from"):
            configure("/tmp", {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET, "BOTROLL_PUSH_TOKEN": SECRET})
        same = "abcde" + "f" * 27
        with self.assertRaisesRegex(ValueError, "different from"):
            configure("/tmp", {"BOTROLL_PASSWORD": same, "BOTROLL_SECRET": SECRET, "BOTROLL_PUSH_TOKEN": same})
        with self.assertRaisesRegex(ValueError, "whitespace"):
            configure("/tmp", {"BOTROLL_PASSWORD": " " * 12, "BOTROLL_SECRET": SECRET})
        with self.assertRaisesRegex(ValueError, "common"):
            configure("/tmp", {"BOTROLL_PASSWORD": "password1234", "BOTROLL_SECRET": SECRET})
        with self.assertRaisesRegex(ValueError, "different characters"):
            configure("/tmp", {"BOTROLL_PASSWORD": "p" * 12, "BOTROLL_SECRET": SECRET})
        reused = "abcde" + "g" * 27
        with self.assertRaisesRegex(ValueError, "BOTROLL_SECRET"):
            configure("/tmp", {"BOTROLL_PASSWORD": reused, "BOTROLL_SECRET": reused})
        cfg = configure("/tmp", {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET}, behind_proxy=True)
        self.assertEqual(cfg.client_ip_header, "")
        cfg = configure("/tmp", {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET,
                                 "BOTROLL_CLIENT_IP_HEADER": "CF-Connecting-IP"}, behind_proxy=True)
        self.assertEqual(cfg.client_ip_header, "CF-Connecting-IP")

    def test_insecure_local_runs_without_secrets_on_loopback_only(self):
        cfg = configure("/tmp", {}, insecure_local=True)
        server = make_server(cfg, 0)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_cli_exits_2(self):
        from botroll.cli import main
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stderr"):
            self.assertEqual(main(["serve", "--data", "/tmp"]), 2)


class Auth(Server):
    def test_health_is_public(self):
        with mock.patch.dict(os.environ, {"BOTROLL_COMMIT": "a" * 40}):
            r = self.request("GET", "/health")
            head = self.request("HEAD", "/health")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, f"ok\nversion: {botroll.__version__}\ncommit: {'a' * 40}\n")
        self.assertEqual(r.body.splitlines()[0], "ok")  # what existing health checks look for
        self.assertEqual(r.headers["X-BotRoll-Version"], f"{botroll.__version__}+{'a' * 12}")
        self.assertEqual((head.status, head.body, head.headers["Content-Length"]),
                         (200, "", str(len(r.body.encode()))))
        self.assertEqual(head.headers["X-BotRoll-Version"], r.headers["X-BotRoll-Version"])

    def test_version_header_only_on_health(self):
        self.assertIsNone(self.request("GET", "/login").headers["X-BotRoll-Version"])
        self.assertIsNone(self.request("GET", "/").headers["X-BotRoll-Version"])

    def test_every_other_route_needs_a_session(self):
        for method, path in [("GET", "/"), ("GET", "/?x=1"), ("GET", "/nope"), ("POST", "/"), ("POST", "/logout"),
                             ("GET", "/static/../"), ("GET", "/health/x")]:
            with self.subTest(path=path):
                r = self.request(method, path, b"", {"Content-Length": "0"})
                self.assertEqual((r.status, r.headers["Location"]), (303, "/login"))
                self.assertNotIn("Ann", r.body)
        self.assertEqual(self.request("GET", "/api/roll/x").status, 401)

    def test_security_headers_everywhere(self):
        for r in [self.request("GET", "/health"), self.request("GET", "/login"), self.request("GET", "/"),
                  self.request("GET", "/api/watch"), self.request("GET", "/" + "x" * 70000)]:
            with self.subTest(status=r.status):
                self.assertEqual(r.headers["Cache-Control"], "no-store")
                self.assertEqual(r.headers["X-Frame-Options"], "DENY")
                self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
                self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", r.headers["Content-Security-Policy"])
                self.assertIsNone(r.headers["Strict-Transport-Security"])

    def test_sign_in_and_out(self):
        r, cookies = self.sign_in()
        self.assertEqual((r.status, r.headers["Location"]), (303, "/"))
        cookie = next(c for c in r.headers.get_all("Set-Cookie") if c.startswith("botroll_session="))
        for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Max-Age", cookie)
        cookies.update(r.cookies())
        board = self.request("GET", "/", cookies=cookies)
        self.assertEqual(board.status, 200)
        self.assertIn("Ann", board.body)
        self.assertEqual(self.request("GET", "/login", cookies=cookies).headers["Location"], "/")

        csrf = re.search(r'name="csrf" value="([^"]+)"', board.body)[1]
        self.assertEqual(self.request("POST", "/logout", {"csrf": "forged"}, cookies=cookies).status, 403)
        out = self.request("POST", "/logout", {"csrf": csrf}, cookies=cookies)
        self.assertEqual((out.status, out.headers["Location"], out.cookies()["botroll_session"]), (303, "/login", ""))
        self.assertIn("Max-Age=0", out.headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/", cookies=cookies).status, 303)  # a copied cookie is revoked too

    def test_theme_is_a_cookie_the_server_renders(self):
        csrf, cookies = self.form_token()
        self.assertIn('<html lang="en">', self.request("GET", "/login", cookies=cookies).body)
        for value in ("forged", ""):
            self.assertEqual(self.request("POST", "/theme", {"theme": "dark", "csrf": value}, cookies=cookies).status, 403)
        self.assertEqual(self.request("POST", "/theme", {"theme": "purple", "csrf": csrf}, cookies=cookies).status, 403)
        r = self.request("POST", "/theme", {"theme": "dark", "csrf": csrf}, cookies=cookies)
        self.assertEqual((r.status, r.headers["Location"], r.cookies()["botroll_theme"]), (303, "/", "dark"))
        cookies.update(r.cookies())
        login = self.request("GET", "/login", cookies=cookies).body
        self.assertIn('<html lang="en" data-theme="dark">', login)
        self.assertIn('value="dark" aria-pressed="true"', login)
        session = {**self.session(), "botroll_theme": "dark"}
        self.assertIn('data-theme="dark"', self.request("GET", "/", cookies=session).body)
        self.assertIn('<html lang="en">', self.request("GET", "/", cookies={**session, "botroll_theme": "x\"><script>"}).body)
        back = self.request("POST", "/theme", {"theme": "system", "csrf": csrf}, cookies=cookies)
        self.assertIn("Max-Age=0", back.headers["Set-Cookie"])

    def test_keep_me_signed_in(self):
        r, _ = self.sign_in(remember=True)
        self.assertIn("Max-Age=2592000", r.headers["Set-Cookie"])

    def test_wrong_password(self):
        for pw in ["", "correct horse", PASSWORD.upper()]:
            r, _ = self.sign_in(pw)
            self.assertEqual(r.status, 401)
            self.assertIn("That password isn&#x27;t right.", r.body)
            self.assertNotIn("botroll_session", r.headers.get("Set-Cookie", ""))

    def test_csrf(self):
        csrf, cookies = self.form_token()
        no_cookie = self.request("POST", "/login", {"password": PASSWORD, "csrf": csrf})
        no_token = self.request("POST", "/login", {"password": PASSWORD}, cookies=cookies)
        other = self.request("POST", "/login", {"password": PASSWORD, "csrf": csrf},
                             cookies={"botroll_csrf": "A" * 43})
        for r in (no_cookie, no_token, other):
            self.assertEqual(r.status, 403)
            self.assertNotIn("botroll_session", r.headers.get("Set-Cookie", ""))

    def test_tampered_cookies(self):
        good = self.session()["botroll_session"]
        expires, sig = good.split(".")
        self.assertEqual(sig, sign(SECRET, "session:" + expires, HASH, self.cfg.session_version))
        later, past = str(int(expires) + 10**6), str(int(time.time()) - 1)
        for value in ["", "x", sig, f"{later}.{sig}", f"{expires}.{sig[:-1]}{'B' if sig[-1] == 'A' else 'A'}", f"{expires}.",
                      f"{expires}.{sign('z' * 40, 'session:' + expires)}",  # signed with another secret
                      f"{later}.{sign(SECRET, 'csrf:' + later)}",  # a signature for another purpose
                      f"{past}.{sign(SECRET, 'session:' + past)}"]:  # expired
            with self.subTest(value=value):
                self.assertEqual(self.request("GET", "/", cookies={"botroll_session": value}).status, 303)
        self.assertEqual(self.request("GET", "/", cookies={"botroll_session": good}).status, 200)

    def test_rate_limit(self):
        for _ in range(ATTEMPTS):
            self.assertEqual(self.sign_in("wrong")[0].status, 401)
        r, _ = self.sign_in()
        self.assertEqual(r.status, 429)
        self.assertGreater(int(r.headers["Retry-After"]), 800)
        self.assertIn("Too many attempts. Try again in 15 minutes.", r.body)
        # Without --behind-proxy, X-Forwarded-For is not trusted to pick a fresh address.
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "203.0.113.9"})[0].status, 429)
        with mock.patch("time.time", return_value=time.time() + 15 * 60 + 1):
            self.assertEqual(self.sign_in()[0].status, 303)

    def test_parallel_guesses_dont_outrun_the_limit(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.sign_in("wrong")[0].status)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [401] * ATTEMPTS + [429] * (20 - ATTEMPTS))

    def test_odd_content_lengths(self):
        for length in ["\N{SUPERSCRIPT TWO}", "1" * 5000, "-1", "1e3", ""]:
            with self.subTest(length=length[:10]):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                conn.putrequest("POST", "/login")
                if length:
                    conn.putheader("Content-Length", length)
                conn.endheaders()
                self.assertEqual(conn.getresponse().status, 411)
                conn.close()

    def test_oversize_form(self):
        csrf, cookies = self.form_token()
        r = self.request("POST", "/login", "password=" + "x" * 5000, cookies=cookies)
        self.assertEqual(r.status, 413)


class BehindProxy(Server):
    behind_proxy = True
    client_ip_header = "X-Real-IP"

    def test_hsts_only_over_https(self):
        self.assertEqual(self.request("GET", "/health", headers={"X-Forwarded-Proto": "https"})
                         .headers["Strict-Transport-Security"], "max-age=31536000; includeSubDomains")
        self.assertIsNone(self.request("GET", "/health").headers["Strict-Transport-Security"])

    def test_requests_refused_before_their_headers(self):
        r = self.request("GET", "/" + "x" * 70000)
        self.assertEqual((r.status, r.headers["X-Frame-Options"]), (414, "DENY"))

    def test_rate_limit_is_per_client(self):
        for _ in range(ATTEMPTS):
            self.sign_in("wrong", headers={"X-Real-IP": "203.0.113.9"})
        self.assertEqual(self.sign_in(headers={"X-Real-IP": "203.0.113.9",
                                              "X-Forwarded-For": "198.51.100.7"})[0].status, 429)
        self.assertEqual(self.sign_in(headers={"X-Real-IP": "198.51.100.7"})[0].status, 303)
        # Exhaust the peer bucket; a client-sent XFF must not mint a fresh one.
        for _ in range(ATTEMPTS):
            self.sign_in("wrong")
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "203.0.113.80"})[0].status, 429)

    def test_client_ip_header_is_configurable(self):
        self.cfg.client_ip_header = "CF-Connecting-IP"
        for _ in range(ATTEMPTS):
            self.sign_in("wrong", headers={"CF-Connecting-IP": "203.0.113.9"})
        self.assertEqual(self.sign_in(headers={"CF-Connecting-IP": "203.0.113.9"})[0].status, 429)
        self.assertEqual(self.sign_in(headers={"X-Real-IP": "198.51.100.7"})[0].status, 303)


class BehindProxyXFF(Server):
    behind_proxy = True
    client_ip_header = "X-Forwarded-For"

    def test_rate_limit_is_per_client(self):
        attacker = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
        for _ in range(ATTEMPTS):
            self.sign_in("wrong", headers=attacker)
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.1"})[0].status, 429)
        self.assertEqual(self.sign_in(headers={"X-Forwarded-For": "198.51.100.7"})[0].status, 303)


class BehindProxyNoHeader(Server):
    behind_proxy = True

    def test_forged_client_ip_headers_are_ignored(self):
        for i in range(ATTEMPTS):
            self.sign_in("wrong", headers={"X-Real-IP": f"203.0.113.{i}",
                                           "X-Forwarded-For": f"198.51.100.{i}"})
        self.assertEqual(self.sign_in(headers={"X-Real-IP": "203.0.113.99"})[0].status, 429)


class Deadline(Server):
    def test_slow_headers_time_out(self):
        with mock.patch.object(serve, "REQUEST_SECONDS", 0.4):
            sock = socket.create_connection(("127.0.0.1", self.port), 2)
            self.addCleanup(sock.close)
            sock.sendall(b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: ")
            time.sleep(0.7)
            sock.settimeout(2)
            data = sock.recv(4096)
            self.assertIn(b"408", data)

    def test_slow_body_times_out(self):
        with mock.patch.object(serve, "REQUEST_SECONDS", 0.4):
            sock = socket.create_connection(("127.0.0.1", self.port), 2)
            self.addCleanup(sock.close)
            sock.sendall(b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Type: application/x-www-form-urlencoded\r\n"
                         b"Content-Length: 20\r\n\r\nx")
            time.sleep(0.7)
            sock.settimeout(2)
            data = sock.recv(4096)
            self.assertIn(b"408", data)

    def test_health_and_session_survive_full_unauth(self):
        held = []
        self.addCleanup(lambda: [s.close() for s in held])
        for _ in range(serve.MAX_UNAUTH):
            s = socket.create_connection(("127.0.0.1", self.port), 2)
            s.sendall(b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            held.append(s)
        time.sleep(0.2)
        self.assertEqual(self.request("GET", "/health").status, 200)
        expires = str(int(time.time()) + 3600)
        sig = sign(SECRET, "session:" + expires, HASH, self.cfg.session_version)
        self.assertEqual(self.request("GET", "/", cookies={"botroll_session": f"{expires}.{sig}"}).status, 200)
        self.assertEqual(self.request("GET", "/login").status, 503)

    def test_forged_cookies_cannot_starve_signed_in(self):
        held = []
        self.addCleanup(lambda: [s.close() for s in held])
        for _ in range(serve.MAX_UNAUTH):
            s = socket.create_connection(("127.0.0.1", self.port), 2)
            s.sendall(b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            held.append(s)
        for _ in range(serve.MAX_THREADS - serve.MAX_UNAUTH):
            s = socket.create_connection(("127.0.0.1", self.port), 2)
            s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Cookie: botroll_session=9999999999.forgedsig\r\n")
            held.append(s)
        time.sleep(0.2)
        expires = str(int(time.time()) + 3600)
        sig = sign(SECRET, "session:" + expires, HASH, self.cfg.session_version)
        self.assertEqual(self.request("GET", "/", cookies={"botroll_session": f"{expires}.{sig}"}).status, 200)


class InsecureLocal(Server):
    insecure_local = True

    def test_board_without_sign_in(self):
        r = self.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertNotIn("Sign out", r.body)
        self.assertIn('action="/theme"', r.body)
        self.assertEqual(self.request("GET", "/", headers={"Host": "localhost"}).status, 200)

    def test_only_answers_to_localhost(self):
        for host in ["evil.example", "evil.example:80", "localhost.evil.example", "127.0.0.1.nip.io"]:
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/", headers={"Host": host}).status, 403)


def seed_budget(d, at=None):
    """An hour's prune budget, unused, started just now: a missing one fails closed."""
    n = len(list(pathlib.Path(d).glob("*.md")))
    pathlib.Path(d, ".prune.json").write_text(json.dumps({"start": (time.time() if at is None else at) - 1,
                                                          "bots": n, "deleted": 0}))


class Push(Server):
    def test_needs_the_token(self):
        roll = b"- [doing] T-1 Hello"
        for headers in [{}, {"Authorization": "Bearer " + "x" * 40}, {"Authorization": "Bearer " + TOKEN[:-1]},
                        {"Authorization": TOKEN}, {"Authorization": "Basic " + TOKEN}]:
            with self.subTest(headers=headers):
                r = self.request("POST", "/api/roll/new", roll, headers)
                self.assertEqual((r.status, r.headers["WWW-Authenticate"]), (401, "Bearer"))
        session = self.session()
        self.assertEqual(self.request("POST", "/api/roll/new", roll, cookies=session).status, 401)
        self.assertFalse(os.path.exists(os.path.join(self.data, "new.md")))

    def test_bearer_is_case_insensitive(self):
        r = self.request("POST", "/api/roll/new", b"- [doing] T-1 Hello\n",
                         {"Authorization": f"bearer {TOKEN}"})
        self.assertEqual(r.status, 200)
        r = self.request("POST", "/api/roll/new", b"- [doing] T-1 Hello\n",
                         {"Authorization": f"BEARER {TOKEN}"})
        self.assertEqual(r.status, 200)

    def test_stores_a_roll(self):
        r = self.push("/api/roll/new", "---\nname: Nia\nrole: Ops\n---\n- [doing] T-1 Hello · pid 1\n".encode())
        self.assertEqual((r.status, json.loads(r.body)), (200, {"bot": "new", "items": 1}))
        self.assertIn("Nia", self.request("GET", "/", cookies=self.session()).body)

    def test_rejects_bad_bodies_and_slugs(self):
        for path, body, status in [
            ("/api/roll/Bad", b"", 400), ("/api/roll/..%2F..%2Fetc", b"", 400), ("/api/roll/.lock", b"", 400),
            ("/api/roll/a%00", b"", 400), ("/api/roll/../x", b"", 404), ("/api/roll/a/b", b"", 404),
            ("/api/roll/", b"", 404), ("/api/roll/ok", b"\xff\xfe", 400), ("/api/roll/ok", b"a\0b", 400),
            ("/api/elsewhere", b"", 404), ("/api/watch", b"not json", 400), ("/api/watch", b"{}", 400),
            ("/api/watch", b'[{"kind": "stale", "text": "x", "bot": null, "id": null, "pid": null}]', 400),
            ("/api/watch", b'[{"kind": "cpu", "text": "x", "bot": "../x", "id": null, "pid": null}]', 400),
            ("/api/watch", b'[{"kind": "cpu", "text": "x", "bot": "qa\\n", "id": null, "pid": null}]', 400),
        ]:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.push(path, body).status, status)
        self.assertEqual(self.request("GET", "/api/watch", headers={"Authorization": f"Bearer {TOKEN}"}).status, 404)
        self.assertEqual(sorted(os.listdir(self.data)), sorted(p.name for p in EXAMPLES.glob("*.md")))

    def test_size_limit(self):
        self.assertEqual(self.push("/api/roll/big", b"x", **{"Content-Length": str(MAX_ROLL + 1)}).status, 413)
        try:
            self.assertEqual(self.push("/api/roll/big", b"-" * (MAX_ROLL + 1)).status, 413)
        except (ConnectionResetError, BrokenPipeError):
            pass  # refused unread, so macOS may reset the connection before the 413 is read
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/roll/ok")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()
        self.assertFalse(os.path.exists(os.path.join(self.data, "big.md")))

    def test_a_hostile_roll_still_renders(self):
        roll = "- [doing] T-1 <script>alert(1)</script> · pid: " + "1" * 5000 + "\n- [doing] T-99999999999999999999 x"
        self.assertEqual(self.push("/api/roll/evil", roll.encode()).status, 200)
        r = self.request("GET", "/", cookies=self.session())
        self.assertEqual(r.status, 200)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", r.body)
        self.assertNotIn("<script>alert", r.body)

    def test_findings_reach_the_board(self):
        flag = {"kind": "capture", "text": "ffmpeg (PID 4242) has been capturing for 2h", "bot": None, "id": None,
                "pid": 4242}
        self.assertEqual(json.loads(self.push("/api/watch", json.dumps([flag]).encode()).body), {"findings": 1})
        os.utime(os.path.join(self.data, serve.FINDINGS), (NOW.timestamp() - 120,) * 2)
        body = self.request("GET", "/", cookies=self.session()).body
        self.assertIn("PID 4242", body)
        self.assertIn("Pushed 2m ago", body)
        self.push("/api/roll/qa", (EXAMPLES / "qa.md").read_bytes())
        os.utime(os.path.join(self.data, "qa.md"), (NOW.timestamp(),) * 2)
        self.assertIn("Pushed just now", self.request("GET", "/", cookies=self.session()).body)
        self.assertNotIn('class="banner"', body)

    def test_stale_push_banner(self):
        old = NOW.timestamp() - 3 * 3600
        for f in pathlib.Path(self.data).iterdir():
            os.utime(f, (old, old))
        body = self.request("GET", "/", cookies=self.session()).body
        self.assertIn("Pushed 3h ago", body)
        self.assertIn("Nothing pushed for 3h. This board may be out of date.", body)

    def test_client(self):
        source = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, source)
        pathlib.Path(source, "nova.md").write_text("---\nname: Nova\nrole: Support\n---\n- [doing] T-1 Reply\n")
        with mock.patch.object(serve, "processes", return_value={}):
            self.assertEqual(serve.push(self.url, TOKEN, source), (1, 0, 0))
        self.assertEqual(pathlib.Path(self.data, "nova.md").read_text(), pathlib.Path(source, "nova.md").read_text())
        with self.assertRaisesRegex(ValueError, "401"):
            serve.push(self.url, "x" * 40, source)
        with self.assertRaisesRegex(ValueError, "https"):
            serve.push("http://example.com", TOKEN, source)

    def test_prune_endpoint(self):
        for f in pathlib.Path(self.data).glob("*.md"):
            f.unlink()
        for slug in ("keep", "also", "third", "old", "older"):
            pathlib.Path(self.data, slug + ".md").write_text("- [doing] T-1 x\n")
        r = self.request("POST", "/api/prune", b'{"keep": ["keep"]}', {"Authorization": "Bearer " + "x" * 40})
        self.assertEqual(r.status, 401)
        self.assertEqual(self.request("POST", "/api/prune", b'{"keep": ["keep"]}',
                                      cookies=self.session()).status, 401)
        for body in (b"[]", b'{"keep": []}', b'{"keep": "keep"}', b'{"keep": [1]}', b'{"keep": ["../x"]}', b"nope"):
            with self.subTest(body=body):
                self.assertEqual(self.push("/api/prune", body).status, 400)
        names = ["also.md", "keep.md", "old.md", "older.md", "third.md"]
        seed_budget(self.data)
        self.assertEqual(sorted(p.name for p in pathlib.Path(self.data).glob("*.md")), names)
        r = self.push("/api/prune", b'{"keep": ["keep", "also", "third", "absent"]}')  # dry run by default
        self.assertEqual((r.status, json.loads(r.body)), (200, {"would_remove": ["old", "older"], "dry_run": True}))
        self.assertEqual(sorted(p.name for p in pathlib.Path(self.data).glob("*.md")), names)
        r = self.push("/api/prune", b'{"keep": ["keep", "also", "third", "absent"], "confirm": true}')
        self.assertEqual((r.status, json.loads(r.body)), (200, {"removed": ["old", "older"]}))
        self.assertEqual(sorted(p.name for p in pathlib.Path(self.data).glob("*.md")),
                         ["also.md", "keep.md", "third.md"])

    def test_prune_cap_refuses_mass_delete(self):
        for f in pathlib.Path(self.data).glob("*.md"):
            f.unlink()
        slugs = [f"bot{i}" for i in range(8)]
        for slug in slugs:
            pathlib.Path(self.data, slug + ".md").write_text("- [doing] T-1 x\n")
        seed_budget(self.data)
        # the #6 incident: keep a bot that doesn't exist
        r = self.push("/api/prune", b'{"keep": ["nonexistent"]}')
        self.assertEqual((r.status, len(json.loads(r.body)["would_remove"])), (200, 8))
        for keep in (["nonexistent"], slugs[:4], slugs[:2] + ["x"]):  # 8; 4 > 3; 6 > half
            with self.subTest(keep=keep):
                r = self.push("/api/prune", json.dumps({"keep": keep, "confirm": True}).encode())
                self.assertEqual(r.status, 409)
                self.assertIn("refusing", r.body)
        self.assertEqual(len(list(pathlib.Path(self.data).glob("*.md"))), 8)
        self.assertEqual(self.push("/api/prune", b'{"keep": ["bot0"], "confirm": "yes"}').status, 400)
        two = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, two)
        for slug in ("a", "b"):
            pathlib.Path(two, slug + ".md").write_text("- [doing] T-1 x\n")
        from botroll import prune
        seed_budget(two)
        self.assertEqual(prune(two, ["a"], confirm=True), ["b"])  # 1 of 2 is exactly half: allowed

    def roll_dir(self, n):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for i in range(n):
            pathlib.Path(d, f"bot{i}.md").write_text("- [doing] T-1 x\n")
        return d

    def test_t1_three_bots_keep_one_is_409(self):
        for f in pathlib.Path(self.data).glob("*.md"):
            f.unlink()
        for i in range(3):
            pathlib.Path(self.data, f"bot{i}.md").write_text("- [doing] T-1 x\n")
        seed_budget(self.data)
        r = self.push("/api/prune", b'{"keep": ["bot0"], "confirm": true}')
        self.assertEqual(r.status, 409)
        self.assertIn("half", r.body)
        self.assertEqual(len(list(pathlib.Path(self.data).glob("*.md"))), 3)

    def test_p1_hourly_cap_across_calls(self):
        for f in pathlib.Path(self.data).glob("*.md"):
            f.unlink()
        slugs = [f"bot{i}" for i in range(10)]
        for slug in slugs:
            pathlib.Path(self.data, slug + ".md").write_text("- [doing] T-1 x\n")
        results = []
        seed_budget(self.data)
        for n in (8, 6, 4):  # keep 8 (delete 2), then keep 6 (2 more: 4 > 3), then keep 4
            r = self.push("/api/prune", json.dumps({"keep": slugs[:n], "confirm": True}).encode())
            results.append(r.status)
        self.assertEqual(results, [200, 409, 409])
        self.assertEqual(len(list(pathlib.Path(self.data).glob("*.md"))), 8)
        self.assertIn('"deleted": 2', pathlib.Path(self.data, ".prune.json").read_text())

    def test_p1_budget_persists_resets_hourly_and_keeps_half_and_last(self):
        from botroll import PruneRefused, prune
        t = 1_800_000_000
        big = self.roll_dir(10)
        seed_budget(big, t)
        self.assertEqual(len(prune(big, [f"bot{i}" for i in range(6)], confirm=True, cap=10, at=t)), 4)
        with self.assertRaisesRegex(PruneRefused, "half the 10 bots at the start of the hour"):
            prune(big, [f"bot{i}" for i in range(4)], confirm=True, cap=10, at=t + 60)  # 4 left < 5; 2 of 6 now is fine
        d = self.roll_dir(4)
        seed_budget(d, t)
        self.assertEqual(prune(d, ["bot0", "bot1", "bot2"], confirm=True, at=t), ["bot3"])
        with self.assertRaisesRegex(PruneRefused, "half the 4 bots at the start of the hour"):
            prune(d, ["bot0"], confirm=True, at=t + 60)  # 1 left < 2
        self.assertTrue(pathlib.Path(d, ".prune.json").exists())
        self.assertEqual(prune(d, ["bot0", "bot1"], confirm=True, at=t + 3600), ["bot2"])  # new hour, new budget
        self.assertEqual(prune(d, ["bot0"], confirm=True, at=t + 7300), ["bot1"])
        with self.assertRaisesRegex(PruneRefused, "last bot"):
            prune(d, ["nobody"], confirm=True, at=t + 11000)
        self.assertEqual(prune(d, ["nobody"], at=t + 11000), ["bot0"])  # a dry run still reports it
        self.assertTrue(pathlib.Path(d, "bot0.md").exists())  # a dry run still reports it

    def test_budget_fails_closed(self):
        from botroll import PruneRefused, prune
        t = 1_800_000_000
        for label, content in [("missing", None), ("corrupt", "{nope"), ("wrong shape", '{"start": "x"}'),
                               ("future-dated", json.dumps({"start": t + 600, "bots": 6, "deleted": 0}))]:
            with self.subTest(label):
                d = self.roll_dir(6)
                if content is not None:
                    pathlib.Path(d, ".prune.json").write_text(content)
                with self.assertRaisesRegex(PruneRefused, "at most 3 deletions an hour \\(3 already\\)"):
                    prune(d, [f"bot{i}" for i in range(5)], confirm=True, at=t)
                self.assertEqual(len(list(pathlib.Path(d).glob("*.md"))), 6)
                fresh = json.loads(pathlib.Path(d, ".prune.json").read_text())
                self.assertEqual(fresh, {"start": t, "bots": 6, "deleted": 3})  # a fresh, used-up hour
                with self.assertRaises(PruneRefused):
                    prune(d, [f"bot{i}" for i in range(5)], confirm=True, at=t + 3599)
                self.assertEqual(prune(d, [f"bot{i}" for i in range(5)], confirm=True, at=t + 3600), ["bot5"])
        d = self.roll_dir(6)  # the clock went backwards past the hour's start
        seed_budget(d, t)
        with self.assertRaises(PruneRefused):
            prune(d, [f"bot{i}" for i in range(5)], confirm=True, at=t - 7200)

    def test_budget_saved_before_deleting(self):
        from botroll import prune
        d = self.roll_dir(6)
        seed_budget(d)
        with mock.patch("botroll.write_atomic", side_effect=OSError(f"[Errno 28] No space: {d}/.prune.json")):
            with self.assertRaises(ValueError) as c:
                prune(d, [f"bot{i}" for i in range(5)], confirm=True)
        self.assertNotIn(d, str(c.exception))
        self.assertIn("nothing was deleted", str(c.exception))
        self.assertEqual(len(list(pathlib.Path(d).glob("*.md"))), 6)

    def test_prune_400_has_no_absolute_path(self):
        for i in range(3):
            pathlib.Path(self.data, f"x{i}.md").write_text("- [doing] T-1 x\n")
        seed_budget(self.data)
        keep = [p.stem for p in pathlib.Path(self.data).glob("*.md") if p.stem != "x0"]
        with mock.patch("botroll.write_atomic", side_effect=OSError(f"denied: {self.data}/.prune.json")):
            r = self.push("/api/prune", json.dumps({"keep": keep, "confirm": True}).encode())
        self.assertEqual(r.status, 400)
        self.assertNotIn(self.data, r.body)
        self.assertNotIn("/tmp", r.body)
        self.assertTrue(pathlib.Path(self.data, "x0.md").exists())

    def test_client_prune(self):
        source = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, source)
        pathlib.Path(source, "nova.md").write_text("- [doing] T-1 Reply\n")
        for f in pathlib.Path(self.data).glob("*.md"):
            f.unlink()
        pathlib.Path(self.data, "stale.md").write_text("- [doing] T-1 Old\n")
        seed_budget(self.data)
        removed = []
        with mock.patch.object(serve, "processes", return_value={}):
            serve.push(self.url, TOKEN, source)
            self.assertTrue(pathlib.Path(self.data, "stale.md").exists())  # no prune by default
            self.assertEqual(serve.push(self.url, TOKEN, source, prune=True, removed=removed), (1, 0, 0))
            self.assertEqual(removed, ["stale"])
            self.assertTrue(pathlib.Path(self.data, "stale.md").exists())  # --prune alone is a dry run
            removed.clear()
            serve.push(self.url, TOKEN, source, prune=True, removed=removed, confirm=True)
        self.assertEqual(removed, ["stale"])
        self.assertFalse(pathlib.Path(self.data, "stale.md").exists())
        self.assertTrue(pathlib.Path(self.data, "nova.md").exists())
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        with mock.patch.object(serve, "processes", return_value={}), self.assertRaisesRegex(ValueError, "prune"):
            serve.push(self.url, TOKEN, empty, prune=True)
        self.assertTrue(pathlib.Path(self.data, "nova.md").exists())

    def test_client_never_follows_redirects(self):
        seen = []

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302 if self.path.startswith("/api") else 200)
                self.send_header("Location", "/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()
            do_GET = do_POST

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        source = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, source)
        pathlib.Path(source, "nova.md").write_text("- [doing] T-1 Reply\n")
        with self.assertRaisesRegex(ValueError, "302"):
            serve.push(f"http://127.0.0.1:{server.server_address[1]}", TOKEN, source)
        self.assertEqual([path for path, _ in seen], ["/api/roll/nova"])

    def test_skips_an_oversize_roll_and_pushes_the_rest(self):
        source = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, source)
        pathlib.Path(source, "ok.md").write_text("- [doing] T-1 Hello\n")
        pathlib.Path(source, "huge.md").write_bytes(b"x" * (MAX_ROLL + 1))
        with mock.patch.object(serve, "processes", return_value={}), \
                mock.patch("sys.stderr", new=self._stderr()) as err:
            self.assertEqual(serve.push(self.url, TOKEN, source), (1, 0, 1))
        self.assertIn("skipping huge", err.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.data, "ok.md")))
        self.assertFalse(os.path.exists(os.path.join(self.data, "huge.md")))

    def _stderr(self):
        import io
        return io.StringIO()

    def test_long_slug_and_deep_json_are_400(self):
        self.assertEqual(self.push("/api/roll/" + "a" * 65, b"- [doing] T-1 x").status, 400)
        deep = b"[" * 20 + b"1" + b"]" * 20
        self.assertEqual(self.push("/api/watch", deep).status, 400)
        self.assertEqual(load_json(b"[1, 2, {\"a\": true}]", 3), [1, 2, {"a": True}])
        with self.assertRaises(ValueError):
            load_json(b"[" * (MAX_JSON_DEPTH + 2) + b"0" + b"]" * (MAX_JSON_DEPTH + 2))


class PushOff(Server):
    token = None

    def test_api_is_off_without_a_token(self):
        self.assertEqual(self.push("/api/roll/x", b"").status, 404)
        self.assertEqual(self.request("POST", "/api/roll/x", b"", {"Authorization": "Bearer "}).status, 404)


class Revoke(Server):
    def test_password_change_revokes_sessions(self):
        cookies = self.session()
        self.assertEqual(self.request("GET", "/", cookies=cookies).status, 200)
        self.cfg.auth_id = "changed-hash"
        self.assertEqual(self.request("GET", "/", cookies=cookies).status, 303)

    def test_csp_hashes_only_the_known_blocks(self):
        r = self.request("GET", "/", cookies=self.session())
        policy = r.headers["Content-Security-Policy"]
        self.assertIn(STYLE_HASH, policy)
        self.assertIn(SCRIPT_HASH, policy)
        injected = hashlib.sha256(b"alert(1)").digest()
        self.assertNotIn(base64.b64encode(injected).decode(), policy)
        self.assertEqual(serve.csp("<script>alert(1)</script><style>body{color:red}</style>"),
                         serve.csp("<main></main>"))


class InsecureHttp(Server):
    def setUp(self):
        super().setUp()
        self.cfg.insecure_http = True

    def test_cookie_is_not_secure(self):
        r, _ = self.sign_in()
        cookie = next(c for c in r.headers.get_all("Set-Cookie") if c.startswith("botroll_session="))
        self.assertNotIn("Secure", cookie)


class DataDir(unittest.TestCase):
    def test_refuses_an_unwritable_data_dir(self):
        data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data, ignore_errors=True)
        os.chmod(data, 0o555)
        self.addCleanup(os.chmod, data, 0o755)
        if os.access(data, os.W_OK):
            self.skipTest("running as root; chmod 555 is still writable")
        with self.assertRaisesRegex(ValueError, "isn't writable"):
            serve.ensure_data(data)


class HashPassword(unittest.TestCase):
    def test_cli_rejects_a_short_password(self):
        from botroll.cli import main
        with mock.patch("getpass.getpass", return_value="short"), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stderr"):
            self.assertEqual(main(["hash-password"]), 2)

    def test_cli_rejects_a_common_or_blank_password(self):
        from botroll.cli import main
        for pw in ("password1234", " " * 12, "p" * 12):
            with mock.patch("getpass.getpass", return_value=pw), \
                    mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("sys.stderr"):
                self.assertEqual(main(["hash-password"]), 2)


if __name__ == "__main__":
    unittest.main()
