"""Optional email from botroll serve: "Needs you" when a bot starts waiting on you, and a daily digest.

Off unless BOTROLL_EMAIL_PROVIDER is set. Standard library only. Bot text is untrusted: it is escaped,
and its links and domains are defanged, so an email can only link to your own board.
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__, local, locked, title, write_atomic
from .board import needs_you

STATE = ".notify.json"  # what was already emailed, next to the roll files, so a restart doesn't resend
EVERY = 15 * 60  # at most one "Needs you" email per 15 minutes
QUIET = 60  # wait for a minute without new asks, so a burst becomes one email
MAX_WAIT = 5 * 60  # send within 5 minutes even if asks keep arriving
DELAYS = (2, 8)  # seconds between the tries of one send
TLS = {"starttls": 587, "ssl": 465, "off": 25}
LOOPBACK = ("localhost", "127.0.0.1", "::1")
FOOTER = "Sent by your BotRoll server. Text from your bots is shown with its links disabled."


@dataclass
class Mail:
    subject: str
    text: str
    html: str


@dataclass
class Mailer:
    provider: str
    sender: str
    to: list[str]
    env: dict
    board: str | None = None  # BOTROLL_URL, the only link an email carries
    digest_hour: int | None = None

    def send(self, mail: Mail) -> None:
        SEND[self.provider](self, mail)


def log(message: str) -> None:
    print(f"botroll: email: {message}", file=sys.stderr, flush=True)


# Configuration -----------------------------------------------------------------

def configure(env=os.environ) -> Mailer | None:
    """The mailer from the environment, None when email is off, or a ValueError naming what's missing."""
    provider = env.get("BOTROLL_EMAIL_PROVIDER", "").strip().lower()
    if not provider:
        return None
    need = {"resend": ["RESEND_API_KEY"], "cloudflare": ["CF_ACCOUNT_ID", "CF_EMAIL_TOKEN"], "smtp": ["SMTP_HOST"]}
    if provider not in need:
        raise ValueError(f"BOTROLL_EMAIL_PROVIDER must be one of {', '.join(need)}")
    missing = [k for k in ["BOTROLL_EMAIL_FROM", "BOTROLL_EMAIL_TO", *need[provider]] if not env.get(k, "").strip()]
    if missing:
        raise ValueError(f"email via {provider} needs {', '.join(missing)}")
    sender, to = env["BOTROLL_EMAIL_FROM"].strip(), [a.strip() for a in env["BOTROLL_EMAIL_TO"].split(",") if a.strip()]
    for address in [sender, *to]:
        if not re.fullmatch(r"[^\s@<>,]+@[^\s@<>,]+|[^\r\n<>]*<[^\s@<>,]+@[^\s@<>,]+>", address):
            raise ValueError(f"not an email address: {address!r}")
    if provider == "smtp":
        tls = env.get("SMTP_TLS", "starttls").lower()
        if tls not in TLS:
            raise ValueError(f"SMTP_TLS must be one of {', '.join(TLS)}")
        if tls == "off" and env["SMTP_HOST"] not in LOOPBACK:
            raise ValueError("SMTP_TLS=off sends your password in the clear; it's only allowed to localhost")
        if not env.get("SMTP_PORT", str(TLS[tls])).isdigit():
            raise ValueError("SMTP_PORT must be a number")
        if bool(env.get("SMTP_USER")) != bool(env.get("SMTP_PASS")):
            raise ValueError("set both SMTP_USER and SMTP_PASS, or neither")
    board = env.get("BOTROLL_URL", "").strip() or None
    if board and not re.match(r"https://|http://(localhost|127\.0\.0\.1)[:/]", board + "/"):
        raise ValueError("BOTROLL_URL must be an https address")
    hour = env.get("BOTROLL_DIGEST_HOUR", "").strip()
    if hour and not (hour.isdigit() and int(hour) < 24):
        raise ValueError("BOTROLL_DIGEST_HOUR must be an hour from 0 to 23")
    try:
        env.get("BOTROLL_TZ") and ZoneInfo(env["BOTROLL_TZ"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"BOTROLL_TZ isn't a timezone: {env['BOTROLL_TZ']!r}") from None
    return Mailer(provider, sender, to, dict(env), board, int(hour) if hour else None)


# Providers: each turns a Mail into one request ----------------------------------

def _post(url: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": f"botroll/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise OSError(f"{e.code} {e.read()[:300].decode(errors='replace')}") from None


def resend(m: Mailer, mail: Mail) -> None:
    _post("https://api.resend.com/emails", m.env["RESEND_API_KEY"],
          {"from": m.sender, "to": m.to, "subject": mail.subject, "text": mail.text, "html": mail.html})


def cloudflare(m: Mailer, mail: Mail) -> None:
    reply = _post(f"https://api.cloudflare.com/client/v4/accounts/{m.env['CF_ACCOUNT_ID']}/email/sending/send",
                  m.env["CF_EMAIL_TOKEN"],
                  {"from": m.sender, "to": m.to, "subject": mail.subject, "text": mail.text, "html": mail.html})
    if not reply.get("success", True) or (reply.get("result") or {}).get("permanent_bounces"):
        raise OSError(f"Cloudflare didn't deliver it: {json.dumps(reply)[:300]}")


def smtp(m: Mailer, mail: Mail) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = mail.subject, m.sender, ", ".join(m.to)
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain="botroll")
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    tls = m.env.get("SMTP_TLS", "starttls").lower()
    host, port, context = m.env["SMTP_HOST"], int(m.env.get("SMTP_PORT", TLS[tls])), ssl.create_default_context()
    connect = (lambda: smtplib.SMTP_SSL(host, port, timeout=30, context=context)) if tls == "ssl" else \
        (lambda: smtplib.SMTP(host, port, timeout=30))
    with connect() as s:
        if tls == "starttls":
            s.starttls(context=context)
        if m.env.get("SMTP_USER"):
            s.login(m.env["SMTP_USER"], m.env["SMTP_PASS"])
        s.send_message(msg)


SEND = {"resend": resend, "cloudflare": cloudflare, "smtp": smtp}


# Content -----------------------------------------------------------------------

# Letters in any script, so аpple.com (Cyrillic а) is caught. IPv4 is separate.
DOMAIN = re.compile(r"(?:[\w-]+\.)+[^\W\d_]{2,}", re.UNICODE)
IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
# Two or more colons with hex groups, with or without brackets.
IPV6 = re.compile(r"(?i)(?:\[(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}\]|(?:[0-9a-f]{1,4}:){1,7}:[0-9a-f]{0,4}|:(?::[0-9a-f]{1,4}){1,7})")
HEXIP = re.compile(r"(?i)(?<![0-9a-fx])0x[0-9a-f]{6,10}(?![0-9a-f])")
DOTHEX = re.compile(r"(?i)(?<![0-9a-f])(?:0x[0-9a-f]{1,4}|\d{1,3})(?:\.(?:0x[0-9a-f]{1,4}|\d{1,3})){3}(?![0-9a-f])")
INTIP = re.compile(r"(?<!\d)(\d{9,10})(?!\d)")
FAR_STATE = 10 * 365 * 86400  # reject notify timestamps more than 10 years from now


def defang(text: str | None, limit: int = 300) -> str:
    """Bot text made safe to email: one line, no invisible or direction-changing characters,
    and nothing a mail client would turn into a link (https[://]evil[.]example)."""
    spaced = (" " if c in "\r\n\t" else c for c in text or "")
    text = "".join(c for c in spaced if unicodedata.category(c) not in ("Cc", "Cf"))
    text = unicodedata.normalize("NFKC", " ".join(text.split()))
    for dot in ("\u3002", "\uff0e", "\uff61"):  # ideographic / fullwidth dots NFKC may leave
        text = text.replace(dot, ".")
    text = text.replace("://", "[://]")
    text = DOMAIN.sub(lambda m: m[0].replace(".", "[.]"), text)
    text = IPV4.sub(lambda m: m[0].replace(".", "[.]"), text)
    text = IPV6.sub(lambda m: m[0].replace(":", "[:]"), text)
    text = HEXIP.sub(lambda m: m[0][:2] + "[.]" + m[0][2:], text)
    text = DOTHEX.sub(lambda m: m[0].replace(".", "[.]"), text)

    def _intip(m: re.Match) -> str:
        n = int(m[1])
        if n > 4294967295:
            return m[1]
        return m[1][:-2] + "[.]" + m[1][-2:]

    text = INTIP.sub(_intip, text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def compose(subject: str, lead: str, sections: list[tuple[str, list[tuple[str, str]]]], board: str | None) -> Mail:
    """One email as text and HTML from the same parts. Rows are (line, detail), already defanged."""
    text = [lead, ""]
    for label, rows in sections:
        text += [label] + [f"- {line}" + (f"\n  {detail}" if detail else "") for line, detail in rows] + [""]
    text += [f"Open the board: {board}", ""] if board else []
    e = html.escape  # the board's light tokens, inline, since mail clients drop <style> and ignore light-dark()
    font = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,Roboto,'Helvetica Neue',Arial,sans-serif"
    body = "".join(
        f'<p style="margin:28px 0 10px;font-size:13px;font-weight:500;color:#6b6b6b">{e(label)}</p>'
        '<div style="background:#f6f5f3;border-radius:20px;padding:4px 22px">'
        + "".join(f'<div style="padding:14px 0;{"border-top:1px solid #e4e2de" if n else ""}">'
                  f'<div style="font-size:15px;line-height:1.5">{e(line)}</div>'
                  + (f'<div style="margin-top:2px;font-size:13px;line-height:1.5;color:#6b6b6b">{e(detail)}</div>' if detail else "")
                  + "</div>" for n, (line, detail) in enumerate(rows)) + "</div>" for label, rows in sections)
    button = (f'<p style="margin:28px 0 0"><a href="{e(board)}" style="display:inline-block;padding:12px 22px;'
              'border-radius:999px;background:#111;color:#fff;text-decoration:none;font-size:14px;font-weight:500">'
              'Open the board</a></p>' if board else "")
    page = (f'<div style="{font};background:#fff;padding:32px 20px;color:#111;line-height:1.6">'
            '<div style="max-width:560px;margin:0 auto">'
            '<div style="font-size:16px;font-weight:500;letter-spacing:-0.03em">BotRoll</div>'
            f'<p style="margin:28px 0 0;font-size:28px;font-weight:400;line-height:1.2;letter-spacing:-0.02em">{e(lead)}</p>'
            f'{body}{button}<p style="margin:32px 0 0;font-size:12px;color:#6b6b6b">{e(FOOTER)}</p></div></div>')
    return Mail(subject, "\n".join(text + [FOOTER]) + "\n", page)


def _who(bot: dict, item: dict) -> str:
    return f"{defang(title(bot['name'], bot['role']))} · {item['id']}"


def _count(n: int, one: str, many: str) -> str:
    return (one if n == 1 else many).format(n)


def needs_you_mail(asks: list[tuple[dict, dict]], waiting: int, board: str | None) -> Mail:
    lead = _count(len(asks), "1 new thing needs you.", "{} new things need you.")
    if waiting > len(asks):
        lead += f" {waiting} are waiting in all."
    rows = [(defang(i["note"] or i["title"]), _who(b, i) + (f" · {defang(i['title'])}" if i["note"] else ""))
            for b, i in asks]
    return compose("BotRoll: " + lead.split(".")[0], lead, [("New", rows)], board)


def digest_mail(snap: dict, board: str | None) -> Mail | None:
    """The day's digest, or None if there's nothing to say."""
    every = [(b, i) for b in snap["bots"] for i in b["items"]]
    done = [(defang(i["title"]), _who(b, i) + (f" · {defang(i['note'])}" if i["note"] else ""))
            for b, i in every if i["state"] == "done"]
    stuck = [(defang(i["title"]), _who(b, i) + " · " + (f"blocked: {defang(i['note'])}" if i["state"] == "blocked"
                                                          else "no update since " + (i["updated"] or "it was asked")))
             for b, i in every if i["state"] == "blocked" or (i["stale"] and i["state"] in ("doing", "todo"))]
    quiet = [(defang(name), "nothing it's doing has been updated lately") for name in snap["summary"]["quiet_bots"]]
    found = [(defang(f["text"]), f["kind"] + (f" · {f['bot']}/{f['id']}" if f["bot"] and f["id"] else ""))
             for f in snap["flags"] if f["kind"] != "stale"]
    if not (done or stuck or quiet or found):
        return None
    parts = [_count(n, one, many) for n, one, many in [
        (len(done), "1 done in the last 24h", "{} done in the last 24h"), (len(stuck), "1 stuck", "{} stuck"),
        (len(quiet), "1 gone quiet", "{} gone quiet"), (len(found), "1 watchdog finding", "{} watchdog findings")] if n]
    lead = " and ".join(filter(None, [", ".join(parts[:-1]), parts[-1]])) + "."
    sections = [(label, rows) for label, rows in [("Done today", done), ("Stuck", stuck), ("Gone quiet", quiet),
                                                  ("Watchdog", found)] if rows]
    return compose(f"BotRoll daily: {len(done)} done, {len(stuck)} stuck", lead, sections, board)


def hello(board: str | None) -> Mail:
    """What `botroll email test` sends."""
    return compose("BotRoll: test email", "Email from BotRoll works.",
                   [("What you'll get", [("Needs you", "when a bot starts waiting on you, at most every 15 minutes"),
                                         ("A daily digest", "at BOTROLL_DIGEST_HOUR, if set and there's news")])], board)


# Deciding what to send -------------------------------------------------------------

def _ok_ts(v, now: float) -> bool:
    """A real timestamp, not NaN/Infinity/bool, and not more than ten years away."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and abs(v) <= now + FAR_STATE)


def _valid_state(raw, now: float | None = None) -> bool:
    """True if raw is a notify state object with the right types. [] / 42 / bad fields are not."""
    now = time.time() if now is None else now
    if not isinstance(raw, dict):
        return False
    if "notified" in raw and not (isinstance(raw["notified"], list) and all(isinstance(x, str) for x in raw["notified"])):
        return False
    if "pending" in raw and not (isinstance(raw["pending"], dict)
                                 and all(isinstance(k, str) and _ok_ts(v, now) for k, v in raw["pending"].items())):
        return False
    for key in ("sent_at", "retry_at"):
        if key in raw and not _ok_ts(raw[key], now):
            return False
    if "failures" in raw and (not isinstance(raw["failures"], (int, float)) or isinstance(raw["failures"], bool)
                              or not math.isfinite(raw["failures"])):
        return False
    if "digest_on" in raw and raw["digest_on"] is not None and not isinstance(raw["digest_on"], str):
        return False
    return True


def load_state(path: str) -> dict:
    """The saved notify state, or a fresh one. A bad file is moved aside so it can't crash-loop serve."""
    fresh = {"notified": [], "pending": {}, "sent_at": 0, "retry_at": 0, "failures": 0, "digest_on": None}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return fresh
    except (OSError, ValueError):
        raw = None
    if raw is None or not _valid_state(raw):
        bad = f"{path}.bad-{int(time.time())}"
        try:
            os.replace(path, bad)
            log(f"ignored a bad {STATE}; moved it to {os.path.basename(bad)} and starting fresh")
        except OSError as e:
            log(f"ignored a bad {STATE} ({e}); starting fresh")
        return fresh
    return fresh | raw


class Notifier:
    """Called every minute with the time. State lives in the data directory, so restarts don't resend."""

    def __init__(self, data: str, mailer: Mailer, snapshot: Callable[[], dict], delays=DELAYS):
        self.data, self.mailer, self.snapshot, self.delays = data, mailer, snapshot, delays
        self.path = os.path.join(data, STATE)
        self.state = load_state(self.path)

    def tick(self, at: float) -> None:
        before = json.dumps(self.state, sort_keys=True)
        snap, st = self.snapshot(), self.state
        waiting = {f"{b['bot']}/{i['id']}": (b, i) for b, i in needs_you(snap)}
        # An ask that stops waiting is forgotten, so if it comes back it's new again.
        st["notified"] = [k for k in st["notified"] if k in waiting]
        st["pending"] = {k: t for k, t in st["pending"].items() if k in waiting and isinstance(t, (int, float))}
        st["pending"] |= {k: at for k in waiting if k not in st["notified"] and k not in st["pending"]}
        settled = at - max(st["pending"].values()) >= QUIET if st["pending"] else False
        starved = at - min(st["pending"].values()) >= MAX_WAIT if st["pending"] else False
        if (st["pending"] and (settled or starved) and at - st["sent_at"] >= EVERY
                and at >= st["retry_at"]):
            keys = [k for k in waiting if k in st["pending"]]  # oldest first, as on the board
            if self.deliver(needs_you_mail([waiting[k] for k in keys], len(waiting), self.mailer.board), at):
                st["notified"] += keys
                st["pending"], st["sent_at"] = {}, at
        if self.mailer.digest_hour is not None and at >= st["retry_at"]:
            today = local(at)
            if today.hour == self.mailer.digest_hour and st["digest_on"] != today.date().isoformat():
                mail = digest_mail(snap, self.mailer.board)
                if mail is None or self.deliver(mail, at):
                    st["digest_on"] = today.date().isoformat()
        if json.dumps(st, sort_keys=True) != before:
            with locked(self.data):
                write_atomic(self.path, json.dumps(st))

    def deliver(self, mail: Mail, at: float) -> bool:
        """Send with a few quick retries. After that, log it and back off: 2, 4, 8 minutes up to an hour."""
        for delay in (*self.delays, None):
            try:
                self.mailer.send(mail)
                self.state["failures"], self.state["retry_at"] = 0, 0
                return True
            except (OSError, smtplib.SMTPException, ValueError) as e:
                error = e
                if delay is not None:
                    time.sleep(delay)
        self.state["failures"] += 1
        wait = min(60 * 2 ** self.state["failures"], 3600)
        self.state["retry_at"] = at + wait
        log(f"couldn't send {mail.subject!r} via {self.mailer.provider}: {error}. Trying again in {wait // 60}m.")
        return False


def start(notifier: Notifier, every: float = 60) -> threading.Event:
    """Check every minute in a background thread, until the returned event is set."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                notifier.tick(time.time())
            except Exception as e:  # a bug here must never take the board down
                log(f"skipped a check: {e!r}")
            stop.wait(every)
    threading.Thread(target=loop, name="botroll-email", daemon=True).start()
    return stop
