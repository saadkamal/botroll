"""The watchdog. It reports; it never kills anything."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

from . import OPEN, STATES, Item, waiting_label

CAPTURE = re.compile(r"\b(ffmpeg|x11grab|wf-recorder|screencapture)\b")
CAPTURE_MINUTES = 30
CPU_SHARE, CPU_HOURS = 0.9, 1


@dataclass
class Proc:
    pid: int
    ppid: int
    elapsed: int  # seconds since start
    cpu: int  # seconds of CPU used
    args: str

    @property
    def name(self) -> str:
        return os.path.basename(self.args.split(" ")[0]) if self.args else "?"


@dataclass
class Flag:
    kind: str  # stale, state, pid, cpu, capture
    text: str
    bot: str | None = None
    id: str | None = None
    pid: int | None = None


def seconds(text: str) -> int:
    """'[dd-][hh:]mm:ss[.cc]', the etime/time format of both Linux and macOS ps."""
    days, _, clock = text.strip().rpartition("-")
    total = 0.0
    for part in clock.split(":"):
        total = total * 60 + float(part)
    return int(total + int(days or 0) * 86400)


def processes() -> dict[int, Proc]:
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,etime=,time=,args="],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    procs = {}
    for line in out.splitlines():
        try:
            pid, ppid, et, ct, args = (line.split(None, 4) + [""])[:5]
            procs[int(pid)] = Proc(int(pid), int(ppid), seconds(et), seconds(ct), args)
        except ValueError:
            continue
    procs.pop(os.getpid(), None)
    return procs


def duration(hours: float | None) -> str:
    if hours is None:
        return "an unknown time"
    m = round(hours * 60)
    if m < 60:
        return f"{m}m"
    if m < 600:
        return f"{m // 60}h" + (f" {m % 60}m" if m % 60 else "")
    if m < 2880:
        return f"{m // 60}h"
    return f"{m // 1440}d"


def stale_text(item: Item, at: datetime) -> str:
    age = duration(item.age(at))
    return {
        "doing": f"no update for {age}",
        "blocked": f"blocked, no update for {age}",
        "waiting-human": f"{waiting_label()} for {age}",
        "todo": f"never started, queued for {age}",
    }[item.state]


def check(entries: list[tuple[str, Item]], procs: dict[int, Proc], at: datetime) -> list[Flag]:
    """Flags for items and processes. entries are (bot, item) pairs across all files."""
    flags: list[Flag] = []
    owner: dict[int, tuple[str, Item]] = {}
    for bot, item in entries:
        if item.state not in STATES:
            flags.append(Flag("state", f"unknown state [{item.state}] on {item.id} {item.title}", bot, item.id))
        stale = item.stale(at)
        if stale:
            flags.append(Flag("stale", stale_text(item, at), bot, item.id))
        for pid in item.pids:
            if item.state in OPEN or pid not in owner:
                owner[pid] = (bot, item)
            if pid in procs and (stale or item.state in ("done", "dropped")):
                why = f"{item.id} is {item.state}" if not stale else f"{item.id} is stale"
                flags.append(Flag("pid", f"pid {pid} ({procs[pid].name}) still running, but {why}", bot, item.id, pid))

    def owned_by(pid: int) -> tuple[str, Item] | None:
        seen = set()
        while pid in procs and pid not in seen:
            if pid in owner:
                return owner[pid]
            seen.add(pid)
            pid = procs[pid].ppid
        return None

    for p in procs.values():
        who = owned_by(p.pid)
        if p.elapsed > CPU_HOURS * 3600 and p.cpu > CPU_SHARE * p.elapsed:
            text = f"pid {p.pid} ({p.name}) at {100 * p.cpu // p.elapsed}% CPU for {duration(p.elapsed / 3600)}"
            flags.append(Flag("cpu", text, who[0] if who else None, who[1].id if who else None, p.pid))
        # A recorded pid on a closed item is already a pid flag; its children are not.
        untracked = who is None or (who[1].state not in OPEN and p.pid not in owner)
        if CAPTURE.search(p.args) and p.elapsed >= CAPTURE_MINUTES * 60 and untracked:
            text = f"pid {p.pid} ({p.name}) capturing for {duration(p.elapsed / 3600)}, not on any open item"
            flags.append(Flag("capture", text, None, None, p.pid))
    return flags
