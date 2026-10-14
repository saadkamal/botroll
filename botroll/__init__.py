"""BotRoll: an agent-first task list. One markdown file per bot, one line per item.

    - [doing] T-12 Compare vector databases · asked 2026-10-10 13:05 · updated 2026-10-10 15:05 · pid: 4821

Lines the parser doesn't recognise (headings, comments, notes) are kept as written.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

__version__ = "0.1.0"

STATES = ("todo", "doing", "waiting-human", "blocked", "done", "dropped")
OPEN = ("todo", "doing", "waiting-human", "blocked")
TIME = "%Y-%m-%d %H:%M"
SEP = " · "
LINE = re.compile(r"^\s*[-*]\s*\[\s*([\w-]+)\s*\]\s*(T-\d+)\b\s*(.*)$", re.I)
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # use with fullmatch; 64 chars so the .md name fits any filesystem
# Hours without an update before an open item counts as stale.
LIMITS = {"doing": 6, "blocked": 6, "waiting-human": 12, "todo": 24}


def tasks_dir() -> str:
    return os.path.expanduser(os.environ.get("BOTROLL_DIR", "~/.botroll"))


def waiting_label() -> str:
    """How waiting-human is written in files, e.g. waiting-saad when BOTROLL_HUMAN=saad."""
    return "waiting-" + os.environ.get("BOTROLL_HUMAN", "human").strip().lower()


def state_of(word: str) -> str:
    word = word.lower()
    return "waiting-human" if word.startswith("waiting") else word


_BAD_ZONES: set = set()


def _zone(name: str | None):
    """BOTROLL_TZ as a ZoneInfo; an unknown or malformed name falls back to UTC with one warning."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError) as e:  # ZoneInfoNotFoundError is a KeyError
        if name not in _BAD_ZONES:
            _BAD_ZONES.add(name)
            logging.getLogger("botroll").warning("BOTROLL_TZ=%r is not a valid time zone (%s); using UTC", name, e)
        return timezone.utc


def local(timestamp: float | None = None) -> datetime:
    """A moment as the task files write it: naive, in BOTROLL_TZ, else the machine's zone."""
    at = datetime.fromtimestamp(time.time() if timestamp is None else timestamp, _zone(os.environ.get("BOTROLL_TZ")))
    return at.replace(tzinfo=None, second=0, microsecond=0)


def now() -> datetime:
    return parse_time(os.environ.get("BOTROLL_NOW", "")) or local()


def parse_time(text: str) -> datetime | None:
    """Parse a task timestamp. Accepts the file format and a few common variants bots write.

    Always returns a naive datetime in board-local terms (no timezone conversion of Z/offsets:
    a trailing Z or +00:00 is stripped so UTC-labelled stamps still match BOTROLL_TZ wall times
    the bots already wrote as Melbourne-local numbers)."""
    if not text or not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    # Drop trailing Z / offset; bots sometimes paste ISO stamps beside local asked/updated fields.
    raw = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", raw).strip()
    raw = raw.replace("T", " ", 1)
    for fmt in (TIME, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt == "%Y-%m-%d":
                return dt  # midnight of that day
            return dt.replace(second=0, microsecond=0)
        except ValueError:
            continue
    return None


def clean(text: str) -> str:
    return " ".join(text.replace(SEP.strip(), "-").split())


@dataclass
class Item:
    state: str
    id: str
    title: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str | None = field(default=None, compare=False, repr=False)  # the line as read, kept until changed

    def get(self, key: str) -> str:
        return self.fields.get(key, "")

    @property
    def pids(self) -> list[int]:
        """The leading run of numbers, so 'pid: 4821 (ffmpeg, 2 hrs)' is just [4821]."""
        pids = []
        for token in re.split(r"[\s,]+", self.get("pid").strip()):
            if not re.fullmatch(r"[0-9]{1,9}", token):
                break
            pids.append(int(token))
        return pids

    def age(self, at: datetime) -> float | None:
        """Hours since the last update, or None if the line has no usable time."""
        t = parse_time(self.get("updated")) or parse_time(self.get("asked"))
        return max((at - t).total_seconds() / 3600, 0) if t else None  # a clock-skewed future time is "just now"

    def stale(self, at: datetime) -> bool:
        limit = LIMITS.get(self.state)
        if limit is None:
            return False
        age = self.age(at)
        return age is None or age > limit

    @property
    def written_state(self) -> str:
        return waiting_label() if self.state == "waiting-human" else self.state

    def line(self) -> str:
        parts = [f"- [{self.written_state}] {self.id} {self.title}"]
        for key, value in self.fields.items():
            if key.startswith("_"):
                parts.append(value)
            elif key in ("asked", "updated"):
                parts.append(f"{key} {value}")
            elif value:
                parts.append(f"{key}: {value}")
        return SEP.join(parts)


def parse(text: str) -> list[Item | str]:
    out: list[Item | str] = []
    if text.startswith("\ufeff"):
        text = text[1:]  # a BOM before --- would hide the front matter
    for raw in text.splitlines():
        m = LINE.match(raw)
        if not m:
            out.append(raw)
            continue
        title, *rest = [p.strip() for p in m.group(3).split(SEP.strip())]
        item = Item(state_of(m.group(1)), m.group(2).upper(), title, raw=raw)
        for i, part in enumerate(rest):
            word = part.split(" ")[0].lower()
            key = word if word in ("asked", "updated") else word[:-1] if word.endswith(":") else ""
            if key and key not in item.fields:
                item.fields[key] = part[len(word):].strip()
            else:
                item.fields[f"_{i}"] = part
        out.append(item)
    return out


def items(lines: list[Item | str]) -> list[Item]:
    """Item lines, first occurrence of each id. A repeated id is kept in the file but shown once."""
    seen, out = set(), []
    for line in lines:
        if isinstance(line, Item) and line.id not in seen:
            seen.add(line.id)
            out.append(line)
    return out


def about(lines: list[Item | str], bot: str) -> tuple[str, str]:
    """(name, role) from the file's front matter. The name falls back to the slug, the role to ''."""
    meta: dict[str, str] = {}
    if lines and lines[0] == "---":
        for line in lines[1:]:
            if not isinstance(line, str) or line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip().lower()] = value.strip()
    return meta.get("name") or bot, meta.get("role", "")


def title(name: str, role: str) -> str:
    return f"{name} · {role}" if role else name


def path(directory: str, bot: str) -> str:
    if not SLUG.fullmatch(bot):
        raise ValueError(f"bot slugs are lowercase letters, digits, - and _, at most 64 characters: {bot!r}")
    return os.path.join(directory, bot + ".md")


def bots(directory: str) -> list[str]:
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(n[:-3] for n in names if n.endswith(".md") and SLUG.fullmatch(n[:-3]))


def read(directory: str, bot: str) -> list[Item | str]:
    try:
        with open(path(directory, bot), encoding="utf-8") as f:
            return parse(f.read())
    except FileNotFoundError:
        return ["---", f"name: {bot}", "role:", "---", ""]


@contextmanager
def locked(directory: str):
    """One lock per directory, so every writer re-reads the file it is about to replace."""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_atomic(target: str, text: str) -> None:
    """Replace target so readers see the old file or the new one, never half of either."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        mode = os.stat(target).st_mode if os.path.exists(target) else 0o644
        os.chmod(tmp, mode & 0o777)
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise


@contextmanager
def editing(directory: str, bot: str):
    """Yield the bot's lines under the lock, then write them back atomically."""
    target = path(directory, bot)
    with locked(directory):
        lines = read(directory, bot)
        yield lines
        write_atomic(target, "\n".join(l if isinstance(l, str) else l.raw or l.line() for l in lines).rstrip("\n") + "\n")


def store(directory: str, bot: str, text: str) -> int:
    """Replace a bot's whole file, as a push does, and return how many items it has."""
    target = path(directory, bot)
    count = len(items(parse(text)))
    with locked(directory):
        write_atomic(target, text)
    return count


PRUNE_MAX = 3  # roll files that may be deleted per hour, across calls
PRUNE_LOG = ".prune.json"  # the hour's budget, kept next to the roll files so restarts don't reset it


class PruneRefused(ValueError):
    """A confirmed prune over a cap. HTTP 409."""


def _save_budget(log: str, start: float, base: int, used: int) -> None:
    """Write the hour's prune budget, or ValueError with no file path in it (it goes back over HTTP)."""
    import json
    try:
        write_atomic(log, json.dumps({"start": start, "bots": base, "deleted": used}))
    except OSError:
        raise ValueError("couldn't save the prune budget; nothing was deleted") from None


def prune(directory: str, keep, confirm: bool = False, cap: int = PRUNE_MAX, at: float | None = None) -> list[str]:
    """The roll files whose slug is not in keep. Deletes them, under the lock, only with confirm.

    A confirmed prune is refused (PruneRefused, nothing deleted) if it would:
    - delete more than cap files in this hour, counting earlier prunes in the hour;
    - leave fewer than half the bots there were at the start of the hour;
    - delete more than half the bots there are now; or
    - delete the last bot.
    An empty keep is refused too, so a push with a wrong keep list can't wipe the board."""
    import json
    keep = set(keep)
    if not keep:
        raise ValueError("refusing to prune with an empty keep list")
    for slug in keep:
        path(directory, slug)  # validates
    at = time.time() if at is None else at
    log = os.path.join(directory, PRUNE_LOG)
    with locked(directory):
        current = bots(directory)
        doomed = [slug for slug in current if slug not in keep]
        if not confirm or not doomed:
            return doomed
        try:
            with open(log, encoding="utf-8") as f:
                hour = json.load(f)
            start, base, used = float(hour["start"]), int(hour["bots"]), int(hour["deleted"])
            if not (start <= at and base >= 0 and used >= 0):
                raise ValueError("future-dated budget, or the clock went backwards")
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            # Missing, corrupt, future-dated or a clock that went back: fail closed. Start a fresh hour that
            # is already used up, so nothing is deleted until it has passed.
            start, base, used = at, len(current), cap
            _save_budget(log, start, base, used)
        if at - start >= 3600:
            start, base, used = at, len(current), 0
        left = len(current) - len(doomed)
        why = ("it would delete the last bot" if left < 1
               else f"at most {cap} deletions an hour ({used} already)" if used + len(doomed) > cap
               else f"never below half the {base} bots at the start of the hour" if 2 * left < base
               else f"never more than half the {len(current)} bots" if 2 * len(doomed) > len(current)
               else "")
        if why:
            raise PruneRefused(f"refusing to delete {len(doomed)} of {len(current)} roll files: {why}. "
                               "Delete them by hand if you mean it")
        _save_budget(log, start, base, used + len(doomed))  # spend the budget first; no save, no delete
        for slug in doomed:
            try:
                os.remove(path(directory, slug))
            except FileNotFoundError:
                pass
            except OSError:
                raise ValueError(f"couldn't delete the roll file for {slug}") from None
    return doomed


def _apply(item: Item, at: datetime, state: str | None, note: str | None, pid: str | None) -> None:
    if state is not None:
        state = state_of(state)
        if state not in STATES:
            raise ValueError(f"state must be one of: {', '.join(STATES)}")
        item.state = state
    item.raw = None
    if note is not None:
        item.fields["note"] = clean(note)
    if pid is not None:
        item.fields["pid"] = clean(pid)
    item.fields["updated"] = at.strftime(TIME)


def add(directory: str, bot: str, title: str, state: str = "doing",
        note: str | None = None, pid: str | None = None) -> str:
    """Append an item and return its id, e.g. 'T-7'."""
    at = now()
    with editing(directory, bot) as lines:
        n = max([int(i.id[2:]) for i in items(lines)] + [0]) + 1
        item = Item("todo", f"T-{n}", clean(title), {"asked": at.strftime(TIME), "updated": ""})
        _apply(item, at, state, note, pid)
        lines.append(item)
    return item.id


def update(directory: str, bot: str, item_id: str, state: str | None = None,
           note: str | None = None, pid: str | None = None) -> Item:
    with editing(directory, bot) as lines:
        for item in items(lines):
            if item.id == item_id.upper():
                _apply(item, now(), state, note, pid)
                return item
        raise LookupError(f"no {item_id.upper()} in {path(directory, bot)}")
