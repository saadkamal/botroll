"""Box health: an optional collector for the machine the bots run on, and the rolling store the board reads.

Linux only (it reads /proc and procps `ps`). Nothing here runs unless you run `botroll health`.
The parsers are pure functions over text, so they're tested against fixtures.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import subprocess
import time
import urllib.request
from urllib.parse import unquote, urlsplit

log = logging.getLogger("botroll.health")

HEALTH = ".health.json"  # the rolling store, next to the roll files
KEEP_SECONDS = 24 * 3600
MAX_SAMPLES = 24 * 60 + 60  # 24h at 60s, with some slack
MAX_SNAPSHOT = 64 * 1024  # bytes, per pushed snapshot

DEFAULT_JOBS = [  # name, cmdline regex, expected minutes before it counts as stalled under load
    {"name": "ffmpeg", "match": r"(^|/)ffmpeg\b", "expect_minutes": 30},
    {"name": "vitest", "match": r"\bvitest\b", "expect_minutes": 15},
    {"name": "next build", "match": r"\bnext(\.js)?\b.*\bbuild\b", "expect_minutes": 15},
    {"name": "npm install", "match": r"\bnpm(-cli\.js)?\b.*\b(install|ci|i)\b", "expect_minutes": 10},
    {"name": "npm run", "match": r"\bnpm(-cli\.js)?\b.*\brun\b", "expect_minutes": 30},
]
DEFAULTS = {
    "name": None,  # host label on the board; default: the hostname
    "browsers": {},  # user-data-dir (full path or its last part) -> bot name
    # Optional command: argv receives the profile path; stdout first line is the bot name.
    # Empty / non-zero exit / timeout → unmapped. Site-specific lookups belong in this hook, not in core.
    "profile_resolve": None,
    # Plain names file: one "profile<TAB>name" per line (profile = full path or last segment).
    "names_file": None,
    # Per-display name files: "<display_names_dir>/<N>" holds the name of whoever works on DISPLAY=:N.
    # N comes from the browser's own DISPLAY, else from a profile folder ending in -N (e.g. Fork-3).
    "display_names_dir": "~/.botroll/displays",
    "browser_match": r"(^|/)(chrome|chromium|chromium-browser|google-chrome|msedge)( |$)",
    "helper_match": "playwright-runtime",
    "helper_stale_hours": 2,
    "jobs": DEFAULT_JOBS,
    "tabs": True,  # read DevTools /json/list on 127.0.0.1 debugging ports
    "tab_titles": True,  # false: list tabs by origin only
    "control": False,  # poll and run owner Close/Stop commands (needs BOTROLL_HEALTH_CONTROL on server)
    # Heavy jobs an owner may Stop: [executable] or [executable, subcommand], matched on argv[0]'s
    # basename (after a node/nodejs launcher), never anywhere in the command line.
    "stop_jobs": None,
}
DEFAULT_STOP_JOBS = [["ffmpeg"], ["vitest"], ["next", "build"], ["npm", "install"], ["npm", "ci"],
                     ["npm", "i"], ["npm", "run"]]


def load_config(file: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if file:
        with open(file, encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError(f"{file}: expected a JSON object")
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"{file}: unknown settings {', '.join(sorted(unknown))}")
        cfg.update(user)
    for j in cfg["jobs"]:
        re.compile(j["match"])
    if cfg.get("profile_resolve") is not None and not isinstance(cfg["profile_resolve"], str):
        raise ValueError("profile_resolve must be a command string or null")
    if cfg.get("profile_resolve") == "":
        cfg["profile_resolve"] = None
    for key in ("names_file", "display_names_dir"):
        if cfg.get(key) is not None and not isinstance(cfg[key], str):
            raise ValueError(f"{key} must be a path or null")
        if cfg.get(key) == "":
            cfg[key] = None
    cfg["control"] = cfg.get("control") is True
    stop = cfg.get("stop_jobs")
    if stop is None:
        cfg["stop_jobs"] = [list(p) for p in DEFAULT_STOP_JOBS]
    elif not (isinstance(stop, list) and all(isinstance(p, list) and 1 <= len(p) <= 2
                                             and all(isinstance(x, str) and x for x in p) for p in stop)):
        raise ValueError("stop_jobs must be a list of [executable] or [executable, subcommand]")
    return cfg


# Parsers -----------------------------------------------------------------------

def parse_meminfo(text: str) -> dict:
    """MemTotal and MemAvailable in MiB, from /proc/meminfo."""
    kb = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            kb[m[1]] = int(m[2])
    if "MemTotal" not in kb:
        raise ValueError("no MemTotal in meminfo")
    avail = kb.get("MemAvailable", kb.get("MemFree", 0) + kb.get("Cached", 0))
    return {"total_mb": round(kb["MemTotal"] / 1024), "available_mb": round(avail / 1024),
            "swap_total_mb": round(kb.get("SwapTotal", 0) / 1024), "swap_free_mb": round(kb.get("SwapFree", 0) / 1024)}


def parse_loadavg(text: str) -> list[float]:
    return [float(x) for x in text.split()[:3]]


def parse_ps(text: str) -> list[dict]:
    """Rows of `ps -eo pid=,ppid=,rss=,etimes=,times=,args=`. rss in KiB, etimes and times in seconds."""
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not all(p.isdigit() for p in parts[:5]):
            continue
        pid, ppid, rss, etimes, cpu = map(int, parts[:5])
        rows.append({"pid": pid, "ppid": ppid, "rss_kb": rss, "elapsed_s": etimes, "cpu_s": cpu, "args": parts[5]})
    return rows


def cpu_percent(before: list[dict], after: list[dict], seconds: float) -> dict[int, float]:
    """CPU% per pid over the interval between two ps samples (100 = one core)."""
    prev = {r["pid"]: r["cpu_s"] for r in before}
    return {r["pid"]: round(max(r["cpu_s"] - prev.get(r["pid"], r["cpu_s"]), 0) / seconds * 100, 1)
            for r in after} if seconds > 0 else {}


def flag(args: str, name: str) -> str | None:
    m = re.search(rf"--{re.escape(name)}=(\"[^\"]*\"|\S+)", args)
    return m[1].strip('"') if m else None


def _mb(kb: int) -> int:
    return round(kb / 1024)


def _bot_for(profile: str, mapping: dict) -> str | None:
    full = norm_path(plain_text(profile))
    keys = {norm_path(plain_text(k)): v for k, v in mapping.items() if isinstance(k, str)}
    return keys.get(full) or keys.get(os.path.basename(full))


RESOLVE_TIMEOUT = 2  # seconds for an optional profile_resolve command


def configured_browser(user_data_dir: str, cfg: dict) -> bool:
    """True when the full --user-data-dir path is a key in cfg["browsers"]. A last-segment key still
    names the browser, but never makes it closable: two folders can share a name."""
    mapping = cfg.get("browsers") or {}
    full = norm_path(user_data_dir)
    return full.startswith("/") and any(isinstance(k, str) and norm_path(k) == full for k in mapping)


NAME_MAX = 64
NAME_FILE_MAX = 64 * 1024


# Invisible or look-alike-making characters beyond categories Cc/Cf (which covers bidi controls,
# zero-width characters, the BOM, the soft hyphen and tag characters): the combining grapheme joiner,
# Hangul fillers, and variation selectors (standard, supplementary and Mongolian).
# U+2800 (braille blank) renders as an empty cell, so it can pad or fake an empty name.
# Deliberately NOT stripped: confusables (Cyrillic "а" for Latin "a") and combining marks. Both are
# real letters in many scripts, so removing them would mangle legitimate names; names are labels
# only (Close is decided by the configured full path, never by a name), so a look-alike can't gain access.
INVISIBLE_EXTRA = re.compile("[\u034f\u115f\u1160\u2800\u3164\uffa0\u180b-\u180f\ufe00-\ufe0f"
                             "\U000e0000-\U000e007f\U000e0100-\U000e01ef]")


def plain_text(text) -> str:
    """Text with control, format (Cf) and other invisible characters removed, so a name shows as written."""
    import unicodedata
    text = INVISIBLE_EXTRA.sub("", text if isinstance(text, str) else "")
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))


def norm_path(path: str) -> str:
    """A profile path in normal form: no trailing slash, no "." or "x/.." parts ("/" stays "/", "" stays "")."""
    return os.path.normpath(path) if path else path


def _clean_name(text: str) -> str | None:
    text = " ".join(plain_text(text).split())
    return text if text and len(text) <= NAME_MAX else None


def _read_small(path: str, limit: int = NAME_FILE_MAX) -> str | None:
    """Read at most `limit` bytes of a small regular file, without blocking. Refused: symlinks (the file or
    any parent directory), hard links, FIFOs, devices and directories."""
    import stat
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.realpath(path) != path:
        return None
    try:
        fd = os.open(path,
                     os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def names_from_file(path: str | None) -> dict:
    """profile → name from a "profile<TAB>name" file. Blank lines and # comments are skipped."""
    out: dict = {}
    for line in (_read_small(path) or "").splitlines() if path else []:
        if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
            continue
        key, name = line.split("\t", 1)
        key, name = norm_path(plain_text(key).strip()), _clean_name(name)
        if key and name:
            out[key] = name
    return out


def _display_of(pid: int | None) -> str | None:
    """The display number from a process's own DISPLAY (":3" or ":3.0" → "3")."""
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read(256 * 1024).split(b"\0")
    except OSError:
        return None
    for kv in env:
        if kv.startswith(b"DISPLAY="):
            m = re.fullmatch(rb"[\w.-]*:(\d+)(\.\d+)?", kv[8:])
            return m[1].decode() if m else None
    return None


def display_name(profile: str, cfg: dict, pid: int | None = None) -> str | None:
    """Name written for the browser's display: <display_names_dir>/<N>, first line."""
    folder = cfg.get("display_names_dir")
    if not folder:
        return None
    num = _display_of(pid)
    if num is None:
        m = re.fullmatch(r"[^\W\d_][\w.-]*?-(\d{1,4})", os.path.basename(norm_path(plain_text(profile))))
        num = m[1] if m else None
    if num is None:
        return None
    text = _read_small(os.path.join(os.path.expanduser(folder), num), 1024)
    return _clean_name(text.splitlines()[0]) if text and text.splitlines() else None


def profile_label(user_data_dir: str) -> str | None:
    """The name Chrome shows for the profile (Local State → profile.info_cache), if any."""
    raw = _read_small(os.path.join(user_data_dir, "Local State"), 4 * 1024 * 1024)
    try:
        info = json.loads(raw)["profile"]["info_cache"] if raw else None
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(info, dict) or not info:
        return None
    entry = info.get("Default") if isinstance(info.get("Default"), dict) else next(
        (v for v in info.values() if isinstance(v, dict)), None)
    name = _clean_name(str(entry.get("name") or "")) if entry else None
    return None if name and DEFAULT_LABEL.fullmatch(name) else name  # Chrome's placeholder says nothing


DEFAULT_LABEL = re.compile(r"Your Chrome|Person \d+|Profile \d+|Default", re.I)


SALT_FILE = "~/.botroll/health-salt"
_salt: bytes | None = None


def _install_salt() -> bytes:
    """A random per-install key (0600), so a profile id can't be computed from a guessed path.
    The file is opened without following symlinks; a missing, short or unreadable file is (re)created, and an
    existing one is tightened to 0600 (or replaced if that fails). Only if writing fails is a per-process key
    used, with a warning (ids then change on restart)."""
    global _salt
    if _salt is None:
        _salt = _load_or_make_salt(os.path.expanduser(SALT_FILE))
    return _salt


def _load_or_make_salt(path: str) -> bytes:
    import stat
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
        try:
            st = os.fstat(fd)
            ok = stat.S_ISREG(st.st_mode)
            if ok and st.st_mode & 0o077:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    ok = False
            data = os.read(fd, 64) if ok else b""
        finally:
            os.close(fd)
        if len(data) >= 16:
            return data
    except OSError:
        pass
    salt = os.urandom(32)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(salt)
            os.replace(tmp, path)  # replaces a symlink or bad file itself, never what it points at
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as err:
        log.warning("botroll: could not write %s (%s); using a temporary key, profile ids will change on restart",
                    path, err)
    return salt


def profile_id(user_data_dir: str) -> str:
    """A short, stable id for a profile path (HMAC with the per-install salt), so two folders with the same name
    can be told apart on the board without sending or revealing the path itself."""
    import hashlib
    import hmac
    return hmac.new(_install_salt(), norm_path(user_data_dir).encode("utf-8", "surrogatepass"),
                    hashlib.sha256).hexdigest()[:8]


def resolve_profile(profile: str, cfg: dict, cache: dict | None = None, pid: int | None = None) -> str | None:
    """Map a user-data-dir to a bot name. Never invents a name. In order:
    the browsers map, names_file, the optional profile_resolve command, then the per-display name file."""
    mapped = _bot_for(profile, cfg.get("browsers") or {})
    if mapped:
        return mapped
    if cfg.get("names_file"):
        names = cache.get("\0names") if cache is not None and "\0names" in cache else names_from_file(cfg["names_file"])
        if cache is not None:
            cache["\0names"] = names
        mapped = _bot_for(profile, names)
        if mapped:
            return mapped
    cmd = cfg.get("profile_resolve")
    if cmd:
        name = _run_resolver(profile, cmd, cache)
        if name:
            return name
    return display_name(profile, cfg, pid)


def _run_resolver(profile: str, cmd, cache: dict | None) -> str | None:
    key = profile
    if cache is not None and key in cache:
        return cache[key]
    name = None
    try:
        r = subprocess.run(cmd if isinstance(cmd, list) else ["/bin/bash", "-c", cmd, "_", profile],
                           input=profile + "\n", capture_output=True, text=True, timeout=RESOLVE_TIMEOUT, check=False)
        if r.returncode == 0:
            line = (r.stdout or "").splitlines()[:1]
            text = line[0].strip() if line else ""
            if text and len(text) <= 64 and "\0" not in text:
                name = text
    except (OSError, subprocess.TimeoutExpired):
        name = None
    if cache is not None:
        cache[key] = name
    return name


def group_browsers(rows: list[dict], cpu: dict, cfg: dict, at: float | None = None) -> list[dict]:
    """Chrome-family processes grouped by --user-data-dir, heaviest first.

    Each group includes main_pid / started_at (unix) for the browser main process when found,
    so an owner Close command can target it without PID reuse.
    """
    match = re.compile(cfg["browser_match"])
    groups: dict[str, dict] = {}
    cache: dict = {}
    now = at if at is not None else time.time()
    for r in rows:
        exe = r["args"].split(" --", 1)[0]
        if not match.search(exe + " "):
            continue
        full = norm_path(flag(r["args"], "user-data-dir") or "") or "(default profile)"
        profile = os.path.basename(full.rstrip("/")) or full
        g = groups.setdefault(full, {"profile": profile, "bot": None, "label": None, "_full": full,
                                        "procs": 0, "rss_mb": 0, "cpu": 0.0, "port": None,
                                        "main_pid": None, "started_at": None,
                                        "closable": configured_browser(full, cfg),
                                        "profile_id": profile_id(full), "_kb": 0})
        g["procs"] += 1
        g["_kb"] += r["rss_kb"]
        g["cpu"] = round(g["cpu"] + cpu.get(r["pid"], 0.0), 1)
        port = flag(r["args"], "remote-debugging-port")
        if port and port.isdigit() and int(port) > 0:
            g["port"] = int(port)
        # Main process: no --type= (zygote/renderer/utility)
        if "--type=" not in r["args"] and (g["main_pid"] is None or r["elapsed_s"] >= (now - (g["started_at"] or now))):
            g["main_pid"] = r["pid"]
            g["started_at"] = max(0, int(now - r["elapsed_s"]))
    out = []
    for g in groups.values():
        full = g.pop("_full")
        g["bot"] = resolve_profile(full, cfg, cache, g["main_pid"])
        if not g["bot"] and full != "(default profile)":
            g["label"] = profile_label(full)
        g["rss_mb"] = _mb(g.pop("_kb"))
        out.append(g)
    return sorted(out, key=lambda g: -g["rss_mb"])


def redact(args: str, label: str) -> str:
    """Only the executable's name and a known label: arguments can carry tokens, paths and URLs."""
    exe = os.path.basename(args.split(" ", 1)[0])[:40]
    return f"{exe} ({label})" if label and label != exe else exe


def helpers(rows: list[dict], cpu: dict, cfg: dict, at: float | None = None) -> list[dict]:
    """node processes whose command line matches helper_match, flagged stale past helper_stale_hours."""
    pat, stale = cfg["helper_match"], cfg["helper_stale_hours"] * 3600
    now = at if at is not None else time.time()
    out = []
    for r in rows:
        exe = os.path.basename(r["args"].split(" ", 1)[0])
        if exe in ("node", "nodejs") and pat and pat in r["args"]:
            port = re.search(r"(?:127\.0\.0\.1|localhost):(\d+)", r["args"])
            out.append({"pid": r["pid"], "rss_mb": _mb(r["rss_kb"]), "cpu": cpu.get(r["pid"], 0.0),
                        "elapsed_s": r["elapsed_s"], "started_at": max(0, int(now - r["elapsed_s"])),
                        "stale": r["elapsed_s"] > stale,
                        "port": int(port[1]) if port else None, "cmd": redact(r["args"], "helper")})
    return sorted(out, key=lambda h: -h["rss_mb"])


def heavy_jobs(rows: list[dict], cpu: dict, cfg: dict, at: float | None = None) -> list[dict]:
    """Heavy jobs by kind. A job's child that matches the same kind (npm → node → vitest) counts once."""
    pats = [(j, re.compile(j["match"])) for j in cfg["jobs"]]
    now = at if at is not None else time.time()
    found = {}
    for r in rows:
        for j, p in pats:
            if p.search(r["args"]):
                found[r["pid"]] = {"pid": r["pid"], "ppid": r["ppid"], "kind": j["name"],
                                   "expect_s": int(j["expect_minutes"] * 60), "elapsed_s": r["elapsed_s"],
                                   "started_at": max(0, int(now - r["elapsed_s"])),
                                   "rss_mb": _mb(r["rss_kb"]), "cpu": cpu.get(r["pid"], 0.0), "cmd": redact(r["args"], j["name"])}
                break
    parents = {r["pid"]: r["ppid"] for r in rows}

    def nested(job) -> bool:
        p, seen = job["ppid"], set()
        while p in parents and p not in seen:
            seen.add(p)
            if p in found:
                return True
            p = parents[p]
        return False
    jobs = [j for j in found.values() if not nested(j)]
    for j in jobs:
        del j["ppid"]
    return sorted(jobs, key=lambda j: -j["elapsed_s"])


def overlaps(jobs: list[dict]) -> list[dict]:
    if len(jobs) < 2:
        return []
    kinds = ", ".join(f'{j["kind"]} ({j["pid"]})' for j in jobs)
    return [{"kind": "overlap", "text": f"{len(jobs)} heavy jobs at once: {kinds}"}]


def stalls(jobs: list[dict], hlp: list[dict], load1: float, cores: int) -> list[dict]:
    """Anything past its expected runtime while the 1-minute load is over the core count."""
    if load1 <= cores:
        return []
    out = [{"kind": "stall", "pid": j["pid"],
            "text": f'{j["kind"]} ({j["pid"]}) running {duration(j["elapsed_s"])}, expected under '
                    f'{duration(j["expect_s"])}, with load {load1:.1f} on {cores} cores'}
           for j in jobs if j["elapsed_s"] > j["expect_s"]]
    out += [{"kind": "stall", "pid": h["pid"],
             "text": f'helper {h["pid"]} running {duration(h["elapsed_s"])} with load {load1:.1f} on {cores} cores'}
            for h in hlp if h["stale"]]
    return out


def duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h < 24 else f"{h // 24}d {h % 24}h"


TITLE_MAX = 60


def origin(url: str) -> str:
    """scheme://host[:port] with any user:pass@ dropped; paths and queries can hold secrets too."""
    try:
        u = urlsplit(url)
        host, port = u.hostname or "", u.port
    except ValueError:
        return ""
    if not host:
        return f"{u.scheme}:" if u.scheme else ""
    if ":" in host:
        host = f"[{host}]"
    return f"{u.scheme}://{host}" + (f":{port}" if port else "")


def _decode(text: str) -> str:
    """Percent-decode until stable, so %2541 can't hide an A behind two layers."""
    for _ in range(10):
        out = unquote(text)
        if out == text:
            break
        text = out
    return text


def safe_title(title: str, url: str) -> str:
    """The tab's title, or its host ("Untitled" without one) whenever the title could carry part of the URL.

    Safe by construction rather than by pattern: after fully decoding both, the title is dropped if
    (a) it starts with a scheme such as https:, data:, blob:, file:, about: or chrome:;
    (b) it starts with the host (so also host:port/...), an IPv6 host with or without its brackets;
    (c) it contains any path, query or fragment segment of the URL of 4+ characters, case-insensitively;
    (d) the URL isn't http or https.
    (c) also drops honest titles that repeat the path, e.g. "GitHub - saadkamal/botroll" on
    github.com/saadkamal/botroll shows as "github.com". That's deliberate: safety over detail."""
    t, u = _decode(title).strip(), _decode(url).strip()
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower()
    except ValueError:
        parts, host = None, ""
    fallback = host or "Untitled"
    if not t or parts is None or parts.scheme.lower() not in ("http", "https"):  # (d)
        return fallback
    low = t.lower()
    segments = [x for x in re.split(r"[/?&=#;]+", f"{parts.path}?{parts.query}#{parts.fragment}") if len(x) >= 4]
    if (re.match(r"\w+:", t)  # (a)
            or (host and (low.startswith(host) or low.startswith(f"[{host}]")))  # (b), IPv6 with brackets too
            or any(seg.lower() in low for seg in segments)):  # (c)
        return fallback
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1] + "…"


def parse_tabs(raw: bytes, titles: bool = True) -> list[dict]:
    """DevTools /json/list → page tabs: origin only, and a safe title cut to 60 characters (or dropped)."""
    data = json.loads(raw)
    out = []
    for t in data if isinstance(data, list) else []:
        if isinstance(t, dict) and t.get("type") == "page":
            url = str(t.get("url", ""))
            out.append({"title": safe_title(str(t.get("title", "")), url) if titles else "",
                        "origin": origin(url)[:120]})
    return out


def build(meminfo: str, loadavg: str, cores: int, before: list[dict], after: list[dict], interval: float,
          cfg: dict, tabs=None, ts: float | None = None, host: str | None = None) -> dict:
    """One snapshot from raw readings. tabs is a function port → list, or None to skip."""
    ts_ = int(ts if ts is not None else time.time())
    cpu = cpu_percent(before, after, interval)
    load = parse_loadavg(loadavg)
    br = group_browsers(after, cpu, cfg, at=ts_)
    hl = helpers(after, cpu, cfg, at=ts_)
    jobs = heavy_jobs(after, cpu, cfg, at=ts_)
    if tabs:
        for g in br:
            found = tabs(g["port"]) if g["port"] else None
            g["tabs"] = found[:40] if found is not None else None
    alerts = overlaps(jobs) + stalls(jobs, hl, load[0], cores)
    alerts += [{"kind": "stale-helper", "pid": h["pid"],
                "text": f'helper {h["pid"]} has run {duration(h["elapsed_s"])} holding {h["rss_mb"]} MB'}
               for h in hl if h["stale"] and h["pid"] not in {a.get("pid") for a in alerts}]
    return fit({"v": 1, "ts": ts_, "host": host or "box", "cores": cores,
                "load": load, "mem": parse_meminfo(meminfo), "browsers": br, "helpers": hl, "jobs": jobs,
                "alerts": alerts})


# Live sampling -----------------------------------------------------------------

PS = ["ps", "-eo", "pid=,ppid=,rss=,etimes=,times=,args="]


def _ps() -> list[dict]:
    return parse_ps(subprocess.run(PS, capture_output=True, text=True, timeout=20, check=True).stdout)


def _tabs(port: int, titles: bool = True) -> list[dict] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
            return parse_tabs(r.read(1024 * 1024), titles)
    except (OSError, ValueError):
        return None


def sample(cfg: dict, interval: float = 2.0) -> dict:
    if not os.path.exists("/proc/meminfo"):
        raise ValueError("botroll health reads /proc, so it runs on Linux only")
    before = _ps()
    t0 = time.monotonic()
    time.sleep(interval)
    after = _ps()
    took = time.monotonic() - t0
    with open("/proc/meminfo") as f:
        mem = f.read()
    with open("/proc/loadavg") as f:
        load = f.read()
    snap = build(mem, load, os.cpu_count() or 1, before, after, took, cfg,
                 tabs=(lambda port: _tabs(port, cfg["tab_titles"])) if cfg["tabs"] else None, host=cfg["name"] or socket.gethostname())
    # Use the kernel's start time (what the kill guard compares against), not ps's rounded elapsed time.
    for row, key in ([(g, "main_pid") for g in snap["browsers"]] + [(h, "pid") for h in snap["helpers"]]
                     + [(j, "pid") for j in snap["jobs"]]):
        got = _proc_started_at(row[key]) if row.get(key) else None
        if got is not None:
            row["started_at"] = got
    return snap


# The rolling store ---------------------------------------------------------------

class Rejected(ValueError):
    """A well-formed sample the store won't take: out of the time window, or a replay. HTTP 422 / 409."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


FUTURE_SECONDS = 60
MAX_STORE = 4 * 1024 * 1024  # bytes for the whole rolling store; oldest samples go first
MAX_LIST = 64
TEXT = re.compile(r"[^\x00-\x1f\x7f]*")


def _num(v, lo: float = 0, hi: float = 1e12) -> float:
    if type(v) not in (int, float) or v != v or v in (float("inf"), float("-inf")) or not lo <= v <= hi:
        raise ValueError(f"expected a number, got {v!r}"[:80])
    return v


def _int(v, lo: int = 0, hi: int = 2**31) -> int:
    if type(v) is not int:
        raise ValueError(f"expected an integer, got {v!r}"[:80])
    return int(_num(v, lo, hi))


def _bool(v) -> bool:
    if type(v) is not bool:
        raise ValueError("expected true or false")
    return v


def _str(v, n: int = 120, empty: bool = True) -> str:
    if not isinstance(v, str) or len(v) > n or not TEXT.fullmatch(v) or (not empty and not v):
        raise ValueError("expected a short single-line string")
    return v


def _opt(v, f, *a):
    return None if v is None else f(v, *a)


def _list(v, f) -> list:
    if not isinstance(v, list) or len(v) > MAX_LIST:
        raise ValueError(f"expected a list of at most {MAX_LIST}")
    return [f(x) for x in v]


def _obj(v) -> dict:
    if not isinstance(v, dict):
        raise ValueError("expected an object")
    return v


def _tab(t) -> dict:
    t = _obj(t)
    return {"title": _str(t.get("title", ""), TITLE_MAX), "origin": _str(t.get("origin", ""))}


def _browser(g) -> dict:
    g = _obj(g)
    out = {"profile": plain_text(os.path.basename(_str(g["profile"], 200).rstrip("/"))[:80]),
           "bot": _opt(g.get("bot"), _str, 60) and (plain_text(g["bot"]).strip() or None),
           "label": _opt(g.get("label"), _str, NAME_MAX) and (plain_text(g["label"]).strip() or None),
           "procs": _int(g["procs"], 0, 100000), "rss_mb": _num(g["rss_mb"]), "cpu": _num(g["cpu"], 0, 1e6),
           "port": _opt(g.get("port"), _int, 1, 65535),
           "main_pid": _opt(g.get("main_pid"), _int, 1),
           "started_at": (_opt(g.get("started_at"), _int, 0) if isinstance(g.get("started_at"), int) and g["started_at"] >= 0 else None),
           "closable": g.get("closable") is True,
           "profile_id": g["profile_id"] if isinstance(g.get("profile_id"), str)
           and re.fullmatch(r"[0-9a-f]{8}", g["profile_id"]) is not None else None}
    if "tabs" in g:
        out["tabs"] = _opt(g["tabs"], _list, _tab)
    return out


def _helper(h) -> dict:
    h = _obj(h)
    return {"pid": _int(h["pid"], 1), "rss_mb": _num(h["rss_mb"]), "cpu": _num(h["cpu"], 0, 1e6),
            "elapsed_s": _num(h["elapsed_s"]),
            "started_at": (_opt(h.get("started_at"), _int, 0) if isinstance(h.get("started_at"), int) and h["started_at"] >= 0 else None),
            "stale": _bool(h["stale"]), "port": _opt(h.get("port"), _int, 1, 65535),
            "cmd": _str(h.get("cmd", ""), 80)}


def _job(j) -> dict:
    j = _obj(j)
    return {"pid": _int(j["pid"], 1), "kind": _str(j["kind"], 60, False), "expect_s": _num(j["expect_s"]),
            "elapsed_s": _num(j["elapsed_s"]),
            "started_at": (_opt(j.get("started_at"), _int, 0) if isinstance(j.get("started_at"), int) and j["started_at"] >= 0 else None),
            "rss_mb": _num(j["rss_mb"]), "cpu": _num(j["cpu"], 0, 1e6),
            "cmd": _str(j.get("cmd", ""), 80)}


def _alert(a) -> dict:
    a = _obj(a)
    return {"kind": _str(a["kind"], 20, False), "text": _str(a["text"], 300),
            **({"pid": _opt(a["pid"], _int, 1)} if "pid" in a else {})}


def clean(snap) -> dict:
    """A strictly typed copy of a snapshot with unknown fields dropped, or ValueError."""
    try:
        s = _obj(snap)
        if s.get("v") != 1 or type(s.get("v")) is not int:
            raise ValueError("v must be 1")
        mem = _obj(s["mem"])
        load = s["load"]
        if not isinstance(load, list) or len(load) != 3:
            raise ValueError("load must be three numbers")
        return {"v": 1, "ts": _int(s["ts"], 0, 2**40), "host": _str(s["host"], 80), "cores": _int(s["cores"], 1, 4096),
                "load": [_num(x, 0, 1e5) for x in load],
                "mem": {k: _int(mem[k], 0, 2**40) for k in ("total_mb", "available_mb", "swap_total_mb", "swap_free_mb")
                        if k in mem or k in ("total_mb", "available_mb")},
                "browsers": _list(s["browsers"], _browser), "helpers": _list(s["helpers"], _helper),
                "jobs": _list(s["jobs"], _job), "alerts": _list(s["alerts"], _alert)}
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"not a box-health snapshot: missing or bad {e}") from None


LIMITS = {"host": 80, "profile": 80, "bot": 60, "cmd": 80, "kind": 20, "text": 300, "title": TITLE_MAX,
          "origin": 120}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clip(v, key: str = ""):
    if isinstance(v, str):
        v = _CTRL.sub(" ", v)
        n = LIMITS.get(key, 120)
        return v if len(v) <= n else v[: n - 1] + "…"
    if isinstance(v, list):
        return [_clip(x, key) for x in v[:MAX_LIST]]
    if isinstance(v, dict):
        return {k: _clip(x, k) for k, x in v.items()}
    return v


def fit(snap: dict) -> dict:
    """Apply the server's caps on the sending side (string lengths, list sizes, sample size), so a busy box
    sends a smaller sample instead of one the server refuses."""
    snap = _clip(snap)
    snap["alerts"] = [a | {"kind": a["kind"][:20]} for a in snap["alerts"]]
    size = lambda: len(json.dumps(snap))  # noqa: E731
    for g in snap["browsers"]:
        if size() <= MAX_SNAPSHOT:
            break
        if g.get("tabs"):
            g["tabs"] = g["tabs"][:5]
    for key in ("alerts", "jobs", "helpers", "browsers"):
        while size() > MAX_SNAPSHOT and len(snap[key]) > 1:
            snap[key] = snap[key][: len(snap[key]) // 2]
    if size() > MAX_SNAPSHOT:
        for g in snap["browsers"]:
            g.pop("tabs", None)
    return snap


def valid(snap) -> bool:
    try:
        clean(snap)
        return True
    except ValueError:
        return False


def prune(samples: list[dict], at: float) -> list[dict]:
    keep = sorted((s for s in samples if at - KEEP_SECONDS <= s["ts"] <= at + FUTURE_SECONDS),
                  key=lambda s: s["ts"])[-MAX_SAMPLES:]
    size = sum(len(json.dumps(s, separators=(",", ":"))) + 1 for s in keep)
    while keep and size > MAX_STORE:
        size -= len(json.dumps(keep.pop(0), separators=(",", ":"))) + 1
    return keep


def load(directory: str) -> list[dict]:
    """The stored samples. A bad one (hand-edited, or from an older version) is skipped, never fatal."""
    try:
        with open(os.path.join(directory, HEALTH), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return []
    out = []
    for s in data if isinstance(data, list) else []:
        try:
            out.append(clean(s))
        except ValueError:
            continue
    return out


def append(directory: str, snap: dict, at: float | None = None) -> int:
    from . import locked, write_atomic
    snap = clean(snap)
    if len(json.dumps(snap)) > MAX_SNAPSHOT:
        raise ValueError(f"snapshot over {MAX_SNAPSHOT} bytes")
    at = at if at is not None else time.time()
    if snap["ts"] > at + FUTURE_SECONDS:
        raise Rejected("sample time is more than 60s in the future", 422)
    if snap["ts"] < at - KEEP_SECONDS:
        raise Rejected("sample time is older than the 24h window", 422)
    os.makedirs(directory, exist_ok=True)
    with locked(directory):
        samples = load(directory)
        last = max((x["ts"] for x in samples if x["host"] == snap["host"]), default=None)
        if last is not None and snap["ts"] <= last:
            raise Rejected(f"sample time is not newer than the last one from {snap['host']}", 409)
        samples = prune(samples + [snap], at)
        write_atomic(os.path.join(directory, HEALTH), json.dumps(samples, separators=(",", ":")))
    return len(samples)


def push(url: str, token: str, snap: dict) -> None:
    from .serve import _NoRedirect, send
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("refusing to send the push token without https")
    req = urllib.request.Request(url.rstrip("/") + "/api/health", data=json.dumps(snap).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    # Not idempotent: the server keeps every sample. A lost reply is reported, and the next sample follows.
    send(urllib.request.build_opener(_NoRedirect), req, idempotent=False)


# Board view helpers --------------------------------------------------------------

BUCKET_MINS = (5, 10, 15, 30, 60)


def thresholds(env=os.environ) -> dict:
    def num(name, default):
        try:
            return float(env.get(name, default))
        except ValueError:
            return float(default)
    raw = str(env.get("BOTROLL_HEALTH_BUCKET_MIN", "30")).strip()
    try:
        bucket = int(raw)
    except ValueError:
        bucket = 30
    if bucket not in BUCKET_MINS:
        bucket = 30
    return {"mem_amber": num("BOTROLL_HEALTH_MEM_AMBER", 25), "mem_red": num("BOTROLL_HEALTH_MEM_RED", 10),
            "load_amber": num("BOTROLL_HEALTH_LOAD_AMBER", 1.0), "load_red": num("BOTROLL_HEALTH_LOAD_RED", 2.0),
            "bucket_min": bucket}


def level(value: float, amber: float, red: float, lower_is_worse: bool = False) -> str:
    if lower_is_worse:
        return "red" if value <= red else "amber" if value <= amber else "ok"
    return "red" if value >= red else "amber" if value >= amber else "ok"


def top_browser(snap: dict) -> dict | None:
    return max(snap["browsers"], key=lambda g: (g.get("cpu") or 0, g.get("rss_mb") or 0), default=None)


def bucket_peaks(samples: list[dict], at: float, bucket_min: int = 30, hours: int = 24) -> list[dict | None]:
    """Per bucket (bucket_min minutes), oldest first: peak load ratio and the busiest browser then.

    The collector keeps ~60s samples for 24h, so 5–60 minute bars all have enough resolution.
    """
    if bucket_min not in BUCKET_MINS:
        bucket_min = 30
    width = bucket_min * 60
    n = max(int(hours * 3600 // width), 1)
    start = at - n * width
    out: list[dict | None] = [None] * n
    for s in samples:
        i = min(int((s["ts"] - start) // width), n - 1)
        if 0 <= i and s["ts"] <= at + FUTURE_SECONDS:
            ratio = s["load"][0] / s["cores"]
            if out[i] is None or ratio > out[i]["ratio"]:
                b = top_browser(s)
                out[i] = {"ts": s["ts"], "ratio": ratio, "load": s["load"][0], "cores": s["cores"],
                          "avail_pct": 100 * s["mem"]["available_mb"] / max(s["mem"]["total_mb"], 1),
                          "bot": (b.get("bot") or os.path.basename(str(b.get("profile", "")))) if b else None}
    return out


def hourly_peaks(samples: list[dict], at: float, hours: int = 24) -> list[dict | None]:
    """Back-compat: 60-minute buckets."""
    return bucket_peaks(samples, at, bucket_min=60, hours=hours)




# Owner remote control (pull model) ----------------------------------------------

COMMANDS = ".health-commands.json"  # pending + recent results
AUDIT = ".health-commands.jsonl"
COMMAND_TTL = 120  # seconds; unused commands expire
COMMAND_GAP = 10  # seconds between commands on the same target
CONTROL_KINDS = ("close-browser", "stop-helper", "stop-job")
KILL_WAIT = 10  # SIGTERM, then SIGKILL after this many seconds


def _cmd_path(data: str) -> str:
    return os.path.join(data, COMMANDS)


def load_commands(data: str) -> list[dict]:
    path = _cmd_path(data)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.loads(f.read())
    except (OSError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def _save_commands(data: str, rows: list[dict]) -> None:
    from . import write_atomic
    write_atomic(_cmd_path(data), json.dumps(rows, separators=(",", ":")))


def _audit(data: str, row: dict) -> None:
    path = os.path.join(data, AUDIT)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def enqueue_command(data: str, kind: str, target: dict, actor: str = "owner") -> dict:
    """Record an owner command from the latest sample's target. Raises ValueError on bad input."""
    if kind not in CONTROL_KINDS:
        raise ValueError("unknown command kind")
    target = target if isinstance(target, dict) else {}
    now = int(time.time())
    entry = {"id": f"{now}-{os.urandom(4).hex()}", "kind": kind, "target": target,
             "created_at": now, "expires_at": now + COMMAND_TTL, "status": "pending",
             "result": None, "actor": actor, "nonce": os.urandom(16).hex()}
    # validate shape
    if kind == "close-browser":
        if not isinstance(target.get("profile"), str) or not target["profile"]:
            raise ValueError("close-browser needs target.profile")
        if not isinstance(target.get("pid"), int) or not isinstance(target.get("started_at"), int):
            raise ValueError("close-browser needs target.pid and started_at from the latest sample")
    else:
        if not isinstance(target.get("pid"), int) or not isinstance(target.get("started_at"), int):
            raise ValueError("stop needs target.pid and started_at from the latest sample")
    from . import locked
    with locked(data):
        rows = [r for r in load_commands(data) if r.get("expires_at", 0) > now - COMMAND_TTL]
        # rate limit same target
        key = (kind, target.get("profile"), target.get("pid"), target.get("started_at"))
        for r in rows:
            t = r.get("target") or {}
            if (r.get("status") == "pending" and (r.get("kind"), t.get("profile"), t.get("pid"),
                                                  t.get("started_at")) == key
                    and now - int(r.get("created_at") or 0) < COMMAND_GAP):
                raise ValueError("rate limited")
        rows.append(entry)
        # keep a short tail
        rows = rows[-200:]
        _save_commands(data, rows)
    _audit(data, {"event": "enqueue", **{k: v for k, v in entry.items() if k != "nonce"}})
    return entry


def pending_commands(data: str, at: float | None = None) -> list[dict]:
    """Commands still pending and not expired, oldest first."""
    now = int(at if at is not None else time.time())
    return [r for r in load_commands(data)
            if r.get("status") == "pending" and int(r.get("expires_at") or 0) >= now]


def clear_pending(data: str, reason: str = "control off") -> int:
    """Cancel every pending command (control was turned off). Returns how many were cancelled."""
    now = int(time.time())
    from . import locked
    with locked(data):
        rows = load_commands(data)
        n = 0
        for r in rows:
            if r.get("status") == "pending":
                r["status"], r["result"], n = "cancelled", {"at": now, "detail": reason}, n + 1
        if n:
            _save_commands(data, rows)
    if n:
        _audit(data, {"event": "clear", "count": n, "detail": reason})
    return n


def finish_command(data: str, cid: str, status: str, detail: str = "", nonce: str | None = None) -> dict | None:
    """Record a result. The nonce must match the one issued with the command, else nothing changes.

    The nonce ties a result to the exact command that was handed out, so a result can't be posted
    for a guessed id or replayed onto another command. It is not a second credential: anyone holding
    the push token can also GET pending commands (and their nonces), so the push token stays the
    trust boundary for the collector. Keep it on the collector host only.
    """
    import hmac
    now = int(time.time())
    from . import locked
    with locked(data):
        rows = load_commands(data)
        found = None
        for r in rows:
            if r.get("id") == cid:
                if not (isinstance(nonce, str) and isinstance(r.get("nonce"), str)
                        and hmac.compare_digest(nonce.encode(), r["nonce"].encode())):
                    return None
                if r.get("status") != "pending":
                    return r
                r["status"] = status
                r["result"] = {"at": now, "detail": (detail or "")[:200]}
                found = r
                break
        if found:
            _save_commands(data, rows)
            _audit(data, {"event": "finish", "id": cid, "status": status, "detail": detail[:200]})
        return found


def recent_results(data: str, limit: int = 20) -> list[dict]:
    rows = [{k: v for k, v in r.items() if k != "nonce"} for r in load_commands(data) if r.get("status") != "pending"]
    return rows[-limit:]


def _proc_started_at(pid: int) -> int | None:
    """Unix start time from /proc/PID (starttime clock ticks → approx), or None if gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            st = f.read()
        # comm can have spaces/parens; split after last )
        right = st.rsplit(")", 1)[-1].split()
        start_ticks = int(right[19])  # starttime is field 22, index 19 after ppid fields in right side... 
        # Actually after ')': state ppid pgrp session tty tpgid flags minflt etc — starttime is index 19 (field 22)
        with open("/proc/stat", encoding="utf-8") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        hz = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK")) if hasattr(os, "sysconf") else 100
        return int(btime + start_ticks / hz)
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def _argv(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _cmdline(pid: int) -> str | None:
    argv = _argv(pid)
    return None if argv is None else " ".join(argv)


START_SKEW = 1  # seconds between the sampled start time and the kernel's


def _alive_match(pid: int, started_at: int, skew: int = START_SKEW) -> bool:
    got = _proc_started_at(pid)
    if got is None:
        return False
    return abs(got - started_at) <= skew


NODE_VALUE_FLAGS = ("-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file",
                    "--title", "--inspect-port", "--conditions", "-C")
NODE_BOOL_FLAGS = ("--experimental-strip-types", "--experimental-transform-types", "--no-experimental-strip-types",
                   "--experimental-detect-module", "--experimental-require-module", "--experimental-wasm-modules",
                   "--experimental-json-modules", "--experimental-global-webcrypto", "--experimental-test-coverage",
                   "--no-experimental-global-navigator", "--frozen-intrinsics",
                   "--trace-exit", "--trace-sigint", "--trace-sync-io", "--zero-fill-buffers",
                   "--report-on-signal", "--report-uncaught-exception", "--report-on-fatalerror",
                   "--watch", "--watch-preserve-output", "--no-network-family-autoselection",
                   "--use-openssl-ca", "--use-bundled-ca", "--openssl-legacy-provider", "--insecure-http-parser",
                   "--no-global-search-paths", "--no-extra-info-on-fatal-exception", "--expose-internals",
                   "--prof", "--cpu-prof", "--heap-prof", "--jitless", "--force-fips", "--enable-fips",
                   "--node-memory-debug", "--no-force-async-hooks-checks",
                   "--experimental-network-imports", "--experimental-permission", "--permission",
                   "--experimental-sqlite", "--experimental-websocket", "--no-experimental-websocket",
                   "--experimental-eventsource", "--experimental-print-required-tla", "--experimental-addon-modules",
                   "--no-warnings", "--no-deprecation", "--trace-warnings", "--trace-deprecation",
                   "--throw-deprecation", "--pending-deprecation", "--enable-source-maps", "--expose-gc",
                   "--experimental-vm-modules", "--preserve-symlinks", "--preserve-symlinks-main",
                   "--abort-on-uncaught-exception", "--trace-uncaught", "--inspect", "--inspect-brk",
                   "--no-experimental-fetch", "--no-addons")
NODE_NO_SCRIPT = ("-e", "--eval", "-p", "--print", "-i", "--interactive", "-", "-c", "--check", "--test", "--run")


def _node_script(args: list[str]) -> int | None:
    """Index of the script node will run, or None (inline code, REPL, stdin). Preloads given with
    -r/--require and friends are skipped, so `node -r ffmpeg x.js` is x.js, never ffmpeg."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return i + 1 if i + 1 < len(args) else None
        if a.split("=", 1)[0] in NODE_NO_SCRIPT:  # --eval=CODE / -p=EXPR run inline code too
            return None
        if a in NODE_VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            if "=" in a or a in NODE_BOOL_FLAGS:
                i += 1
                continue
            return None  # an unknown flag may take a value: don't guess which argument is the script
        return i
    return None


def _program(argv: list[str]) -> tuple[str, list[str]]:
    """(basename of the real program, its arguments). A node/nodejs launcher is looked through, and
    npm-cli.js / vitest.mjs style script names are normalised to npm / vitest."""
    if not argv:
        return "", []
    exe, rest = os.path.basename(argv[0]), argv[1:]
    if exe in ("node", "nodejs"):
        script = _node_script(rest)
        if script is None:
            return exe, []
        exe, rest = os.path.basename(rest[script]), rest[script + 1:]
    return re.sub(r"(-cli)?\.(c|m)?js$", "", exe), rest


def stoppable_job(argv: list[str], cfg: dict) -> bool:
    """True only when argv[0]'s program (and its first argument, when the entry names one) is on stop_jobs."""
    exe, rest = _program(argv)
    for pair in cfg.get("stop_jobs") or DEFAULT_STOP_JOBS:
        if exe == pair[0] and (len(pair) == 1 or (rest[:1] == [pair[1]])):
            return True
    return False


def _in_sample(kind: str, target: dict, sample: dict | None) -> bool:
    """The target must be on the collector's own latest sample (same pid and start time)."""
    if not isinstance(sample, dict):
        return False
    pid, started = target.get("pid"), target.get("started_at")
    if kind == "close-browser":
        return any(g.get("main_pid") == pid and g.get("started_at") == started and g.get("closable") is True
                   and g.get("profile") == target.get("profile") for g in sample.get("browsers") or [])
    rows = sample.get("helpers" if kind == "stop-helper" else "jobs") or []
    return any(r.get("pid") == pid and r.get("started_at") == started for r in rows)


def allowed_target(kind: str, target: dict, cfg: dict, sample: dict | None) -> str | None:
    """None if ok to act; otherwise a short reason to refuse."""
    if kind not in CONTROL_KINDS:
        return "unknown kind"
    if cfg.get("control") is not True:
        return "control off"
    target = target if isinstance(target, dict) else {}
    pid = target.get("pid")
    started = target.get("started_at")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return "bad pid"
    if pid == os.getpid():
        return "refusing collector pid"
    if not isinstance(started, int) or isinstance(started, bool):
        return "missing started_at"
    if not _in_sample(kind, target, sample):
        return "not on the latest local sample"
    if not _alive_match(pid, started):
        return "pid gone or reused"
    argv = _argv(pid) or []
    cmd = " ".join(argv)
    if kind == "close-browser":
        profile = target.get("profile")
        if not isinstance(profile, str) or not profile:
            return "missing profile"
        match = re.compile(cfg.get("browser_match") or DEFAULTS["browser_match"])
        exe = cmd.split(" --", 1)[0]
        if not match.search(exe + " ") or "--type=" in cmd:
            return "not a browser main process"
        ud = norm_path(flag(cmd, "user-data-dir") or "")
        if not ud or os.path.basename(ud) != profile:
            return "profile mismatch"
        if not configured_browser(ud, cfg):
            return "browser not listed in config"
        return None
    if kind == "stop-helper":
        pat = cfg.get("helper_match") or ""
        exe = os.path.basename(argv[0]) if argv else ""
        if exe not in ("node", "nodejs") or not pat or pat not in cmd:
            return "not an allowed helper"
        return None
    if stoppable_job(argv, cfg):
        return None
    return "not an allowed heavy job"


def terminate(pid: int, wait: float = KILL_WAIT) -> str:
    """SIGTERM, then SIGKILL after wait seconds. Returns a short result string."""
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return "already gone"
    except PermissionError:
        return "permission denied"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "terminated"
        time.sleep(0.2)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "sigkill permission denied"
    return "killed"


def _https_or_local(url: str, what: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"refusing to {what} without https")


def fetch_commands(url: str, token: str) -> list[dict]:
    """GET /api/health/commands with the push token.

    Sent like every other push-token request: no redirects (the token is never resent to another
    URL) and dropped connections retried, since a GET changes nothing on the server."""
    from .serve import _NoRedirect, send
    _https_or_local(url, "fetch commands")
    req = urllib.request.Request(url.rstrip("/") + "/api/health/commands", method="GET",
                                 headers={"Authorization": f"Bearer {token}"})
    data = json.loads(send(urllib.request.build_opener(_NoRedirect), req, idempotent=True, timeout=15).decode())
    if isinstance(data, list):
        return data
    return (data.get("commands") or []) if isinstance(data, dict) else []


def report_command(url: str, token: str, cid: str, status: str, detail: str = "", nonce: str = "") -> None:
    """POST a command's result. Safe to retry after a lost reply: the server records the first result
    for an id (with its nonce) and answers a repeat with the row it already has."""
    from .serve import _NoRedirect, send
    _https_or_local(url, "report")
    body = json.dumps({"id": cid, "nonce": nonce, "status": status, "detail": detail}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/health/commands/result", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    send(urllib.request.build_opener(_NoRedirect), req, idempotent=True, timeout=15)


def run_commands(cfg: dict, commands: list[dict], sample: dict | None = None, at: float | None = None,
                 kill=None) -> list[dict]:
    """Execute allowed commands locally. Returns [{id, nonce, status, detail}, ...].

    Refuses everything when control is off, anything past its expires_at, and any target that is not
    on this collector's own latest sample."""
    now = at if at is not None else time.time()
    kill = kill or terminate
    out = []
    for c in commands if isinstance(commands, list) else []:
        c = c if isinstance(c, dict) else {}
        cid, kind, target = c.get("id"), c.get("kind"), c.get("target") or {}
        res = {"id": cid, "nonce": c.get("nonce") or ""}
        exp = c.get("expires_at")
        if cfg.get("control") is not True:
            reason = "control off"
        elif not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp < now:
            reason = "expired"
        else:
            reason = allowed_target(kind, target, cfg, sample)
        if reason:
            out.append({**res, "status": "refused", "detail": reason})
            continue
        detail = kill(int(target["pid"]))
        out.append({**res, "status": "ok" if detail in ("terminated", "killed", "already gone") else "failed",
                    "detail": detail})
    return out


# The collector loop --------------------------------------------------------------

def stamp(at: float | None = None) -> str:
    """An ISO 8601 time with the machine's UTC offset, e.g. 2026-10-17T22:18:27+11:00."""
    from datetime import datetime
    return datetime.fromtimestamp(time.time() if at is None else at).astimezone().isoformat(timespec="seconds")


class Stamped:
    """A text stream that starts every line written to it with stamp(), so a long-running collector's
    log says when each thing happened. Partial writes are joined: only the start of a line is stamped."""

    def __init__(self, stream, clock=None):
        self._stream, self._clock, self._fresh = stream, clock or time.time, True

    def write(self, text: str) -> int:
        out = []
        for piece in text.splitlines(keepends=True):
            if self._fresh:
                out.append(stamp(self._clock()) + " ")
            out.append(piece)
            self._fresh = piece.endswith(("\n", "\r"))
        self._stream.write("".join(out))
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name):  # flush, fileno, isatty, encoding, ... come from the real stream
        return getattr(self._stream, name)


@contextlib.contextmanager
def stamped_stderr(clock=None):
    """Stamp every stderr line (prints, warnings, retries) until the block ends."""
    import sys
    real = sys.stderr
    sys.stderr = Stamped(real, clock or time.time)
    try:
        yield sys.stderr
    finally:
        sys.stderr = real


def _say(message: str) -> None:
    import sys
    print(f"botroll: {message}", file=sys.stderr, flush=True)


def _why(e: BaseException) -> str:
    """A one-line reason. ValueErrors from this package already read as sentences."""
    return str(e) if type(e) is ValueError else f"{type(e).__name__}: {e}"


def collect(cfg: dict, directory: str, url: str | None = None, token: str | None = None, every: int = 0,
            show: bool = False, *, iterations: int | None = None, wall=None, mono=None, sleep=None) -> int:
    """`botroll health`: take a sample, push it (or store it in directory), and with every > 0 repeat.

    With every > 0 nothing that goes wrong in one iteration (a sample that fails or times out, a push or
    a command poll that drops) stops the loop: it is logged and the next iteration follows. Only
    KeyboardInterrupt and SystemExit end it. Once (every = 0), errors are raised as before.

    An iteration that starts more than 2 x every seconds after it was due logs "stalled N s", with how far
    the monotonic clock moved, so a stopped or suspended process shows up in the log instead of as a
    silent gap on the board. iterations, wall, mono and sleep are for tests."""
    wall, mono, sleep = wall or time.time, mono or time.monotonic, sleep or time.sleep
    if url and not cfg.get("control") and every:
        # Control is off here: refuse anything the server still has queued so it can't run later.
        try:
            for r in run_commands(cfg, fetch_commands(url, token)):
                report_command(url, token, r["id"], r["status"], r["detail"], r["nonce"])
        except Exception as e:  # noqa: BLE001  (logged; the collector must start anyway)
            if not re.search(r": 404\b", str(e)):  # 404: the server has control off, so nothing is queued
                _say(f"control: couldn't refuse queued commands: {_why(e)}")
    done, last = 0, None
    while True:
        started, started_wall = mono(), wall()
        if every and last is not None:
            late = started_wall - (last[1] + every)
            if late > 2 * every:
                _say(f"stalled {late:.0f} s: this sample started {late:.0f} s after it was due "
                     f"(the monotonic clock moved {started - last[0]:.0f} s since the last one)")
        last = (started, started_wall)
        snap = None
        try:
            snap = sample(cfg)
            if url:
                push(url, token, snap)
            else:
                append(directory, snap)
        except Exception as e:  # noqa: BLE001
            if not every:
                raise
            _say(f"{'sample' if snap is None else 'push' if url else 'store'} failed: {_why(e)}")
        if show and snap is not None:
            print(json.dumps(snap, indent=2, ensure_ascii=False), flush=True)
        done += 1
        if not every or (iterations is not None and done >= iterations):
            return 0
        # Pull-model remote control: poll commands every ~10s between samples when enabled.
        wait = max(every - (mono() - started), 1)
        if cfg.get("control") and url:
            deadline = mono() + wait
            while mono() < deadline:
                try:
                    cmds = fetch_commands(url, token)
                    for r in run_commands(cfg, cmds, snap) if cmds else []:
                        try:
                            report_command(url, token, r["id"], r["status"], r.get("detail") or "",
                                           r.get("nonce") or "")
                        except Exception as e:  # noqa: BLE001
                            _say(f"control report: {_why(e)}")
                        _say(f"control {r['id']} {r['status']}: {r.get('detail')}")
                except Exception as e:  # noqa: BLE001
                    _say(f"control poll: {_why(e)}")
                sleep(min(10, max(deadline - mono(), 0.1)))
        else:
            sleep(wait)
