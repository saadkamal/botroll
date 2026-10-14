"""Optional board actions: the owner clicks, BotRoll POSTs a signed JSON event to the bot's URL.

A board-level endpoint (key "*" in the actions file, or BOTROLL_SYNC_URL) adds a "Sync now"
button that sends action "sync" with no bot or item, e.g. to ask the bots' machine to push.

Off unless an actions file or BOTROLL_ACTION_<SLUG>_URL is set. The board never changes item
state; the bot does that after it acts, then pushes.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import OPEN, SLUG, __version__, locked, write_atomic

FILE = "actions.json"  # default, next to the rolls; or $BOTROLL_ACTIONS
STATE = ".actions.json"  # id → record; claimed before send
LOG = ".actions.jsonl"  # leftover from 0.1; read once if STATE is missing
GAP = 10  # seconds between actions on one item
TIMEOUT = 10
MAX_NOTE = 500
MIN_SECRET = 32
KINDS = ("approve", "decline", "reply", "nudge")
WAITING = ("approve", "decline", "reply")  # only on waiting-human items
ALT_PORTS = frozenset({8080, 8443})
BOARD = "*"  # actions-file key for the board-level endpoint; never a bot slug
SYNC = "sync"
SYNC_ITEM = "sync"  # item_id stored for sync clicks (rate limit and status)
SYNC_ENV = re.compile(r"BOTROLL_SYNC_(URL|SECRET|HEADER)")
SYNC_SHOW = 600  # seconds the "Sync sent/failed" status stays on the board


def _warn(text: str) -> None:
    print(f"botroll: warning: {text}", file=sys.stderr)
ACTION_ENV = re.compile(r"BOTROLL_ACTION_([A-Z0-9]+(?:_[A-Z0-9]+)*)_(URL|SECRET|HEADER)")
TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off", ""})


@dataclass
class Endpoint:
    url: str
    secret: str = ""  # HMAC; empty if the receiver only takes a static header
    header: tuple[str, str] | None = None  # extra header, e.g. ("Authorization", "Bearer …")
    allow_http: bool = False
    allow_private: bool = False
    allow_ports: bool = False


@dataclass
class _Target:
    scheme: str
    host: str
    port: int
    ip: str
    path: str
    host_header: str


def _flag(raw, name: str) -> bool:
    """Only 1/true/yes/on enable; 0/false/no/off/empty disable. Anything else is an error."""
    if raw is True or raw == 1:
        return True
    if raw is False or raw == 0 or raw is None:
        return False
    text = str(raw).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    raise ValueError(f"{name} must be 1/true/yes/on or 0/false/no/off/empty, not {raw!r}")


def _parse_header(raw) -> tuple[str, str] | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        name, value = raw.get("name") or raw.get("header"), raw.get("value")
        if not name or value is None:
            raise ValueError("action header needs name and value")
        return str(name).strip(), str(value)
    text = str(raw).strip()
    name, sep, value = text.partition(":")
    if not sep or not name.strip():
        raise ValueError("action header must look like 'Authorization: Bearer …'")
    return name.strip(), value.strip()


def _endpoint(raw: dict, allow_http: bool, allow_private: bool, allow_ports: bool) -> Endpoint:
    if not isinstance(raw, dict) or not raw.get("url"):
        raise ValueError("each action needs a url")
    secret = str(raw.get("secret") or "")
    header = _parse_header(raw.get("header"))
    if not secret and not header:
        raise ValueError("each action needs a secret (HMAC) or a header (static key)")
    if secret and len(secret) < MIN_SECRET:
        raise ValueError(f"action secret must be at least {MIN_SECRET} characters")
    http = _flag(raw["allow_http"], "allow_http") if "allow_http" in raw else allow_http
    private = _flag(raw["allow_private"], "allow_private") if "allow_private" in raw else allow_private
    ports = _flag(raw["allow_ports"], "allow_ports") if "allow_ports" in raw else allow_ports
    return Endpoint(str(raw["url"]).strip(), secret, header, http, private, ports)


def load(data: str, env=os.environ) -> dict[str, Endpoint]:
    """Endpoints by bot slug. Missing file and empty env → {} (feature off)."""
    allow_http = _flag(env.get("BOTROLL_ACTIONS_ALLOW_HTTP"), "BOTROLL_ACTIONS_ALLOW_HTTP")
    allow_private = _flag(env.get("BOTROLL_ACTIONS_ALLOW_PRIVATE"), "BOTROLL_ACTIONS_ALLOW_PRIVATE")
    allow_ports = _flag(env.get("BOTROLL_ACTIONS_ALLOW_PORTS"), "BOTROLL_ACTIONS_ALLOW_PORTS")
    path = env.get("BOTROLL_ACTIONS") or os.path.join(data, FILE)
    found: dict[str, Endpoint] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.loads(f.read())
        except (OSError, ValueError) as e:
            raise ValueError(f"can't read {path}: {e}") from None
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be a JSON object of slug → {{url, secret}}")
        for slug, spec in raw.items():
            if slug == BOARD:
                found[BOARD] = _endpoint(spec, allow_http, allow_private, allow_ports)
                continue
            if not SLUG.fullmatch(str(slug)):
                raise ValueError(f"action slug is not a bot slug: {slug!r}")
            found[slug] = _endpoint(spec, allow_http, allow_private, allow_ports)
    extras: dict[str, dict] = {}
    sync: dict[str, str] = {}
    for key, val in env.items():
        if key.startswith("BOTROLL_SYNC_"):
            m = SYNC_ENV.fullmatch(key)
            if not m:
                _warn(f"ignoring {key}: not BOTROLL_SYNC_URL, _SECRET or _HEADER")
                continue
            sync[m[1].lower()] = val
            continue
        if not key.startswith("BOTROLL_ACTION_"):
            continue
        m = ACTION_ENV.fullmatch(key)
        if not m:
            raise ValueError(f"{key} is not BOTROLL_ACTION_<SLUG>_URL, _SECRET or _HEADER")
        extras.setdefault(m[1], {})[m[2].lower()] = val
    for token, spec in extras.items():
        if "url" not in spec or not str(spec.get("url") or "").strip():
            raise ValueError(f"BOTROLL_ACTION_{token}_URL is required when _SECRET or _HEADER is set")
        slug = token.lower().replace("_", "-")
        if not SLUG.fullmatch(slug):
            raise ValueError(f"BOTROLL_ACTION_{token}_URL is not a bot slug")
        found[slug] = _endpoint(spec, allow_http, allow_private, allow_ports)
    if sync:
        if not str(sync.get("url") or "").strip():
            print("botroll: BOTROLL_SYNC_URL is empty or unset, so the Sync now button is off"
                  + (" (BOTROLL_SYNC_SECRET/_HEADER ignored)" if set(sync) - {"url"} else ""), file=sys.stderr)
        else:
            if BOARD in found:
                _warn(f'both "{BOARD}" in {path} and BOTROLL_SYNC_URL are set; using BOTROLL_SYNC_URL')
            found[BOARD] = _endpoint(sync, allow_http, allow_private, allow_ports)
    for ep in found.values():
        resolve(ep.url, ep.allow_http, ep.allow_private, ep.allow_ports)
    return found


def _unwrap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _check_port(port: int, allow_http: bool, allow_ports: bool) -> None:
    if port == 443 or port == 80:
        return
    if port in ALT_PORTS and (allow_http or allow_ports):
        return
    if allow_ports:
        return
    raise ValueError("action URL port must be 443 or 80; 8080/8443 need allow_http or "
                     "allow_ports / BOTROLL_ACTIONS_ALLOW_PORTS; any other port needs allow_ports")


def resolve(url: str, allow_http: bool = False, allow_private: bool = False,
            allow_ports: bool = False) -> _Target:
    """Validate the URL and return the IP we will connect to. Raises ValueError."""
    parts = urlsplit(url)
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise ValueError("action URLs must be https (or set allow_http / BOTROLL_ACTIONS_ALLOW_HTTP)")
    host = (parts.hostname or "").strip("[]")
    if not host:
        raise ValueError("action URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    _check_port(port, allow_http, allow_ports)
    try:
        ips = [_unwrap(ipaddress.ip_address(host))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            raise ValueError(f"can't resolve {host}: {e}") from None
        ips = [_unwrap(ipaddress.ip_address(i[4][0])) for i in infos]
    if not ips:
        raise ValueError(f"can't resolve {host}")
    if not allow_private:
        for ip in ips:
            if not ip.is_global:
                raise ValueError(f"action URL points at a private address ({ip})")
    ip = ips[0]
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    default = 443 if parts.scheme == "https" else 80
    name = f"[{host}]" if ":" in host else host
    header = name if port == default else f"{name}:{port}"
    return _Target(parts.scheme, host, port, str(ip), path, header)


def check_url(url: str, allow_http: bool = False, allow_private: bool = False,
              allow_ports: bool = False) -> None:
    resolve(url, allow_http, allow_private, allow_ports)


def sign(secret: str, body: bytes, ts: int) -> str:
    """Stripe-style header value: t=<unix>,v1=<hex HMAC-SHA256 of '{t}.{body}'>."""
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify(secret: str, body: bytes, header: str, max_age: int = 300) -> bool:
    """True if header is a fresh, matching signature. Constant-time on the digest."""
    fields = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, got = fields.get("t", ""), fields.get("v1", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > max_age:
        return False
    want = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(got, want)


def payload(action: str, bot: str, item: dict, note: str, board_url: str | None,
            sent_at: int, rid: str) -> dict:
    return {"event": "botroll.action", "action": action, "bot": bot, "item_id": item["id"],
            "item_title": item["title"], "state": item["state"], "note": note or item.get("note") or "",
            "actor": "owner", "board_url": board_url, "sent_at": sent_at, "id": rid}


def sync_payload(board_url: str | None, sent_at: int, rid: str) -> dict:
    """The board-level "Sync now" event. bot and item_id are null: it is for the whole board."""
    return {"event": "botroll.action", "action": SYNC, "bot": None, "item_id": None,
            "item_title": None, "state": None, "note": "", "actor": "owner",
            "board_url": board_url, "sent_at": sent_at, "id": rid}


def bots_only(endpoints) -> dict:
    """Per-bot endpoints, without the board-level one."""
    return {k: v for k, v in (endpoints or {}).items() if k != BOARD}


def sync_result(data: str, at: float | None = None) -> dict | None:
    """{ok, status, pending, sent_at} for the last Sync now click in the last SYNC_SHOW seconds, or None.

    Older clicks are not shown, so the status resets on a later reload."""
    row = last_for(data, BOARD, SYNC_ITEM)
    if not row:
        return None
    sent_at = int(row.get("sent_at") or 0)
    if (time.time() if at is None else at) - sent_at > SYNC_SHOW:
        return None
    status = row.get("status")
    if status is None:
        return {"ok": False, "status": None, "pending": True, "sent_at": sent_at}
    return {"ok": 200 <= int(status or 0) < 300, "status": status, "pending": False, "sent_at": sent_at}


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("delivery deadline")
    return left


def _http_post(sock, host_header: str, path: str, headers: dict[str, str],
               body: bytes, deadline: float) -> int:
    lines = [f"POST {path} HTTP/1.1", f"Host: {host_header}"]
    for key, value in headers.items():
        if key.lower() != "host":
            lines.append(f"{key}: {value}")
    lines += [f"Content-Length: {len(body)}", "Connection: close", ""]
    raw = ("\r\n".join(lines) + "\r\n").encode() + body
    view = memoryview(raw)
    while view:
        sock.settimeout(_left(deadline))
        sent = sock.send(view)
        view = view[sent:]
    buf = bytearray()
    while b"\r\n" not in buf:
        sock.settimeout(_left(deadline))
        chunk = sock.recv(2048)
        if not chunk:
            return 0
        buf.extend(chunk)
        if len(buf) > 65536:
            return 0
    parts = buf.split(b"\r\n", 1)[0].split(None, 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return 0
    while len(buf) < 256:
        sock.settimeout(_left(deadline))
        chunk = sock.recv(256)
        if not chunk:
            break
        buf.extend(chunk)
    return int(parts[1])


def deliver(ep: Endpoint, body: bytes, ts: int) -> int:
    """POST body to the validated IP. Returns the HTTP status, or 0 on a network error. No redirects."""
    target = resolve(ep.url, ep.allow_http, ep.allow_private, ep.allow_ports)
    headers = {"Content-Type": "application/json", "User-Agent": f"botroll/{__version__}"}
    if ep.secret:
        headers["X-BotRoll-Signature"] = sign(ep.secret, body, ts)
    if ep.header:
        headers[ep.header[0]] = ep.header[1]
    deadline = time.monotonic() + TIMEOUT
    sock = None
    try:
        sock = socket.create_connection((target.ip, target.port), timeout=_left(deadline))
        if target.scheme == "https":
            sock.settimeout(_left(deadline))
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.host)
        return _http_post(sock, target.host_header, target.path, headers, body, deadline)
    except (OSError, TimeoutError, ssl.SSLError):
        return 0
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _read_jsonl(data: str) -> list[dict]:
    path = os.path.join(data, LOG)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id"):
            out.append(row)
    return out


def records(data: str) -> dict[str, dict]:
    """Every stored click, keyed by action id. Not a capped tail of a log."""
    path = os.path.join(data, STATE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.loads(f.read())
    except FileNotFoundError:
        raw = None
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    return {str(row["id"]): row for row in _read_jsonl(data)}


def _save(data: str, recs: dict[str, dict]) -> None:
    write_atomic(os.path.join(data, STATE), json.dumps(recs, ensure_ascii=False))


def claim(data: str, entry: dict) -> str | None:
    """Lock, refuse a duplicate id or a too-soon item, else persist pending. None means send."""
    rid = entry["id"]
    with locked(data):
        recs = records(data)
        if rid in recs:
            return "dup"
        last = 0
        for row in recs.values():
            if row.get("bot") == entry["bot"] and row.get("item_id") == entry["item_id"]:
                last = max(last, int(row.get("sent_at") or 0))
        if last and time.time() - last < GAP:
            return "rate"
        recs[rid] = {**entry, "status": None}
        _save(data, recs)
        return None


def finish(data: str, rid: str, status: int) -> None:
    with locked(data):
        recs = records(data)
        if rid in recs:
            recs[rid]["status"] = status
            _save(data, recs)


def seen(data: str, rid: str) -> dict | None:
    return records(data).get(rid) if rid else None


def last_for(data: str, bot: str, item_id: str) -> dict | None:
    best = None
    for row in records(data).values():
        if row.get("bot") == bot and row.get("item_id") == item_id:
            if best is None or int(row.get("sent_at") or 0) >= int(best.get("sent_at") or 0):
                best = row
    return best


def results(data: str, snap: dict) -> dict[str, dict]:
    """bot/T-n → {ok, action, pending, status} for the last click on that item."""
    updated = {f"{b['bot']}/{i['id']}": i.get("updated") or ""
               for b in snap["bots"] for i in b["items"]}
    out: dict[str, dict] = {}
    for row in records(data).values():
        if row.get("bot") == BOARD:
            continue
        key = f"{row.get('bot')}/{row.get('item_id')}"
        status = row.get("status")
        if status is None:
            out[key] = {"ok": False, "action": row.get("action"), "pending": True, "status": None}
            continue
        ok = 200 <= int(status or 0) < 300
        pending = bool(ok and row.get("sent_at") and updated.get(key, "") <
                       time.strftime("%Y-%m-%d %H:%M", time.localtime(row["sent_at"])))
        out[key] = {"ok": ok, "action": row.get("action"), "pending": pending, "status": status}
    return out


def allowed(action: str, state: str) -> bool:
    if action not in KINDS or state not in OPEN:
        return False
    if action in WAITING and state != "waiting-human":
        return False
    return True
