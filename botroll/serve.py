"""botroll serve: the live board for its one owner, and the push API that feeds it from the bots' machine.

Standard library only. Every route but /health needs a signed session cookie, or for /api/* the push
token. It refuses to start without a password and a secret unless --insecure-local is given.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.client
import json
import math
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import actions, health, notify
from . import SLUG, PruneRefused, __version__, bots, commit, items, local, locked, now, path, prune, read, store, version_string, write_atomic
from .board import BOARD_CSS, BOARD_SCRIPT, THEMES, document, page, snapshot, theme_toggle
from .watch import check, processes

SESSION, CSRF, THEME = "botroll_session", "botroll_csrf", "botroll_theme"
SHORT, LONG = 12 * 3600, 30 * 86400  # session lifetime in seconds: default, and "keep me signed in"
ATTEMPTS, WINDOW = 5, 15 * 60  # failed sign-ins allowed per IP per window
MAX_ROLL, MAX_FORM = 256 * 1024, 4096  # request body limits in bytes
HEALTH_PER_MINUTE, HEALTH_BAD_PER_MINUTE = 6, 30
MAX_JSON_DEPTH = 8  # findings JSON; deeper nesting is refused as 400 rather than RecursionError
MIN_PASSWORD, MIN_DISTINCT = 12, 5
REQUEST_SECONDS, MAX_THREADS, MAX_UNAUTH = 20, 32, 24
READ_CHUNK = 2048  # body and header reads; small so a deadline can fire mid-body
UID = 1000  # the container user; serve() drops to it if it started as root
COMMON_PASSWORDS = frozenset({
    "password1234", "password12345", "password123456", "123456789012", "qwertyuiop12",
    "letmein12345", "admin1234567", "welcome12345", "changeme1234", "iloveyou1234",
})
SESSION_FILE = ".session"  # integer version mixed into the cookie key; bumped on logout
FINDINGS = ".watch.json"  # the last pushed process flags, next to the roll files
PROCESS_KINDS = ("pid", "cpu", "capture")  # flags only the bots' machine can see
SECURITY = [("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex, nofollow"), ("X-Frame-Options", "DENY"),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
            # The board uses none of these browser features; deny them to the page and anything it might embed.
            ("Permissions-Policy", "accelerometer=(), browsing-topics=(), camera=(), display-capture=(), "
                                   "geolocation=(), gyroscope=(), magnetometer=(), microphone=(), midi=(), "
                                   "payment=(), publickey-credentials-get=(), serial=(), usb=(), xr-spatial-tracking=()")]


def _sha256_b64(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


STYLE_HASH, SCRIPT_HASH = _sha256_b64(BOARD_CSS), _sha256_b64(BOARD_SCRIPT)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _same(a: str, b: str) -> bool:
    """Constant-time; unequal lengths compare as false instead of raising."""
    left, right = a.encode(), b.encode()
    n = max(len(left), len(right), 1)
    return hmac.compare_digest(left.ljust(n), right.ljust(n)) and len(left) == len(right)


# Passwords and configuration -------------------------------------------------

def require_password(password: str, secret: str = "") -> None:
    """Refuse empty, whitespace-only, short, common, or secret-equal passwords."""
    if not password or not password.strip():
        raise ValueError("password cannot be empty or only whitespace")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if password.lower() in COMMON_PASSWORDS or password.strip().lower() in COMMON_PASSWORDS:
        raise ValueError("password is too common")
    if len(set(password)) < MIN_DISTINCT:
        raise ValueError(f"password needs at least {MIN_DISTINCT} different characters")
    if secret and password == secret:
        raise ValueError("password must be different from BOTROLL_SECRET")


def hash_password(password: str, n: int = 2**14, r: int = 8, p: int = 1) -> str:
    """An scrypt hash for BOTROLL_PASSWORD_HASH: scrypt$n$r$p$salt$key."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(key)}"


def check_password(password: str, stored: str) -> bool:
    """Constant-time check. Raises ValueError if stored isn't a hash from hash_password."""
    scheme, n, r, p, salt, key = stored.split("$")
    if scheme != "scrypt":
        raise ValueError(scheme)
    want = _unb64(key)
    got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(want))
    return hmac.compare_digest(got, want)


@dataclass
class Config:
    data: str
    password_hash: str | None
    secret: bytes
    push_token: str | None
    insecure_local: bool = False
    behind_proxy: bool = False
    insecure_http: bool = False
    client_ip_header: str = ""  # empty: use the socket peer; never implied by --behind-proxy
    auth_id: str = ""
    session_version: int = 0
    mailer: notify.Mailer | None = None
    actions: dict = field(default_factory=dict)
    board_url: str | None = None
    health: bool = False  # POST /api/health accepts samples only when BOTROLL_HEALTH is set
    health_control: bool = False  # owner Close/Stop; needs BOTROLL_HEALTH_CONTROL=1
    health_token: str | None = None  # BOTROLL_HEALTH_TOKEN: when set, the only token for /api/health*
    host_cookies: bool = False  # BOTROLL_HOST_COOKIES=1: cookie names get the __Host- prefix


HEALTH_ROUTE = re.compile(r"/api/health(/.*)?")


def cookie_name(cfg: Config, name: str) -> str:
    """The cookie's name on the wire: with BOTROLL_HOST_COOKIES the browser enforces __Host- (Secure, Path=/, no
    Domain), so a sibling subdomain or a plain-http page can't set or shadow it."""
    return "__Host-" + name if cfg.host_cookies else name


def token_for(cfg: Config, path_: str) -> str | None:
    """The one Bearer token an /api/ route takes: BOTROLL_HEALTH_TOKEN for /api/health and its sub-routes when it
    is set (the push token is then refused there, and it is refused everywhere else), else BOTROLL_PUSH_TOKEN."""
    if cfg.health_token and HEALTH_ROUTE.fullmatch(path_):
        return cfg.health_token
    return cfg.push_token


def session_version(data: str) -> int:
    try:
        return int(open(os.path.join(data, SESSION_FILE), encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return 0


def bump_session(data: str) -> int:
    """Invalidate every session cookie. Called on logout (the one owner, so this is logout-everywhere)."""
    version = session_version(data) + 1
    os.makedirs(data, exist_ok=True)
    write_atomic(os.path.join(data, SESSION_FILE), str(version))
    return version


def configure(data: str, env=os.environ, insecure_local: bool = False, behind_proxy: bool = False,
              insecure_http: bool = False, client_ip_header: str | None = None) -> Config:
    """Read the auth settings, failing closed: a ValueError instead of an open board."""
    hashed = env.get("BOTROLL_PASSWORD_HASH") or None
    password = env.get("BOTROLL_PASSWORD") or ""
    secret, token = env.get("BOTROLL_SECRET", ""), env.get("BOTROLL_PUSH_TOKEN") or None
    if password:
        require_password(password, secret)
    if not hashed and password:
        hashed = hash_password(password)
        auth_id = hashlib.sha256(b"botroll-pw:" + password.encode()).hexdigest()
    else:
        auth_id = hashed or ""
    if not insecure_local:
        if not hashed:
            raise ValueError("set BOTROLL_PASSWORD_HASH (from `botroll hash-password`) or BOTROLL_PASSWORD, "
                             "or pass --insecure-local to serve on localhost without sign-in")
        if len(secret) < 32:
            raise ValueError("set BOTROLL_SECRET to a random string of at least 32 characters, "
                             "or pass --insecure-local to serve on localhost without sign-in")
    if hashed:
        try:
            check_password("", hashed)
        except (ValueError, TypeError):
            raise ValueError("BOTROLL_PASSWORD_HASH isn't a hash from `botroll hash-password`") from None
    if token is not None and len(token) < 32:
        raise ValueError("BOTROLL_PUSH_TOKEN must be at least 32 characters")
    if token and (token == secret or (password and token == password)):
        raise ValueError("BOTROLL_PUSH_TOKEN must be different from the password and BOTROLL_SECRET")
    health_token = env.get("BOTROLL_HEALTH_TOKEN") or None
    if health_token is not None:
        if len(health_token) < 32:
            raise ValueError("BOTROLL_HEALTH_TOKEN must be at least 32 characters")
        if health_token in (secret, token) or (password and health_token == password):
            raise ValueError("BOTROLL_HEALTH_TOKEN must be different from the password, BOTROLL_SECRET "
                             "and BOTROLL_PUSH_TOKEN")
    host_cookies = _on(env.get("BOTROLL_HOST_COOKIES"))
    if host_cookies and (insecure_local or insecure_http):
        raise ValueError("BOTROLL_HOST_COOKIES needs Secure cookies: not with --insecure-local or --insecure-http")
    if client_ip_header is not None:
        header = client_ip_header.strip()
    else:
        header = (env.get("BOTROLL_CLIENT_IP_HEADER") or "").strip()
    board_url = (env.get("BOTROLL_URL") or "").strip().rstrip("/") or None
    return Config(data, hashed, secret.encode(), token, insecure_local, behind_proxy, insecure_http, header,
                  auth_id, session_version(data), notify.configure(env),
                  actions.load(data, env), board_url,
                  _on(env.get("BOTROLL_HEALTH")), _on(env.get("BOTROLL_HEALTH_CONTROL")), health_token, host_cookies)


def _on(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# Pages -------------------------------------------------------------------------

def csp(_body: str = "") -> str:
    """A Content-Security-Policy that allows only the known board CSS and the one collapse script.

    Hashes are of those constants, not of whatever happens to be in the response, so an escaping
    bug that injected a <script> would not whitelist itself.
    """
    return (f"default-src 'none'; style-src {STYLE_HASH}; script-src {SCRIPT_HASH}; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def load_json(raw: bytes, depth: int = MAX_JSON_DEPTH):
    """json.loads that refuses deep nesting with ValueError instead of RecursionError."""
    try:
        value = json.loads(raw)
    except RecursionError:
        raise ValueError("JSON is too deeply nested") from None
    except ValueError as e:
        raise ValueError(str(e)) from None

    def walk(obj, n: int) -> None:
        if n > depth:
            raise ValueError("JSON is too deeply nested")
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v, n + 1)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, n + 1)
    walk(value, 0)
    return value


def login_page(csrf: str, error: str = "", theme: str | None = None) -> str:
    e = html.escape
    return document("Sign in · BotRoll", f"""<main>
<header><span class="brand">BotRoll</span><div class="bar">{theme_toggle(csrf, theme)}</div></header>
<form class="signin" method="post" action="/login">
<h1>Sign in to see your bots.</h1>
<p class="lede">The roll call for your bots: what they're doing and what they need from you.</p>
{f'<p class="error" role="alert">{e(error)}</p>' if error else ""}
<label class="sr" for="password">Password</label>
<input class="field" id="password" name="password" type="password" placeholder="Password" autocomplete="current-password" required autofocus>
<label class="keep"><input type="checkbox" name="remember" value="1"> Keep me signed in for 30 days</label>
<input type="hidden" name="csrf" value="{e(csrf)}">
<button class="btn primary" type="submit">Sign in</button>
</form>
</main>""", theme=theme)


def served_snapshot(data: str) -> dict:
    """The board from pushed files. The host's own processes say nothing about the bots, so procs is {}."""
    try:
        with open(os.path.join(data, FINDINGS), encoding="utf-8") as f:
            flags = json.load(f)
    except (OSError, ValueError):
        flags = []
    times = [os.path.getmtime(os.path.join(data, n)) for n in os.listdir(data)
             if n.endswith(".md") or n == FINDINGS] if os.path.isdir(data) else []
    pushed = local(max(times)) if times else None
    return snapshot(data, procs={}, pushed_flags=flags, pushed=pushed)


def store_findings(data: str, findings) -> int:
    def valid(f) -> bool:
        return (isinstance(f, dict) and set(f) == {"kind", "text", "bot", "id", "pid"}
                and f["kind"] in PROCESS_KINDS and isinstance(f["text"], str) and len(f["text"]) <= 500
                and (f["bot"] is None or isinstance(f["bot"], str) and bool(SLUG.fullmatch(f["bot"])))
                and (f["id"] is None or isinstance(f["id"], str) and bool(re.fullmatch(r"T-\d+", f["id"])))
                and (f["pid"] is None or type(f["pid"]) is int))
    if not isinstance(findings, list) or not all(valid(f) for f in findings):
        raise ValueError("findings must be a list of process flags from botroll watch")
    with locked(data):
        write_atomic(os.path.join(data, FINDINGS), json.dumps(findings))
    return len(findings)


# The server --------------------------------------------------------------------

class _DeadlineFile:
    """A socket reader that never blocks past an absolute monotonic deadline.

    http.server's rfile.read(n) waits until n bytes arrive, and each recv resets
    Handler.timeout, so a 1-byte/5s drip never hits a 20s body check. Headers are
    the same. This reads small chunks and settimeouts to the time still left.
    """

    def __init__(self, sock, deadline: float, chunk: int = READ_CHUNK):
        self._sock, self._deadline, self._chunk = sock, deadline, chunk
        self._buf = bytearray()
        self.closed = False

    def _left(self) -> float:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request deadline")
        return left

    def _recv(self, n: int) -> bytes:
        self._sock.settimeout(self._left())
        try:
            data = self._sock.recv(min(max(n, 1), self._chunk))
        except TimeoutError:
            raise TimeoutError("request deadline") from None
        except OSError as e:
            if "timed out" in str(e).lower():
                raise TimeoutError("request deadline") from None
            raise
        return data or b""

    def read(self, n: int = -1) -> bytes:
        if n == 0:
            return b""
        if n < 0:
            out = bytes(self._buf)
            self._buf.clear()
            while True:
                chunk = self._recv(self._chunk)
                if not chunk:
                    return out
                out += chunk
        while len(self._buf) < n:
            chunk = self._recv(n - len(self._buf))
            if not chunk:
                break
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def readline(self, limit: int = -1) -> bytes:
        cap = 65537 if limit < 0 else limit
        while b"\n" not in self._buf and len(self._buf) < cap:
            chunk = self._recv(min(self._chunk, cap - len(self._buf)))
            if not chunk:
                break
            self._buf.extend(chunk)
        nl = self._buf.find(b"\n") + 1
        n = min(cap, nl if nl else len(self._buf))
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self) -> None:
        self.closed = True

    def flush(self) -> None:
        pass


def _sign(cfg: Config, text: str) -> str:
    material = f"{cfg.auth_id}:{cfg.session_version}".encode()
    key = hmac.new(cfg.secret, material, hashlib.sha256).digest()
    return _b64(hmac.new(key, text.encode(), hashlib.sha256).digest())


def _session_ok(cfg: Config, value: str) -> bool:
    """True if value is a live session cookie. Signature check is constant-time."""
    expires, _, sig = value.partition(".")
    want = _sign(cfg, "session:" + (expires if expires.isdigit() else "0"))
    match = _same(sig, want)
    return bool(expires.isdigit() and match and int(expires) > time.time())


def _cookie_value(header: str, name: str) -> str:
    try:
        jar = SimpleCookie(header)
    except CookieError:
        return ""
    return jar[name].value if name in jar else ""


def _raw_headers(block: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in block.split(b"\r\n"):
        name, sep, value = raw.partition(b":")
        if sep:
            fields[name.decode("latin-1").lower()] = value.decode("latin-1").strip()
    return fields


def _peek_kind(request, cfg: Config | None = None) -> str:
    """Classify a new connection from a short non-consuming peek: health, auth, or unknown.

    Reserved slots stay free for /health and *verified* sessions or Bearer tokens.
    A forged botroll_session or Bearer counts as unauth. A 50ms peek that sees nothing
    is unknown.
    """
    try:
        prev = request.gettimeout()
    except OSError:
        prev = None
    try:
        request.settimeout(0.05)
        data = request.recv(4096, socket.MSG_PEEK)
    except OSError:
        return "unknown"
    finally:
        try:
            request.settimeout(prev)
        except OSError:
            pass
    head = data.split(b"\r\n\r\n", 1)[0]
    line, _, headers = head.partition(b"\r\n")
    parts = line.split(None, 2)
    path = parts[1].split(b"?", 1)[0] if len(parts) >= 2 else b""
    if len(parts) >= 1 and parts[0] in (b"GET", b"HEAD") and path == b"/health":
        return "health"
    if cfg is None:
        return "unknown"
    if cfg.insecure_local:
        return "auth"
    fields = _raw_headers(headers)
    if _session_ok(cfg, _cookie_value(fields.get("cookie", ""), cookie_name(cfg, SESSION))):
        return "auth"
    route = path.decode("latin-1")
    want = token_for(cfg, route) if route.startswith("/api/") else None
    if want and _bearer(fields.get("authorization", ""), want):
        return "auth"
    return "unknown"


def _bearer(auth: str, token: str) -> bool:
    """True if Authorization is Bearer <token>, scheme matched case-insensitively."""
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return _same(parts[1], token)


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    failures: dict[str, list[float]]
    lock: threading.Lock
    hashing: threading.BoundedSemaphore
    timeout = REQUEST_SECONDS
    _replied = False

    def version_string(self) -> str:
        return "botroll"

    def setup(self) -> None:
        """Replace makefile rfile with a deadline reader so headers share the body clock."""
        self.connection = self.request
        self.deadline = time.monotonic() + REQUEST_SECONDS
        try:
            self.connection.settimeout(self.deadline - time.monotonic())
        except OSError:
            pass
        self.rfile = _DeadlineFile(self.connection, self.deadline)
        self.wfile = self.connection.makefile("wb", buffering=0)

    def handle_one_request(self) -> None:
        self._replied = False
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = self.request_version = self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            name = "do_" + self.command
            if not hasattr(self, name):
                self.send_error(501, f"Unsupported method ({self.command!r})")
                return
            getattr(self, name)()
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True
            if not self._replied:
                try:
                    self.reply(408, "Request timed out\n", "text/plain")
                except OSError:
                    pass

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_HEAD(self) -> None:
        if urlsplit(self.path).path == "/health":
            return self.health_ok(head=True)
        self.reply(501, "Unsupported method\n", "text/plain", head=True)

    def route(self, method: str) -> None:
        path_ = urlsplit(self.path).path
        local = re.fullmatch(r"(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?", self.headers.get("Host", ""))
        if self.cfg.insecure_local and not local:
            return self.reply(403, "--insecure-local only answers to localhost\n", "text/plain")  # DNS rebinding
        if path_ == "/health":
            return self.health_ok()
        if path_.startswith("/api/"):
            return self.api(method, path_)
        if path_ == "/theme" and method == "POST":
            return self.set_theme()
        if path_ == "/login":
            if self.signed_in():
                return self.redirect("/")
            return self.login() if method == "POST" else self.show_login()
        if path_ == "/action":
            return self.act() if method == "POST" else self.reply(
                401 if not self.signed_in() else 405, "Sign in required\n" if not self.signed_in() else "POST only\n",
                "text/plain")
        if not self.signed_in():
            return self.redirect("/login")
        if path_ == "/" and method == "GET":
            csrf, headers = self.csrf_token()
            snap = served_snapshot(self.cfg.data)
            if not self.cfg.health:
                snap.pop("health", None)  # box health off: no panel from samples stored before it was turned off
            return self.reply(200, page(snap, live=True, csrf=csrf, theme=self.theme(),
                                        signout=not self.cfg.insecure_local,
                                        endpoints=self.cfg.actions,
                                        results=actions.results(self.cfg.data, snap),
                                        sync=actions.sync_result(self.cfg.data),
                                        health_control=self.cfg.health and self.cfg.health_control,
                                        health_results=(health.recent_results(self.cfg.data)
                                                        if self.cfg.health_control else None)),
                              headers=headers)
        if path_ == "/health/control":
            return self.health_control() if method == "POST" else self.reply(
                401 if not self.signed_in() else 405,
                "Sign in required\n" if not self.signed_in() else "POST only\n", "text/plain")
        if path_ == "/logout" and method == "POST":
            return self.logout()
        self.reply(404, "Not found\n", "text/plain")

    # Responses

    def reply(self, status: int, body: str, ctype: str = "text/html; charset=utf-8", headers=(),
              head: bool = False) -> None:
        if self._replied:
            return
        self._replied = True
        raw = body.encode()
        self.send_response(status)
        for key, value in [("Content-Type", ctype), ("Content-Length", str(len(raw))), *SECURITY,
                           ("Content-Security-Policy", csp()), *headers]:
            self.send_header(key, value)
        headers_read = hasattr(self, "headers")  # not when the request line itself was refused
        if self.cfg.behind_proxy and headers_read and self.headers.get("X-Forwarded-Proto") == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.end_headers()
        if not head:
            self.wfile.write(raw)

    def health_ok(self, head: bool = False) -> None:
        """/health: 200 with "ok" first (what health checks look for), then the version and commit that are running,
        so a deploy can be checked from outside. Also as the X-BotRoll-Version header (0.1.0+<commit>)."""
        self.reply(200, f"ok\nversion: {__version__}\ncommit: {commit()}\n", "text/plain",
                   [("X-BotRoll-Version", version_string())], head=head)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        self.reply(code, f"{code} {message or ''}\n", "text/plain")

    def redirect(self, location: str, headers=()) -> None:
        self.reply(303, "", "text/plain", [("Location", location), *headers])

    def json(self, status: int, body: dict, headers=()) -> None:
        self.reply(status, json.dumps(body) + "\n", "application/json", headers)

    def body(self, limit: int) -> bytes | None:
        """The request body, or None after replying 411 or 413."""
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,9}", length):
            return self.reply(411, "Content-Length required\n", "text/plain")
        n = int(length)
        if n > limit:
            self.close_connection = True
            return self.reply(413, f"Body over {limit} bytes\n", "text/plain")
        chunks, left = [], n
        try:
            while left:
                if time.monotonic() > getattr(self, "deadline", time.monotonic() + REQUEST_SECONDS):
                    self.close_connection = True
                    return self.reply(408, "Request timed out\n", "text/plain")
                chunk = self.rfile.read(min(left, READ_CHUNK))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
        except TimeoutError:
            self.close_connection = True
            return self.reply(408, "Request timed out\n", "text/plain")
        return b"".join(chunks)

    # Cookies, sessions and CSRF

    def sign(self, text: str) -> str:
        return _sign(self.cfg, text)

    def cookie(self, name: str) -> str:
        return _cookie_value(self.headers.get("Cookie", ""), cookie_name(self.cfg, name))

    def set_cookie(self, name: str, value: str, max_age: int | None = None) -> tuple[str, str]:
        parts = [f"{cookie_name(self.cfg, name)}={value}", "Path=/", "HttpOnly", "SameSite=Lax"]
        parts += [] if self.cfg.insecure_local or self.cfg.insecure_http else ["Secure"]
        parts += [] if max_age is None else [f"Max-Age={max_age}"]
        return "Set-Cookie", "; ".join(parts)

    def signed_in(self) -> bool:
        if self.cfg.insecure_local:
            return True
        return _session_ok(self.cfg, self.cookie(SESSION))

    def csrf_token(self) -> tuple[str, list]:
        """The token for this browser's forms, and a Set-Cookie header if it has no CSRF cookie yet."""
        seed, headers = self.cookie(CSRF), []
        if not re.fullmatch(r"[\w-]{43}", seed):
            seed = secrets.token_urlsafe(32)
            headers = [self.set_cookie(CSRF, seed)]
        return self.sign("csrf:" + seed), headers

    def csrf_ok(self, form: dict) -> bool:
        seed = self.cookie(CSRF)
        return bool(seed) and _same(form.get("csrf", ""), self.sign("csrf:" + seed))

    def theme(self) -> str | None:
        """The owner's choice of "light" or "dark", or None to follow the device."""
        chosen = self.cookie(THEME)
        return chosen if chosen in ("light", "dark") else None

    def set_theme(self) -> None:
        form = self.form()
        if form is None:
            return
        if not self.csrf_ok(form) or form.get("theme") not in THEMES:
            return self.reply(403, "Forbidden\n", "text/plain")
        chosen = form["theme"]
        self.redirect("/", [self.set_cookie(THEME, chosen, LONG * 12 if chosen != "system" else 0)])

    def form(self) -> dict | None:
        raw = self.body(MAX_FORM)
        return None if raw is None else {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

    # Sign-in

    def client_ip(self) -> str:
        """The client's address for rate limiting.

        The socket peer is used unless --behind-proxy is set and a header was opted
        into with --client-ip-header or BOTROLL_CLIENT_IP_HEADER. --behind-proxy
        alone only reads X-Forwarded-Proto for HSTS. A missing or empty opt-in
        must not fall back to X-Real-IP or X-Forwarded-For: those are forgeable
        when the image is exposed directly.
        """
        header = self.cfg.client_ip_header
        if self.cfg.behind_proxy and header:
            named = self.headers.get(header, "").split(",")[0].strip()
            if named:
                return named
        return self.client_address[0]

    def attempt(self, ip: str) -> float:
        """Count a sign-in attempt from ip and return 0, or return the seconds until it may try again.

        The attempt is counted before the password is checked, so parallel guesses can't outrun the limit.
        """
        at = time.time()
        with self.lock:
            for key in list(self.failures):
                self.failures[key] = [t for t in self.failures[key] if at - t < WINDOW]
                if not self.failures[key]:
                    del self.failures[key]
            recent = self.failures.setdefault(ip, [])
            if len(recent) >= ATTEMPTS:
                return WINDOW - (at - recent[0])
            recent.append(at)
            return 0

    def show_login(self, error: str = "", status: int = 200, headers=()) -> None:
        csrf, cookie = self.csrf_token()
        self.reply(status, login_page(csrf, error, self.theme()), headers=[*cookie, *headers])

    def login(self) -> None:
        form = self.form()
        if form is None:
            return
        if not self.csrf_ok(form):
            return self.show_login("That sign-in form had expired. Try again.", 403)
        ip = self.client_ip()
        wait = self.attempt(ip)
        if wait:
            minutes = math.ceil(wait / 60)
            return self.show_login(f"Too many attempts. Try again in {minutes} minute{'s' * (minutes != 1)}.",
                                   429, [("Retry-After", str(math.ceil(wait)))])
        with self.hashing:
            right = check_password(form.get("password", ""), self.cfg.password_hash)
        if not right:
            return self.show_login("That password isn't right.", 401)
        with self.lock:
            self.failures.pop(ip, None)
        remember = bool(form.get("remember"))
        expires = str(int(time.time()) + (LONG if remember else SHORT))
        session = self.set_cookie(SESSION, f"{expires}.{self.sign('session:' + expires)}", LONG if remember else None)
        self.redirect("/", [session])

    def act(self) -> None:
        """POST /action: the owner clicked. CSRF + session required; the board never changes item state."""
        if not self.signed_in():
            return self.reply(401, "Sign in required\n", "text/plain")
        form = self.form()
        if form is None:
            return
        if not self.csrf_ok(form):
            return self.reply(403, "Forbidden\n", "text/plain")
        if not self.cfg.actions:
            return self.reply(404, "Not found\n", "text/plain")
        bot, item_id = form.get("bot", ""), form.get("item", "")
        action, rid = form.get("action", ""), form.get("rid", "")
        if action == actions.SYNC:
            return self.sync(rid)
        note = (form.get("note") or "")[:actions.MAX_NOTE]
        if not SLUG.fullmatch(bot) or not re.fullmatch(r"T-\d+", item_id) or action not in actions.KINDS:
            return self.reply(400, "Bad action\n", "text/plain")
        try:
            uuid.UUID(rid)
        except ValueError:
            return self.reply(400, "Bad action\n", "text/plain")
        ep = self.cfg.actions.get(bot)
        if not ep:
            return self.reply(404, "Not found\n", "text/plain")
        snap = served_snapshot(self.cfg.data)
        item = next((i for b in snap["bots"] if b["bot"] == bot for i in b["items"] if i["id"] == item_id), None)
        if not item or not actions.allowed(action, item["state"]):
            return self.reply(400, "Bad action\n", "text/plain")
        if action == "reply" and not note.strip():
            return self.reply(400, "Bad action\n", "text/plain")
        ts = int(time.time())
        body = json.dumps(actions.payload(action, bot, item, note, self.cfg.board_url, ts, rid),
                          separators=(",", ":"), ensure_ascii=False).encode()
        outcome = actions.claim(self.cfg.data, {"id": rid, "bot": bot, "item_id": item_id,
                                                "action": action, "note": note, "sent_at": ts,
                                                "actor": "owner"})
        if outcome == "dup":
            return self.redirect("/")
        if outcome == "rate":
            return self.reply(429, "Too many actions on this item\n", "text/plain",
                              [("Retry-After", str(actions.GAP))])
        status = 0
        try:
            status = actions.deliver(ep, body, ts)
        except (ValueError, OSError):
            status = 0
        actions.finish(self.cfg.data, rid, status)
        self.redirect("/")

    def sync(self, rid: str) -> None:
        """Board-level Sync now: a signed "sync" event to the "*" endpoint. Same claim/dedupe/rate limit."""
        try:
            uuid.UUID(rid)
        except ValueError:
            return self.reply(400, "Bad action\n", "text/plain")
        ep = self.cfg.actions.get(actions.BOARD)
        if not ep:
            return self.reply(404, "Not found\n", "text/plain")
        ts = int(time.time())
        body = json.dumps(actions.sync_payload(self.cfg.board_url, ts, rid),
                          separators=(",", ":"), ensure_ascii=False).encode()
        outcome = actions.claim(self.cfg.data, {"id": rid, "bot": actions.BOARD, "item_id": actions.SYNC_ITEM,
                                                "action": actions.SYNC, "note": "", "sent_at": ts,
                                                "actor": "owner"})
        if outcome == "dup":
            return self.redirect("/")
        if outcome == "rate":
            return self.reply(429, "Too many syncs\n", "text/plain", [("Retry-After", str(actions.GAP))])
        status = 0
        try:
            status = actions.deliver(ep, body, ts)
        except (ValueError, OSError):
            status = 0
        actions.finish(self.cfg.data, rid, status)
        self.redirect("/")


    def health_control(self) -> None:
        """POST /health/control: owner queued a Close/Stop against the latest health sample."""
        if not self.signed_in():
            return self.reply(401, "Sign in required\n", "text/plain")
        form = self.form()
        if form is None:
            return
        if not self.csrf_ok(form):
            return self.reply(403, "Forbidden\n", "text/plain")
        if not (self.cfg.health and self.cfg.health_control):
            return self.reply(404, "Not found\n", "text/plain")
        kind = form.get("kind", "")
        try:
            pid = int(form.get("pid", ""))
            started_at = int(form.get("started_at", ""))
        except ValueError:
            return self.reply(400, "Bad target\n", "text/plain")
        target = {"pid": pid, "started_at": started_at}
        if kind == "close-browser":
            target["profile"] = form.get("profile", "")
        # Target must appear on the latest sample
        samples = health.load(self.cfg.data)
        if not samples:
            return self.reply(400, "No health sample\n", "text/plain")
        latest = samples[-1]
        ok = False
        if kind == "close-browser":
            for g in latest.get("browsers") or []:
                if (g.get("profile") == target["profile"] and g.get("main_pid") == pid
                        and g.get("started_at") == started_at and g.get("closable") is True):
                    ok = True
                    break
        elif kind == "stop-helper":
            ok = any(h.get("pid") == pid and h.get("started_at") == started_at
                     for h in (latest.get("helpers") or []))
        elif kind == "stop-job":
            ok = any(j.get("pid") == pid and j.get("started_at") == started_at
                     for j in (latest.get("jobs") or []))
        else:
            return self.reply(400, "Bad kind\n", "text/plain")
        if not ok:
            return self.reply(400, "Target not on latest sample\n", "text/plain")
        try:
            health.enqueue_command(self.cfg.data, kind, target)
        except ValueError as e:
            code = 429 if "rate" in str(e).lower() else 400
            return self.reply(code, f"{e}\n", "text/plain")
        self.redirect("/")

    def logout(self) -> None:
        form = self.form()
        if form is None:
            return
        if not self.csrf_ok(form):
            return self.reply(403, "Forbidden\n", "text/plain")
        self.cfg.session_version = bump_session(self.cfg.data)
        self.redirect("/login", [self.set_cookie(SESSION, "", 0)])

    # The push API

    def _health_window(self, name: str) -> list[float]:
        at = time.monotonic()
        recent = [t for t in getattr(self.server, name, []) if at - t < 60]
        setattr(self.server, name, recent)
        return recent

    def health_slot(self) -> bool:
        """Room for one more accepted sample this minute? Only accepted samples count here, so rejected
        pushes can never block the real collector (it sends one a minute)."""
        with self.lock:
            return len(self._health_window("health_ok")) < HEALTH_PER_MINUTE

    def health_count(self, accepted: bool) -> bool:
        """Record an outcome. For a rejection, False once rejections pass their own (higher) cap: answer 429."""
        with self.lock:
            window = self._health_window("health_ok" if accepted else "health_bad")
            window.append(time.monotonic())
            return accepted or len(window) <= HEALTH_BAD_PER_MINUTE

    def api(self, method: str, path_: str) -> None:
        token = token_for(self.cfg, path_)
        if not token:
            return self.json(404, {"error": "the push API is off: set BOTROLL_PUSH_TOKEN"})
        if not _bearer(self.headers.get("Authorization", ""), token):
            return self.json(401, {"error": "missing or wrong push token"}, [("WWW-Authenticate", "Bearer")])
        bot = re.fullmatch(r"/api/roll/([^/]+)", path_)
        if path_ == "/api/health/commands" and method == "GET":
            if not (self.cfg.health and self.cfg.health_control):
                return self.json(404, {"error": "health control is off"})  # a GET changes nothing; see clear_stale
            return self.json(200, {"commands": health.pending_commands(self.cfg.data)})
        if path_ == "/api/health/commands/result" and method == "POST":
            if not (self.cfg.health and self.cfg.health_control):
                return self.json(404, {"error": "health control is off"})
            raw = self.body(4096)
            if raw is None:
                return
            try:
                body = load_json(raw)
                if not isinstance(body, dict) or not body.get("id"):
                    raise ValueError("id required")
                status = str(body.get("status") or "")
                if status not in ("ok", "failed", "refused"):
                    raise ValueError("status must be ok, failed or refused")
                row = health.finish_command(self.cfg.data, str(body["id"]), status, str(body.get("detail") or ""),
                                            str(body.get("nonce") or ""))
            except (ValueError, OSError) as e:
                return self.json(400, {"error": str(e)})
            if not row:
                return self.json(404, {"error": "unknown command or wrong nonce"})
            return self.json(200, {"id": row["id"], "status": row["status"]})
        if method != "POST" or not (bot or path_ in ("/api/watch", "/api/health", "/api/prune")):
            return self.json(404, {"error": "not found"})
        if path_ == "/api/health":
            if not self.cfg.health:
                return self.json(404, {"error": "box health is off: set BOTROLL_HEALTH=1 on the server"})
            if not self.health_slot():
                return self.json(429, {"error": f"at most {HEALTH_PER_MINUTE} samples a minute"},
                                 [("Retry-After", "60")])
        raw = self.body(health.MAX_SNAPSHOT if path_ == "/api/health" else MAX_ROLL)
        if raw is None:
            return
        try:
            if path_ == "/api/health":
                try:
                    result = {"samples": health.append(self.cfg.data, load_json(raw))}
                except ValueError as e:
                    if not self.health_count(False):
                        return self.json(429, {"error": f"over {HEALTH_BAD_PER_MINUTE} rejected samples a minute"},
                                         [("Retry-After", "60")])
                    return self.json(getattr(e, "status", 400), {"error": str(e)})
                self.health_count(True)
            elif path_ == "/api/prune":
                body = load_json(raw)
                keep = body.get("keep") if isinstance(body, dict) else None
                confirm = body.get("confirm", False) if isinstance(body, dict) else False
                if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep) or type(confirm) is not bool:
                    raise ValueError('prune body must be {"keep": [slug, ...], "confirm": true|false}')
                try:
                    gone = prune(self.cfg.data, keep, confirm=confirm)
                except PruneRefused as e:
                    return self.json(409, {"error": str(e)})
                result = {"removed": gone} if confirm else {"would_remove": gone, "dry_run": True}
            elif bot:
                text = raw.decode("utf-8")
                if "\0" in text:
                    raise ValueError("roll files are text")
                result = {"bot": bot[1], "items": store(self.cfg.data, bot[1], text)}
            else:
                result = {"findings": store_findings(self.cfg.data, load_json(raw))}
        except (ValueError, OSError) as e:
            return self.json(400, {"error": str(e)})
        self.json(200, result)


class Server(ThreadingHTTPServer):
    """A thread per request, capped, with reserved room for /health and signed-in traffic."""
    request_queue_size = 64
    daemon_threads = True

    def __init__(self, address, handler, threads: int = MAX_THREADS, unauth: int = MAX_UNAUTH):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(threads)
        self._unauth = threading.BoundedSemaphore(unauth)
        self._health = threading.BoundedSemaphore(threads)
        self._kind: dict = {}
        self._lock = threading.Lock()

    def _busy(self, request) -> None:
        try:
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 5\r\n"
                            b"Connection: close\r\n\r\nbusy\n")
        except OSError:
            pass
        self.shutdown_request(request)

    def _release(self, request) -> None:
        with self._lock:
            tag = self._kind.pop(request, None)
        if tag == "health":
            self._health.release()
        elif tag == "reserved":
            self._slots.release()
        elif tag == "unauth":
            self._slots.release()
            self._unauth.release()

    def process_request(self, request, client_address) -> None:
        kind = _peek_kind(request, getattr(self.RequestHandlerClass, "cfg", None))
        if kind == "health":
            if not self._health.acquire(blocking=False):
                return self._busy(request)
            tag = "health"
        elif kind == "auth":
            if not self._slots.acquire(blocking=False):
                return self._busy(request)
            tag = "reserved"
        else:
            if not self._unauth.acquire(blocking=False):
                return self._busy(request)
            if not self._slots.acquire(blocking=False):
                self._unauth.release()
                return self._busy(request)
            tag = "unauth"
        with self._lock:
            self._kind[request] = tag
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)


def make_server(cfg: Config, port: int, host: str | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "failures": {}, "lock": threading.Lock(),
                                                # scrypt takes 16 MB a check, so only a few run at once
                                                "hashing": threading.BoundedSemaphore(4)})
    return Server((host or ("127.0.0.1" if cfg.insecure_local else "0.0.0.0"), port), handler)


def ensure_data(data: str) -> None:
    """Create the data directory, drop root to UID if we can, and refuse to start if it isn't writable."""
    os.makedirs(data, exist_ok=True)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(data, UID, UID)
        except OSError:
            pass
        try:
            os.setgid(UID)
            os.setuid(UID)
        except OSError as e:
            print(f"botroll: couldn't drop root to uid {UID}: {e}", file=sys.stderr)
    probe = os.path.join(data, ".writable")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise ValueError(f"data directory {data} isn't writable (need write access, typically uid {UID}): {e}") from None


def clear_stale(cfg: Config) -> int:
    """At startup: with control off (BOTROLL_HEALTH or BOTROLL_HEALTH_CONTROL unset), cancel any command queued
    while it was on, so none can run once it's turned back on. Control can only change with a restart, so this is
    the one place it's needed (GET /api/health/commands no longer does it). Returns how many were cancelled."""
    return 0 if cfg.health and cfg.health_control else health.clear_pending(cfg.data)


def serve(cfg: Config, port: int) -> None:
    ensure_data(cfg.data)
    clear_stale(cfg)
    server = make_server(cfg, port)
    host, port = server.server_address[:2]
    zone = os.environ.get("TZ") or os.environ.get("BOTROLL_TZ") or "UTC"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"botroll: {stamp} ({zone}) serving {cfg.data} on http://{host}:{port}"
          + (" without sign-in (--insecure-local)" if cfg.insecure_local else ""), file=sys.stderr)
    if cfg.insecure_http:
        print("botroll: --insecure-http: the session cookie is sent without Secure; "
              "anyone on this network can steal a sign-in", file=sys.stderr)
    if not cfg.push_token:
        print("botroll: the push API is off; set BOTROLL_PUSH_TOKEN to enable it", file=sys.stderr)
    if cfg.health_token:
        print("botroll: /api/health* takes BOTROLL_HEALTH_TOKEN only (the push token is refused there)",
              file=sys.stderr)
    if cfg.actions:
        names = sorted(actions.bots_only(cfg.actions))
        if names:
            print(f"botroll: actions for {', '.join(names)}", file=sys.stderr)
        if actions.BOARD in cfg.actions:
            print("botroll: Sync now button on", file=sys.stderr)
    if cfg.mailer:
        m = cfg.mailer
        notify.start(notify.Notifier(cfg.data, m, lambda: served_snapshot(cfg.data)))
        print(f"botroll: emailing {', '.join(m.to)} via {m.provider}"
              + (f", with a digest at {m.digest_hour}:00" if m.digest_hour is not None else ""), file=sys.stderr)

    def stop(signum, frame) -> None:
        raise SystemExit(0)
    prev = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever()
    finally:
        for s, handler in prev.items():
            signal.signal(s, handler)
        server.server_close()


# The client --------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect with the Authorization header, to any host, over http too."""

    def redirect_request(self, *args) -> None:
        return None


def _env_number(name: str, default: float, cast=float) -> float:
    """A non-negative number from the environment; a bad value warns and uses the default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(cast(raw), 0)
    except ValueError:
        print(f"botroll: {name}={raw!r} is not a {'whole ' if cast is int else ''}number; using {default}",
              file=sys.stderr)
        return default


def send(opener, req: urllib.request.Request, *, idempotent: bool, timeout: float = 30,
         retries: int | None = None, backoff: float | None = None) -> bytes:
    """Send req and return the body, retrying a dropped connection.

    A drop before the request went out (urllib's URLError: refused, reset or cut during the TLS
    handshake) is always retried. A drop after it was sent (no reply) is retried only when idempotent,
    since the server may have acted on it. A reply, even an error, is never retried.
    retries (default $BOTROLL_RETRIES, 3) and backoff (default $BOTROLL_RETRY_BACKOFF, 1 second,
    doubling and capped at 30) tune it; retries=0 turns it off."""
    retries = int(_env_number("BOTROLL_RETRIES", 3, int)) if retries is None else retries
    backoff = _env_number("BOTROLL_RETRY_BACKOFF", 1.0) if backoff is None else backoff
    parts = urlsplit(req.full_url)
    route = parts.path
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise ValueError(f"{route}: {e.code} {e.read().decode(errors='replace').strip()}") from None
        except urllib.error.URLError as e:
            err = f"can't reach {parts.scheme}://{parts.netloc}: {e.reason}"
        except (OSError, http.client.HTTPException) as e:
            err = f"{route}: connection dropped after sending ({type(e).__name__}: {e})"
            if not idempotent:
                raise ValueError(err + "; not retried, the server may have acted on it") from None
        if attempt == retries:
            raise ValueError(err) from None
        print(f"botroll: {err}; retry {attempt + 1}/{retries}", file=sys.stderr)
        time.sleep(min(backoff * 2 ** attempt, 30))
    raise AssertionError("unreachable")


def push(url: str, token: str, directory: str, prune: bool = False, removed: list | None = None,
         confirm: bool = False) -> tuple[int, int, int]:
    """Upload every roll file and the watchdog's process flags. Returns (rolls, findings, skipped).

    prune: then ask the server which roll files are for bots not in this directory (their slugs go into removed).
    Only with confirm does it delete them, within the server's cap.
    Skipped (oversized) files still count as present, so they are never pruned."""
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("refusing to send the push token without https")
    base = url.rstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)

    def post(route: str, body: bytes, ctype: str, idempotent: bool = True) -> bytes:
        req = urllib.request.Request(base + route, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": ctype})
        return send(opener, req, idempotent=idempotent)

    names = bots(directory)
    pushed, skipped = 0, 0
    for bot in names:
        with open(path(directory, bot), "rb") as f:
            body = f.read()
        if len(body) > MAX_ROLL:
            print(f"botroll: skipping {bot}: roll file is {len(body)} bytes, over {MAX_ROLL}", file=sys.stderr)
            skipped += 1
            continue
        post(f"/api/roll/{bot}", body, "text/markdown; charset=utf-8")
        pushed += 1
    entries = [(bot, i) for bot in names for i in items(read(directory, bot))]
    findings = [asdict(f) for f in check(entries, processes(), now()) if f.kind in PROCESS_KINDS]
    post("/api/watch", json.dumps(findings).encode(), "application/json")
    if prune:
        if not names:
            raise ValueError("refusing to prune: no roll files in " + directory)
        # Not idempotent: a repeat after a lost reply could delete again within the hourly cap.
        out = json.loads(post("/api/prune", json.dumps({"keep": names, "confirm": confirm}).encode(),
                              "application/json", idempotent=False))
        if removed is not None:
            removed.extend(out.get("removed") or out.get("would_remove") or [])
    return pushed, len(findings), skipped
