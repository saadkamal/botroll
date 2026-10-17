"""botroll: the command line."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

from . import STATES, __version__, about, add, bots, items, now, read, tasks_dir, title, update, waiting_label
from . import health, notify, serve
from .board import page, snapshot, text
from .watch import check, processes, stale_text


def main(argv: list[str] | None = None) -> int:
    dir_help = "task files directory (default: $BOTROLL_DIR or ~/.botroll)"
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS, help=dir_help)
    p = argparse.ArgumentParser(prog="botroll",
                                description="An agent-first task list. One markdown file per bot, one line per item.",
                                epilog=f"states: {', '.join(STATES)} (written as {waiting_label()}; set BOTROLL_HUMAN)")
    p.add_argument("--dir", default=tasks_dir(), help=dir_help)
    p.add_argument("--version", action="version", version=f"botroll {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def cmd(name, help):
        return sub.add_parser(name, parents=[common], help=help, description=help)

    def edits(s, state=True):
        if state:
            s.add_argument("--state", default="doing", help="initial state (default: doing; todo if queued)")
        s.add_argument("--note", help="what's needed, why, or where the result went")
        s.add_argument("--pid", help="PID(s) of background processes started for this item")

    s = cmd("add", "add an item and print its id")
    s.add_argument("bot")
    s.add_argument("title", nargs="+")
    edits(s)
    s = cmd("set", "change an item's state")
    s.add_argument("bot")
    s.add_argument("id")
    s.add_argument("state")
    edits(s, state=False)
    s = cmd("note", "record progress on an item")
    s.add_argument("bot")
    s.add_argument("id")
    s.add_argument("text", nargs="+")
    s = cmd("done", "mark an item done (only after delivery)")
    s.add_argument("bot")
    s.add_argument("id")
    edits(s, state=False)
    s = cmd("list", "list open items, flagging stale ones")
    s.add_argument("bot", nargs="*")
    s.add_argument("--all", action="store_true", help="include done and dropped")
    cmd("watch", "report stale items and runaway processes; exit 1 if anything is flagged")
    s = cmd("board", "the human view: terminal by default, or --html / --json")
    out = s.add_mutually_exclusive_group()
    out.add_argument("--html", metavar="FILE", help="write a static HTML page ('-' for stdout)")
    out.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    s = cmd("serve", "serve the live board to its owner, and the push API (see README: Self-host)")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)), help="default: $PORT or 8000")
    s.add_argument("--data", default=os.environ.get("BOTROLL_DATA"),
                   help="where pushed roll files live (default: $BOTROLL_DATA, else --dir)")
    s.add_argument("--insecure-local", action="store_true", help="no sign-in, and listen on 127.0.0.1 only")
    s.add_argument("--behind-proxy", action="store_true",
                   help="trust X-Forwarded-Proto for HSTS; client IP stays the socket peer unless --client-ip-header is set")
    s.add_argument("--client-ip-header", default=None,
                   help="trust this request header for the client IP (default: the peer). Also reads BOTROLL_CLIENT_IP_HEADER")
    s.add_argument("--insecure-http", action="store_true",
                   help="set the session cookie without Secure, for plain http on a private network")
    s = cmd("push", "upload every roll file and the watchdog's process flags to a botroll server")
    s.add_argument("--url", default=os.environ.get("BOTROLL_PUSH_URL") or os.environ.get("BOTROLL_URL"),
                   help="default: $BOTROLL_PUSH_URL, or $BOTROLL_URL")
    s.add_argument("--token", default=os.environ.get("BOTROLL_PUSH_TOKEN"),
                   help="default: $BOTROLL_PUSH_TOKEN (prefer it: arguments show up in ps)")
    s.add_argument("--prune", action="store_true",
                   help="also list roll files on the server for bots that aren't in --dir (a dry run)")
    s.add_argument("--yes", action="store_true",
                   help="with --prune: delete them (at most 3 per push, never more than half the bots)")
    s = cmd("health", "sample this machine's memory, load, browsers and heavy jobs (see README: Box health)")
    s.add_argument("--config", default=os.environ.get("BOTROLL_HEALTH_CONFIG"),
                   help="JSON settings (default: $BOTROLL_HEALTH_CONFIG); see botroll-health.json.example")
    s.add_argument("--url", default=os.environ.get("BOTROLL_PUSH_URL") or os.environ.get("BOTROLL_URL"),
                   help="push to this server (default: $BOTROLL_PUSH_URL); without it, store in --dir")
    s.add_argument("--token", default=os.environ.get("BOTROLL_PUSH_TOKEN"), help="default: $BOTROLL_PUSH_TOKEN")
    s.add_argument("--every", type=int, default=0, metavar="SECONDS",
                   help="keep sampling every SECONDS (e.g. 60) instead of once")
    s.add_argument("--print", action="store_true", help="also print each snapshot as JSON")
    cmd("hash-password", "read a password and print a hash for BOTROLL_PASSWORD_HASH")
    s = cmd("email", "email notifications from botroll serve (see README: Email)")
    s.add_argument("action", choices=["test"], help="test: send a test email with the current settings")

    a = p.parse_args(argv)
    try:
        return run(a)
    except (ValueError, LookupError) as e:
        print(f"botroll: {e}", file=sys.stderr)
        return 2


def run(a: argparse.Namespace) -> int:
    d = a.dir
    if a.cmd == "add":
        print(f"{a.bot}/{add(d, a.bot, ' '.join(a.title), a.state, a.note, a.pid)}")
    elif a.cmd in ("set", "done", "note"):
        state = {"set": getattr(a, "state", None), "done": "done", "note": None}[a.cmd]
        note = " ".join(a.text) if a.cmd == "note" else a.note
        item = update(d, a.bot, a.id, state, note, getattr(a, "pid", None))
        print(f"{a.bot}/{item.id} [{item.written_state}]")
    elif a.cmd == "list":
        at, blocks = now(), []
        for bot in a.bot or bots(d):
            lines = read(d, bot)
            shown = [i for i in items(lines) if a.all or i.state not in ("done", "dropped")]
            if shown:
                blocks.append([title(*about(lines, bot))] + [
                    f"  {bot}/{i.id} [{i.written_state}] {i.title}"
                    + i.line()[len(f"- [{i.written_state}] {i.id} {i.title}"):]
                    + (f"  ! {stale_text(i, at)}" if i.stale(at) else "") for i in shown])
        if blocks:
            print("\n\n".join("\n".join(b) for b in blocks))
    elif a.cmd == "watch":
        entries = [(bot, i) for bot in bots(d) for i in items(read(d, bot))]
        flags = check(entries, processes(), now())
        for f in flags:
            where = f"{f.bot}/{f.id}" if f.bot and f.id else "-"
            print(f"{f.kind:<8} {where:<16} {f.text}")
        if not flags and sys.stdout.isatty():
            print("watch: nothing to report")
        return 1 if flags else 0
    elif a.cmd == "board":
        snap = snapshot(d)
        if a.json:
            print(json.dumps(snap, indent=2, ensure_ascii=False))
        elif a.html == "-":
            sys.stdout.write(page(snap))
        elif a.html:
            with open(a.html, "w", encoding="utf-8") as f:
                f.write(page(snap))
            print(a.html)
        else:
            sys.stdout.write(text(snap))
    elif a.cmd == "serve":
        serve.serve(serve.configure(a.data or d, insecure_local=a.insecure_local, behind_proxy=a.behind_proxy,
                                    insecure_http=a.insecure_http, client_ip_header=a.client_ip_header),
                    a.port)
    elif a.cmd == "push":
        if not a.url or not a.token:
            raise ValueError("push needs --url and --token, or BOTROLL_PUSH_URL and BOTROLL_PUSH_TOKEN")
        removed: list[str] = []
        if a.yes and not a.prune:
            raise ValueError("--yes only goes with --prune")
        rolls, findings, skipped = serve.push(a.url, a.token, d, prune=a.prune, removed=removed, confirm=a.yes)
        verb = "pruned" if a.yes else "would prune (dry run; add --yes to delete)"
        print(f"pushed {rolls} roll file{'s' * (rolls != 1)} and {findings} finding{'s' * (findings != 1)}"
              + (f"; {verb} {len(removed)}" + (f": {', '.join(removed)}" if removed else "") if a.prune else ""))
        if skipped:
            return 1
    elif a.cmd == "health":
        cfg = health.load_config(a.config)
        if a.url and not a.token:
            raise ValueError("health --url needs --token or BOTROLL_PUSH_TOKEN")
        if cfg.get("control") and not a.url:
            print("botroll: control: true needs --url to pull commands", file=sys.stderr)
        if a.url and not cfg.get("control") and a.every:
            # Control is off here: refuse anything the server still has queued so it can't run later.
            try:
                for r in health.run_commands(cfg, health.fetch_commands(a.url, a.token)):
                    health.report_command(a.url, a.token, r["id"], r["status"], r["detail"], r["nonce"])
            except ValueError:
                pass  # server control off (404) or unreachable: nothing queued for us
        while True:
            started = time.monotonic()
            snap = health.sample(cfg)
            try:
                if a.url:
                    health.push(a.url, a.token, snap)
                else:
                    health.append(d, snap)
            except ValueError as e:
                if not a.every:
                    raise
                print(f"botroll: {e}", file=sys.stderr)
            if a.print:
                print(json.dumps(snap, indent=2, ensure_ascii=False), flush=True)
            if not a.every:
                break
            # Pull-model remote control: poll commands every ~10s between samples when enabled.
            wait = max(a.every - (time.monotonic() - started), 1)
            if cfg.get("control") and a.url:
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    try:
                        cmds = health.fetch_commands(a.url, a.token)
                        if cmds:
                            for r in health.run_commands(cfg, cmds, snap):
                                try:
                                    health.report_command(a.url, a.token, r["id"], r["status"], r.get("detail") or "",
                                                          r.get("nonce") or "")
                                except ValueError as e:
                                    print(f"botroll: control report: {e}", file=sys.stderr)
                                print(f"botroll: control {r['id']} {r['status']}: {r.get('detail')}", file=sys.stderr)
                    except ValueError as e:
                        print(f"botroll: control poll: {e}", file=sys.stderr)
                    time.sleep(min(10, max(deadline - time.monotonic(), 0.1)))
            else:
                time.sleep(wait)
    elif a.cmd == "hash-password":
        password = getpass.getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        serve.require_password(password)
        print(serve.hash_password(password))
    elif a.cmd == "email":
        mailer = notify.configure()
        if not mailer:
            raise ValueError("email is off: set BOTROLL_EMAIL_PROVIDER and the settings it needs")
        try:
            mailer.send(notify.hello(mailer.board))
        except OSError as e:
            raise ValueError(f"{mailer.provider} didn't take it: {e}") from None
        print(f"sent a test email to {', '.join(mailer.to)} via {mailer.provider}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
