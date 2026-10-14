"""The human view: one snapshot, rendered as JSON, a terminal view, or a static HTML page."""
from __future__ import annotations

import html
import os
import sys
import uuid
import zlib
from dataclasses import asdict
from datetime import datetime
from importlib import resources

from . import health as box
from . import OPEN, TIME, Item, about, bots, items, local, now, parse_time, read, title
from .watch import check, duration, processes

BOARD_CSS = resources.files("botroll").joinpath("board.css").read_text(encoding="utf-8")
BOARD_SCRIPT = ('if (matchMedia("(max-width: 560px)").matches) '
                'for (const d of document.querySelectorAll("details.done")) d.open = false;')


PUSH_STALE_MINUTES = 30


def snapshot(directory: str, at: datetime | None = None, procs: dict | None = None,
             pushed_flags: list[dict] = (), pushed: datetime | None = None) -> dict:
    """Everything the board shows, as plain data. This is the JSON output.

    On a server, procs is {} (the host's processes say nothing about the bots), and process
    flags arrive from the bots' machine as pushed_flags, with the time of the last push.
    """
    at = at or now()
    procs = processes() if procs is None else procs
    entries, team = [], []
    for slug in bots(directory):
        lines = read(directory, slug)
        name, role = about(lines, slug)
        mine = items(lines)
        entries += [(slug, i) for i in mine]
        shown = [i for i in mine if i.state not in ("done", "dropped") or _recent(i, at)]
        team.append({"bot": slug, "name": name, "role": role, "items": [_item(i, at) for i in shown]})
    flags = [asdict(f) for f in check(entries, procs, at)] + list(pushed_flags)
    snap = {"generated": at.strftime(TIME), "pushed": pushed.strftime(TIME) if pushed else None,
            "dir": directory, "summary": _summary(team), "bots": team, "flags": flags}
    samples = box.load(directory)
    if samples:  # only when `botroll health` has stored something, so boards without it are unchanged
        snap["health"] = samples
    return snap


RECENT_HOURS = 24  # closed items count as "recent" for the summary and card "done" strip
FUTURE_SKEW_MIN = 10  # a stamp this far ahead of the board clock (bot clock drift) still counts as recent


def _today(item: Item, at: datetime) -> bool:
    """Calendar day of `at` (BOTROLL_TZ). Prefer _recent for summaries: midnight resets look empty."""
    t = parse_time(item.get("updated"))
    return bool(t and t.date() == at.date())


def _recent(item: Item, at: datetime, hours: float = RECENT_HOURS) -> bool:
    """True when updated within the last `hours` (rolling), using the same naive BOTROLL_TZ clock."""
    t = parse_time(item.get("updated"))
    if not t:
        return False
    return -FUTURE_SKEW_MIN * 60 <= (at - t).total_seconds() <= hours * 3600


def _item(item: Item, at: datetime) -> dict:
    age = item.age(at)
    return {"id": item.id, "state": item.state, "title": item.title,
            "asked": item.get("asked") or None, "updated": item.get("updated") or None,
            "note": item.get("note") or None, "pids": item.pids,
            "fields": {k: v for k, v in item.fields.items() if k not in ("asked", "updated", "note", "pid")},
            "age_minutes": None if age is None else round(age * 60), "stale": item.stale(at)}


def _plural(n: int, one: str, many: str, none: str) -> str:
    return none if n == 0 else one.format(n) if n == 1 else many.format(n)


def _summary(team: list[dict]) -> dict:
    every = [i for b in team for i in b["items"]]
    doing = [[i for i in b["items"] if i["state"] == "doing"] for b in team]
    quiet = [b["name"] for b, d in zip(team, doing) if d and all(i["stale"] for i in d)]
    counts = {
        "needs_you": sum(i["state"] == "waiting-human" for i in every),
        "working": sum(bool(d) for d in doing),
        "quiet": len(quiet),
        "stuck": sum(i["state"] == "blocked" or (i["stale"] and i["state"] in ("doing", "todo")) for i in every),
        "done_today": sum(i["state"] == "done" for i in every),  # rolling window; key kept for API compat
        "done_recent": sum(i["state"] == "done" for i in every),
    }
    first = _plural(counts["needs_you"], "1 thing needs you.", "{} things need you.", "Nothing needs you.")
    working = _plural(counts["working"], "1 bot is working", "{} bots are working", "No bots are working")
    if quiet:
        names = " and ".join(filter(None, [", ".join(quiet[:-1]), quiet[-1]]))
        working += f" ({names} {'has' if len(quiet) == 1 else 'have'} gone quiet)"
    stuck = _plural(counts["stuck"], "1 item is stuck", "{} items are stuck", "nothing is stuck")
    done = _plural(counts["done_recent"], "1 got done in the last 24h", "{} got done in the last 24h",
                   "nothing got done in the last 24h")
    return counts | {"quiet_bots": quiet, "sentence": [first, f"{working}, {stuck}, and {done}."]}


def _mins(item: dict) -> float:
    return float("inf") if item["age_minutes"] is None else item["age_minutes"]


def _ago(item: dict) -> str:
    m = item["age_minutes"]
    if m is None:
        return duration(None)
    return "just now" if m < 1 else duration(m / 60)


def needs_you(snap: dict) -> list[tuple[dict, dict]]:
    """(bot, item) for every waiting-human item, oldest first."""
    asks = [(b, i) for b in snap["bots"] for i in b["items"] if i["state"] == "waiting-human"]
    return sorted(asks, key=lambda a: -_mins(a[1]))


def card(bot: dict, flags: list[dict]) -> dict:
    """What one bot's card shows. Rows are (kind, label, text, meta)."""
    its = bot["items"]
    of = lambda state: [i for i in its if i["state"] == state]
    doing = sorted(of("doing"), key=_mins)
    live = [i for i in doing if not i["stale"]]
    current = (live or doing or [None])[0]
    waiting = sorted(of("waiting-human"), key=lambda i: -_mins(i))

    if live:
        status = ("doing", "Working")
    elif doing:
        status = ("quiet", f"Quiet {_ago(doing[0])}")
    elif waiting:
        status = ("you", "Waiting on you")
    elif of("blocked"):
        status = ("stuck", "Blocked")
    else:
        status = ("idle", "Queued" if of("todo") else "Idle")

    if current:
        pids = "".join(f" · pid {p}" for p in current["pids"])
        sub = f"{current['id']} · " + (f"no update for {_ago(current)}" if current["stale"] else _ago(current)) + pids
        now_line = (current["title"], sub)
    elif waiting:
        now_line = ("Nothing in progress", f"Waiting on you for {_ago(waiting[0])}")
    else:
        now_line = ("Nothing in progress", "")

    rows, row_items = [], []

    def add(row, item=None):
        rows.append(row)
        row_items.append(item)

    for i in waiting:
        add(("you", "You", i["title"], _ago(i)), i)
    for i in of("blocked"):
        add(("stuck", "Blocked", i["title"], i["note"] or _ago(i)), i)
    for i in doing:
        if i is not current:
            add(("quiet", "Quiet", i["title"], f"no update for {_ago(i)}") if i["stale"]
                else ("doing", "Doing", i["title"], _ago(i)), i)
    for i in of("todo"):
        add(("quiet", "Stale", i["title"], f"queued {_ago(i)}, never started") if i["stale"]
            else ("next", "Next", i["title"], i["note"] or ""), i)
    for f in flags:
        if f["bot"] == bot["bot"] and f["kind"] in ("pid", "cpu", "state"):
            add(("stuck", f["kind"].upper(), f["text"], ""))

    done = [("done" if i["state"] == "done" else "drop", i["title"],
             " · ".join(filter(None, [(i["updated"] or "")[11:], i["note"]])))
            for i in sorted(of("done") + of("dropped"), key=lambda i: i["updated"] or "", reverse=True)]
    updated = min((i["age_minutes"] for i in its if i["age_minutes"] is not None), default=None)
    when = None if updated is None else ("just now" if updated < 1 else duration(updated / 60))
    return {"name": bot["name"], "role": bot["role"], "title": title(bot["name"], bot["role"]), "status": status, "now": now_line, "rows": rows, "done": done,
            "open": len(its) - len(done), "updated": when, "bot": bot["bot"],
            "current": current, "row_items": row_items,
            "you": len(waiting), "trouble": sum(r[0] in ("stuck", "quiet") for r in rows) + bool(doing and not live)}


def cards(snap: dict) -> list[dict]:
    rank = {"you": 0, "quiet": 1, "stuck": 2, "doing": 3, "idle": 4}
    out = [card(b, snap["flags"]) for b in snap["bots"]]
    return sorted(out, key=lambda c: (-c["you"], -c["trouble"], rank[c["status"][0]], c["name"].lower()))


def unowned(snap: dict) -> list[dict]:
    return [f for f in snap["flags"] if f["bot"] is None]


def _when(snap: dict) -> str:
    t = parse_time(snap["generated"])
    return f"{t:%a} {t.day} {t:%b} · {t:%H:%M}" if t else snap["generated"]


# Terminal ------------------------------------------------------------------

def text(snap: dict, color: bool | None = None) -> str:
    if color is None:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    paint = (lambda s, code: f"\033[{code}m{s}\033[0m") if color else (lambda s, code: s)
    tone = {"you": "33", "stuck": "31", "quiet": "33", "doing": "34", "done": "32"}
    first, rest = snap["summary"]["sentence"]
    out = [paint("BotRoll", "1") + f"  {_when(snap)}", "",
           paint(first, "1;33" if snap["summary"]["needs_you"] else "1") + " " + rest]
    asks = needs_you(snap)
    if asks:
        out += ["", paint("Needs you", "1")]
        width = max(len(f"{b['name']} · {i['id']}") for b, i in asks)
        out += [f"  {_ago(i):<8} {(b['name'] + ' · ' + i['id']).ljust(width)}  {i['note'] or i['title']}" for b, i in asks]
    for c in cards(snap):
        kind, label = c["status"]
        out += ["", paint(c["title"], "1") + "  " + paint(label, tone.get(kind, "2")),
                f"  {c['now'][0]}" + (paint(f" · {c['now'][1]}", "2") if c["now"][1] else "")]
        for kind, label, body, meta in c["rows"]:
            out.append(f"  {paint(label.lower().ljust(8), tone.get(kind, '2'))} {body}"
                       + (paint(f" · {meta}", "2") if meta else ""))
        n = sum(k == "done" for k, _, _ in c["done"])
        if n:
            out.append(f"  {paint('done    ', '32')} {n} in 24h")
    if unowned(snap):
        out += ["", paint("No owner", "1")]
        out += [f"  {paint(f['kind'].ljust(8), '31')} {f['text']}" for f in unowned(snap)]
    return "\n".join(out) + "\n"


# HTML ----------------------------------------------------------------------

THEMES = ("system", "light", "dark")
ICONS = {  # 24px line icons, stroked by the .i class
    "system": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "light": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
             'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "dark": '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
}


def icon(name: str) -> str:
    return f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def theme_toggle(csrf: str, theme: str | None) -> str:
    """System / Light / Dark as a form post, so it works without JavaScript."""
    return ('<form class="theme" method="post" action="/theme" role="group" aria-label="Theme">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            + "".join(f'<button name="theme" value="{t}" aria-pressed="{"true" if t == (theme or "system") else "false"}" '
                      f'aria-label="{t.title()} theme" title="{t.title()}">{icon(t)}</button>' for t in THEMES)
            + "</form>")


def _mark(name: str) -> str:
    return f'<span class="mark hue{zlib.crc32(name.encode()) % 5}" aria-hidden="true">{html.escape(name[:1].upper())}</span>'


def _status(result: dict | None) -> str:
    if not result:
        return ""
    if result.get("status") is None:
        return '<span class="act-status">pending action</span>'
    if result.get("ok"):
        text = "Sent ✓" + (" · pending action" if result.get("pending") else "")
        return f'<span class="act-status ok">{text}</span>'
    return '<span class="act-status fail">Failed</span>'


def sync_button(csrf: str, result: dict | None = None) -> str:
    """Board-level "Sync now": a signed "sync" action to the board endpoint. Only when one is configured."""
    e = html.escape
    status = ""
    if result:
        at = local(result["sent_at"]).strftime("%H:%M") if result.get("sent_at") else ""
        when = f' <time>{e(at)}</time>' if at else ""
        status = ('<span class="act-status sync-status">sending</span>' if result.get("status") is None
                  else f'<span class="act-status sync-status ok">Sync sent ✓{when}</span>' if result.get("ok")
                  else f'<span class="act-status sync-status fail">Sync failed{when}</span>')
    return (f'<form class="act sync" method="post" action="/action">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            f'<input type="hidden" name="action" value="sync">'
            f'<input type="hidden" name="rid" value="{uuid.uuid4()}">'
            f'<button class="btn small secondary" type="submit" title="Ask the bots to push now">Sync now</button>'
            f'</form>{status}')


def _act_form(bot: str, item: dict, action: str, csrf: str, label: str, kind: str, note: bool = False) -> str:
    e = html.escape
    rid = str(uuid.uuid4())
    fields = (f'<input type="hidden" name="csrf" value="{e(csrf)}">'
              f'<input type="hidden" name="bot" value="{e(bot)}">'
              f'<input type="hidden" name="item" value="{e(item["id"])}">'
              f'<input type="hidden" name="action" value="{e(action)}">'
              f'<input type="hidden" name="rid" value="{e(rid)}">')
    if note:
        return (f'<details class="reply"><summary class="btn small tertiary">{e(label)}</summary>'
                f'<form class="act" method="post" action="/action">{fields}'
                f'<label class="sr" for="note-{e(rid)}">Note</label>'
                f'<textarea class="note" id="note-{e(rid)}" name="note" required maxlength="500" '
                f'placeholder="A one-line reply"></textarea>'
                f'<button class="btn small primary" type="submit">Send</button></form></details>')
    return (f'<form class="act" method="post" action="/action">{fields}'
            f'<button class="btn small {kind}" type="submit">{e(label)}</button></form>')


def item_actions(bot: str, item: dict, csrf: str, result: dict | None = None) -> str:
    """Approve / Decline / Reply on waiting-human; Nudge on any open item. Empty when csrf is missing."""
    if not csrf or item["state"] not in OPEN:
        return ""
    parts = []
    if item["state"] == "waiting-human":
        parts += [_act_form(bot, item, "approve", csrf, "Approve", "primary"),
                  _act_form(bot, item, "decline", csrf, "Decline", "tertiary"),
                  _act_form(bot, item, "reply", csrf, "Reply…", "tertiary", note=True)]
    parts.append(_act_form(bot, item, "nudge", csrf, "Nudge", "secondary"))
    return f'<div class="acts">{"".join(parts)}{_status(result)}</div>'


def _rows(rows, extras=None) -> str:
    e = html.escape
    rows = list(rows)
    extras = [""] * len(rows) if extras is None else list(extras)
    return "".join(f'<li><span class="k {kind}">{e(label)}</span><span>{e(body)}'
                   + (f' <span class="d">· {e(meta)}</span>' if meta else "")
                   + extra + "</span></li>"
                   for (kind, label, body, meta), extra in zip(rows, extras, strict=True))


def _acts(bot: str, item: dict | None, csrf: str | None, endpoints, results) -> str:
    if not (csrf and item and bot in endpoints):
        return ""
    return item_actions(bot, item, csrf, (results or {}).get(f"{bot}/{item['id']}"))


def _card(c: dict, csrf: str | None = None, endpoints=(), results=None) -> str:
    e = html.escape
    kind, label = c["status"]
    title, sub = c["now"]
    current = c.get("current")
    extras = [_acts(c["bot"], item, csrf, endpoints, results) if item is not current else ""
              for item in (c.get("row_items") or [None] * len(c["rows"]))]
    done = ""
    if c["done"]:
        n = sum(k == "done" for k, _, _ in c["done"])
        rows = _rows((k, "Done" if k == "done" else "Drop", t, m) for k, t, m in c["done"])
        done = (f'<details class="done" open><summary><span class="k done">✓</span>{n} done in 24h'
                f'</summary><ul class="items">{rows}</ul></details>')
    return (f'<article class="card"><div class="head">{_mark(c["name"])}'
            f'<span class="name">{e(c["name"])}'
            + (f' <span class="role">· {e(c["role"])}</span>' if c["role"] else "")
            + f'</span><span class="pill {kind}">{e(label)}</span></div>'
            f'<div class="now{" idle" if title == "Nothing in progress" else ""}">{e(title)}'
            f'<span class="sub">{e(sub)}</span>'
            + _acts(c["bot"], current, csrf, endpoints, results) + "</div>"
            + (f'<ul class="items">{_rows(c["rows"], extras)}</ul>' if c["rows"] else "") + done
            + f'<div class="foot"><span>{c["open"]} open</span>'
            + (f'<span>updated {c["updated"]}</span>' if c["updated"] == "just now"
               else f'<span>updated {c["updated"]} ago</span>' if c["updated"] else "") + "</div></article>")


def document(page_title: str, body: str, refresh: bool = False, theme: str | None = None) -> str:
    """A complete page with the board's styles inlined, so it needs nothing else to render.

    theme is "light" or "dark" from the owner's choice, or None to follow the device."""
    css = BOARD_CSS
    return f"""<!doctype html>
<html lang="en"{f' data-theme="{theme}"' if theme else ""}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="{theme or "light dark"}">
{'<meta http-equiv="refresh" content="60">' if refresh else ""}
<title>{html.escape(page_title)}</title>
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def _pushed(snap: dict) -> tuple[str, str]:
    """('Pushed 3m ago · ', banner html) for a served board, or two empty strings."""
    t, now_ = parse_time(snap["pushed"] or ""), parse_time(snap["generated"])
    if not (t and now_):
        return "", ""
    hours = max((now_ - t).total_seconds() / 3600, 0)
    banner = ""
    if hours * 60 > PUSH_STALE_MINUTES:
        banner = f'<p class="banner">Nothing pushed for {duration(hours)}. This board may be out of date.</p>'
    return f"Pushed {'just now' if hours * 60 < 1 else duration(hours) + ' ago'} · ", banner


def page(snap: dict, live: bool = False, csrf: str | None = None, theme: str | None = None,
         signout: bool = True, endpoints=(), results=None, sync=None,
         health_control: bool = False, health_results=None) -> str:
    """The board. live means served by botroll serve; csrf adds the theme toggle and, with signout, a sign-out button.

    endpoints is a mapping of bot slug → action target. Empty (the default) leaves the board unchanged.
    A "*" key adds a board-level Sync now button; sync is the last sync click's result.
    health_control adds Close/Stop on the box-health panel when the server enables it.
    """
    e = html.escape
    s = snap["summary"]
    first, rest = s["sentence"]
    asks = needs_you(snap)
    strip = ""
    if asks:
        lis = []
        for b, i in asks:
            lis.append(f'<li class="ask"><div class="top">{_mark(b["name"])}'
                       f'<span class="who">{e(b["name"])} · {i["id"]}</span>'
                       f'<span class="pill you"><span class="sr">waiting </span>{_ago(i)}</span></div>'
                       f'<div class="q" title="{e(i["title"])}">{e(i["note"] or i["title"])}</div>'
                       f'{_acts(b["bot"], i, csrf, endpoints, results)}</li>')
        strip = ('<section class="strip"><h2 class="label">Needs you '
                 f'<span class="n">{len(asks)} · oldest first</span></h2><ul class="asks">'
                 + "".join(lis) + "</ul></section>")
    grid = "".join(_card(c, csrf, endpoints, results) for c in cards(snap))
    lost = unowned(snap)
    if lost:
        grid += ('<article class="card unowned"><div class="head"><span class="mark none" aria-hidden="true">?</span>'
                 f'<span class="name">No owner</span><span class="pill stuck">{len(lost)} '
                 f'{"process" if len(lost) == 1 else "processes"}</span></div>'
                 f'<ul class="items">{_rows(("stuck", f["kind"].upper(), f["text"], "") for f in lost)}</ul>'
                 '<div class="foot"><span>The watchdog only reports. Nothing is killed.</span></div></article>')
    if not grid:
        grid = f'<p class="empty">No task files in <code>{e(snap["dir"])}</code> yet.</p>'
    n_items = sum(len(b["items"]) for b in snap["bots"])
    pushed, banner = _pushed(snap)
    controls = sync_button(csrf, sync) if csrf and "*" in (endpoints or {}) else ""
    controls += theme_toggle(csrf, theme) if csrf else ""
    if csrf and signout:
        controls += ('<form method="post" action="/logout"><input type="hidden" name="csrf" '
                     f'value="{e(csrf)}"><button class="btn tertiary small">Sign out</button></form>')
    refresh = "Live. It reloads every minute." if live else "A static snapshot. Regenerate it to refresh."
    return document(f"({s['needs_you']}) BotRoll" if s["needs_you"] else "BotRoll", f"""<main>
<header><span class="brand">BotRoll</span><div class="bar"><span class="when">{pushed}{_when(snap)}</span>{controls}</div></header>
{banner}<section class="hero"><h1 class="summary">{e(first)}</h1><p class="lede">{e(rest)}</p></section>
{strip}
{health_panel(snap, csrf=csrf, control=health_control, control_results=health_results)}<section><h2 class="label">The team <span class="n">{len(snap["bots"])} bots</span></h2><div class="grid">{grid}</div></section>
<footer><span>botroll board · {n_items} items shown · {e(snap["dir"])}</span><span>{refresh}</span></footer>
</main>
<script>{BOARD_SCRIPT}</script>""",
                    refresh=True, theme=theme)


# Box health (optional) -----------------------------------------------------------

def _gb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _gauge(label: str, value: str, detail: str, fraction: float, lvl: str) -> str:
    w = f"{max(0.0, min(fraction, 1.0)) * 100:.1f}%"
    return (f'<div class="gauge {lvl}"><div class="g-top"><span class="g-label">{html.escape(label)}</span>'
            f'<span class="g-value">{html.escape(value)}</span></div>'
            f'<svg class="g-bar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect class="track" width="100" height="8" rx="4"/><rect class="fill" width="{w}" height="8" rx="4"/></svg>'
            f'<span class="g-detail">{html.escape(detail)}</span></div>')


def _table(head: list[str], rows: list[list[str]], cls: str = "") -> str:
    e = html.escape
    return (f'<div class="h-scroll"><table class="h-table {cls}"><thead><tr>'
            + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f'<td class="h-act">{c}</td>' if (i == len(r) - 1 and head and head[-1] == "")
                                       else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
            + "</tbody></table></div>")


def _chart(samples: list[dict], at: float, th: dict) -> str:
    e = html.escape
    bucket = int(th.get("bucket_min") or 30)
    peaks = box.bucket_peaks(samples, at, bucket_min=bucket)
    n = max(len(peaks), 1)
    width = n * 10
    top = max([p["ratio"] for p in peaks if p] + [th["load_red"] * 1.25])
    bars = []
    for i, p in enumerate(peaks):
        if not p:
            continue
        h = p["ratio"] / top * 100
        lvl = box.level(p["ratio"], th["load_amber"], th["load_red"])
        when = local(p["ts"]).strftime("%H:%M")
        tip = f'{when}: load {p["load"]:.1f} on {p["cores"]} cores, {p["avail_pct"]:.0f}% memory free' + (
            f', busiest browser {p["bot"]}' if p["bot"] else "")
        bars.append(f'<rect class="{lvl}" x="{i * 10 + 1}" y="{100 - h:.1f}" width="8" height="{h:.1f}" rx="2">'
                    f'<title>{e(tip)}</title></rect>')
    line = 100 - 1 / top * 100
    worst = sorted((p for p in peaks if p), key=lambda p: -p["ratio"])[:3]
    notes = "".join(f'<li><span class="t">{local(p["ts"]).strftime("%H:%M")}</span> load {p["load"]:.1f} '
                    f'({p["ratio"]:.1f}× cores) · {p["avail_pct"]:.0f}% free'
                    + (f' · busiest: {e(p["bot"])}' if p["bot"] else "") + "</li>" for p in worst)
    label = f"Peak load per {bucket} min, last 24 hours"
    return (f'<div class="h-chart"><svg viewBox="0 0 {width} 100" preserveAspectRatio="none" role="img" '
            f'aria-label="{e(label)}"><line class="cores" x1="0" x2="{width}" y1="{line:.1f}" '
            f'y2="{line:.1f}"/>{"".join(bars)}</svg><div class="h-axis"><span>24h ago</span>'
            f'<span>{bucket} min bars</span><span>now</span></div>'
            f'<ul class="h-peaks">{notes}</ul></div>')


def health_panel(snap: dict, env=os.environ, csrf: str | None = None, control: bool = False,
                 control_results=None) -> str:
    """The Box health section, or "" when there are no usable samples. A bad sample is skipped, never fatal.

    control + csrf: show Close/Stop buttons that POST to /health/control (server must enable them).
    """
    samples = []
    for raw in snap.get("health") or []:
        try:
            samples.append(box.clean(raw))
        except ValueError:
            continue
    while samples:
        try:
            return _health_panel(snap, samples, env, csrf=csrf, control=control, control_results=control_results)
        except (ValueError, TypeError, KeyError, ZeroDivisionError, OverflowError):
            samples.pop()  # the newest one can't be drawn: fall back to the one before
    return ""


def _ctl(kind: str, fields: dict, csrf: str, label: str) -> str:
    """Confirm via <details> (CSP-safe: no inline handlers)."""
    e = html.escape
    hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(str(v))}">' for k, v in fields.items())
    return (f'<details class="h-ctl"><summary class="btn small tertiary">{e(label)}</summary>'
            f'<form class="act" method="post" action="/health/control">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            f'<input type="hidden" name="kind" value="{e(kind)}">{hidden}'
            f'<button class="btn small primary" type="submit">Confirm</button></form></details>')


def _browser_row(g: dict, shown: str, ctl_btn) -> str:
    """One browser: a header (name or fallback, processes, memory, CPU, tab count, Close) with its tabs
    nested under it. Collapsible with <details>; Close sits outside <summary> so clicking it never toggles."""
    e = html.escape
    btn = ""
    if g.get("closable") is True and g.get("main_pid") and g.get("started_at") is not None:
        btn = ctl_btn("close-browser", {"profile": g["profile"], "pid": g["main_pid"],
                                        "started_at": g["started_at"]}, "Close browser")
    hint = "" if g.get("bot") else (
        f' <span class="muted">{e(box.plain_text(g["profile"]))} · profile name</span>' if g.get("label")
        else ' <span class="muted">unmapped</span>')
    tabs = g.get("tabs")
    count = "tabs –" if tabs is None else f'{len(tabs)} tab{"s" * (len(tabs) != 1)}'
    meta = (f'<span class="h-br-meta">{e(str(g.get("procs", "")))} proc · {_gb(g.get("rss_mb", 0))} · '
            f'{g.get("cpu", 0):.0f}% CPU · {count}</span>')
    head = f'<span class="h-br-name">{shown}{hint}</span>{meta}'
    lis = "".join(f'<li><span class="tt">{e(str(t.get("title") or "Untitled"))}</span>'
                  f'<span class="muted">{e(str(t.get("origin", "")))}</span></li>' for t in (tabs or []))
    body = (f'<details class="h-br-tabs"><summary>{head}</summary><ul>{lis}</ul></details>' if lis
            else f'<div class="h-br-tabs h-br-empty">{head}</div>')
    act = f'<div class="h-br-act">{btn}</div>' if btn else ""
    return f'<div class="h-br">{body}{act}</div>'


def _health_panel(snap: dict, samples: list[dict], env, csrf: str | None = None, control: bool = False,
                  control_results=None) -> str:
    e = html.escape
    th = box.thresholds(env)
    s = samples[-1]
    at = s["ts"]
    mem, cores, load1 = s["mem"], s["cores"], s["load"][0]
    free = 100 * mem["available_mb"] / max(mem["total_mb"], 1)
    ratio = load1 / cores
    mlvl = box.level(free, th["mem_amber"], th["mem_red"], lower_is_worse=True)
    llvl = box.level(ratio, th["load_amber"], th["load_red"])
    gen = parse_time(snap["generated"])
    age = max((gen - local(at)).total_seconds() / 60, 0) if gen else 0
    when = "just now" if age < 2 else f"{duration(age / 60)} ago"
    stale = f'<p class="banner">No box-health sample for {duration(age / 60)}. Is the collector running?</p>' \
        if age > 10 else ""
    gauges = (_gauge("Memory headroom", f"{_gb(mem['available_mb'])} free",
                     f"{free:.0f}% of {_gb(mem['total_mb'])} available", free / 100, mlvl)
              + _gauge("CPU load", f"{load1:.1f} on {cores} cores",
                       f"{ratio:.1f}× the core count · 5m {s['load'][1]:.1f} · 15m {s['load'][2]:.1f}",
                       ratio / max(th["load_red"], 0.01), llvl))
    alerts = "".join(f'<li class="h-alert {"red" if a.get("kind") in ("stall", "overlap") else "amber"}">'
                     f'<span class="k">{e(str(a.get("kind", "")).upper())}</span>{e(str(a.get("text", "")))}</li>'
                     for a in s["alerts"])
    if llvl != "ok" and free > th["mem_amber"]:
        alerts = (f'<li class="h-alert {llvl}"><span class="k">CPU</span>Load is {ratio:.1f}× the core count while '
                  f'{_gb(mem["available_mb"])} of memory is free: the box is CPU-bound, not out of memory.</li>') + alerts
    parts = [f'<div class="gauges">{gauges}</div>']
    if alerts:
        parts.append(f'<ul class="h-alerts">{alerts}</ul>')

    def name(g):
        shown = box.plain_text(g.get("bot") or g.get("label") or "").strip()
        return e(shown or box.plain_text(os.path.basename(str(g.get("profile", "")).rstrip("/"))).strip() or "?")
    def ctl_btn(kind, fields, label):
        return _ctl(kind, fields, csrf, label) if (control and csrf) else ""

    if s["browsers"]:
        parts.append('<h3 class="h-sub">Browsers</h3><div class="h-browsers">'
                     + "".join(_browser_row(g, name(g), ctl_btn) for g in s["browsers"]) + "</div>")
    if s["helpers"]:
        rows = []
        for h in s["helpers"]:
            btn = ctl_btn("stop-helper", {"pid": h["pid"], "started_at": h.get("started_at") or 0}, "Stop") if h.get("started_at") is not None else ""
            rows.append([e(str(h.get("pid"))), _gb(h.get("rss_mb", 0)), f'{h.get("cpu", 0):.0f}%',
                         duration(h.get("elapsed_s", 0) / 3600),
                         ('<span class="pill stuck">stale</span> ' if h.get("stale") else "") + btn])
        parts.append('<h3 class="h-sub">Helpers</h3>' + _table(["PID", "Memory", "CPU", "Running", ""], rows))
    if s["jobs"]:
        rows = []
        for j in s["jobs"]:
            btn = ctl_btn("stop-job", {"pid": j["pid"], "started_at": j.get("started_at") or 0}, "Stop") if j.get("started_at") is not None else ""
            rows.append([e(str(j.get("kind"))), e(str(j.get("pid"))), f'{j.get("cpu", 0):.0f}%',
                         duration(j.get("elapsed_s", 0) / 3600), btn])
        parts.append('<h3 class="h-sub">Heavy jobs</h3>' + _table(
            ["Job", "PID", "CPU", "Running", ""] if control else ["Job", "PID", "CPU", "Running"],
            [r if control else r[:-1] for r in rows]))
    if control_results:
        lis = "".join(f'<li><span class="k">{e(str(r.get("status","")))}</span> '
                      f'{e(str(r.get("kind","")))} · {e(str((r.get("result") or {}).get("detail") or r.get("id","")))}</li>'
                      for r in control_results[-5:])
        parts.append(f'<ul class="h-alerts h-ctl-log">{lis}</ul>')
    parts.append('<h3 class="h-sub">Last 24 hours</h3>' + _chart(samples, at, th))
    return (f'<section class="health"><h2 class="label">Box health <span class="n">{e(s["host"])} · sampled {when}'
            f'</span></h2>{stale}<div class="card h-card">{"".join(parts)}</div></section>\n')
