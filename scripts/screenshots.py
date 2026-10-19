#!/usr/bin/env python3
"""Regenerate the screenshots in site/ and docs/ from the example team. Everything shown is invented.

    python3 scripts/screenshots.py            # writes site/*.png and docs/*.png
    python3 scripts/screenshots.py --out DIR  # writes every image into DIR instead

It copies examples/tasks to a temporary folder, adds the process flags in examples/watch-findings.json, turns
examples/box-health.json into 24 hours of Box health samples, and runs `botroll serve` on localhost with a throwaway password, fake secrets and webhook targets
that are never called. The clock is pinned with BOTROLL_NOW to the moment the example team is written for.
A headless Chrome (Playwright) then signs in and takes each picture. Needs Playwright for Python and a
Chrome or Chromium: BOTROLL_CHROME picks the binary (default /usr/bin/google-chrome if it exists). With
Pillow installed, the images are saved as 256-colour PNGs to keep them small.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from botroll.serve import hash_password  # noqa: E402

NOW = "2026-10-10 15:30"  # the example team's moment (examples/README.md)
TZ = "Europe/London"
PUSHED_MIN_AGO = 4  # "Pushed 4m ago"
PASSWORD = "FAKE_PASSWORD_DO_NOT_USE"
FAKE = "FAKE_TOKEN_DO_NOT_USE"  # every secret below starts with this and is never sent anywhere
HOOK = "http://127.0.0.1/botroll-demo-hook"  # never called: nothing is clicked
DESKTOP, WIDE, PHONE = (1280, 900), (1440, 900), (390, 844)

OG = """<!doctype html><html><head><meta charset="utf-8"><style>
body { margin: 0; width: 1200px; height: 630px; background: #fff; color: #111;
  font-family: Inter, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
.wrap { padding: 56px 72px; }
.brand { font-size: 22px; font-weight: 700; letter-spacing: 0.08em; }
h1 { font-size: 76px; font-weight: 400; letter-spacing: -0.03em; line-height: 1.12; margin: 58px 0 18px; }
p { font-size: 25px; color: #6b6b6b; margin: 0 0 36px; }
.rows { border-top: 2px solid #111; }
.row { display: flex; align-items: center; height: 54px; border-bottom: 1px solid #e4e2de; font-size: 25px; }
.no { width: 68px; font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 16px; color: #6b6b6b; }
.who { flex: 1; }
.pill { font-size: 17px; padding: 5px 14px; border-radius: 999px; }
.you { background: #fbeed8; color: #8f5100; } .doing { background: #dcf2ed; color: #08705f; }
.quiet { background: #fbeed8; color: #8f5100; }
</style></head><body><div class="wrap">
<div class="brand">BOTROLL</div>
<h1>The roll call<br>for your bots.</h1>
<p>One plain-text roll per bot. A board humans read.</p>
<div class="rows">
<div class="row"><span class="no">01</span><span class="who">Ann · Chief of Staff</span><span class="pill you">Needs you</span></div>
<div class="row"><span class="no">02</span><span class="who">Max · Engineering</span><span class="pill quiet">Gone quiet</span></div>
<div class="row"><span class="no">03</span><span class="who">Kai · QA</span><span class="pill doing">Working</span></div>
</div></div></body></html>"""


def epoch(when: str) -> int:
    return int(datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(TZ)).timestamp())


def health_samples(spec: dict, at: int) -> list[dict]:
    """One sample per 30 minutes from spec["history"] (oldest first), then the latest snapshot at `at`."""
    base = {"v": 1, "host": spec["host"], "cores": spec["cores"], "helpers": [], "jobs": [], "alerts": []}
    hist = spec["history"]
    out = []
    for i, (load, avail, bot) in enumerate(hist):
        ts = at - (len(hist) - i) * 1800 + 900
        out.append(base | {"ts": ts, "load": [load, load, load],
                           "mem": {"total_mb": spec["total_mb"], "available_mb": avail},
                           "browsers": [{"profile": bot.lower(), "bot": bot, "procs": 8, "rss_mb": 1200, "cpu": 50}]})
    last = spec["latest"]
    browsers = []
    for b in last["browsers"]:
        b = dict(b)
        running = b.pop("running_s", None)
        if running is not None:
            b["started_at"] = at - running
        browsers.append(b)
    out.append(base | {"ts": at, "load": last["load"],
                       "mem": {"total_mb": spec["total_mb"], "available_mb": last["available_mb"]},
                       "browsers": browsers,
                       "helpers": [h | {"started_at": at - int(h["elapsed_s"])} for h in last["helpers"]],
                       "jobs": [j | {"started_at": at - int(j["elapsed_s"])} for j in last["jobs"]],
                       "alerts": last["alerts"]})
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(data: pathlib.Path, health: bool) -> tuple[subprocess.Popen, str]:
    port = free_port()
    actions = data.parent / "actions.json"
    actions.write_text(json.dumps({slug: {"url": HOOK, "secret": f"{FAKE}_{slug}_000000000000"}
                                   for slug in ("assistant", "social", "*")}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOTROLL_")} | {
        "BOTROLL_NOW": NOW, "BOTROLL_TZ": TZ, "BOTROLL_PASSWORD_HASH": hash_password(PASSWORD),
        "BOTROLL_SECRET": f"{FAKE}_session_secret_000000000", "BOTROLL_PUSH_TOKEN": f"{FAKE}_push_token_0000000000",
        "BOTROLL_ACTIONS": str(actions), "BOTROLL_ACTIONS_ALLOW_HTTP": "1", "BOTROLL_ACTIONS_ALLOW_PRIVATE": "1",
        "BOTROLL_HEALTH": "1" if health else "0", "BOTROLL_HEALTH_CONTROL": "1" if health else "0",
        "PYTHONPATH": str(ROOT)}
    # A relative data folder, so the board's footer reads "data" rather than a temporary path.
    proc = subprocess.Popen([sys.executable, "-m", "botroll", "serve", "--dir", data.name, "--data", data.name,
                             "--port", str(port), "--insecure-http"], env=env, cwd=str(data.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=1).read()
            return proc, url
        except OSError:
            if proc.poll() is not None:
                raise SystemExit(f"botroll serve exited: {proc.stderr.read()}")
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("botroll serve did not start")


def team(tmp: pathlib.Path, name: str, health: bool) -> pathlib.Path:
    data = tmp / name / "data"
    shutil.copytree(ROOT / "examples" / "tasks", data)
    at = epoch(NOW)
    if health:
        spec = json.loads((ROOT / "examples" / "box-health.json").read_text(encoding="utf-8"))
        (data / ".health.json").write_text(json.dumps(health_samples(spec, at)))
    # The watchdog's process flags, as `botroll push` would deliver them: invented PIDs, so no real process
    # has to run. On a real setup they come from `botroll watch` on the bots' machine.
    shutil.copy(ROOT / "examples" / "watch-findings.json", data / ".watch.json")
    pushed = at - PUSHED_MIN_AGO * 60
    for f in [*data.glob("*.md"), data / ".watch.json"]:
        os.utime(f, (pushed, pushed))
    return data


def sign_in(browser, url: str, size, scale: int):
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=scale,
                              color_scheme="light", is_mobile=scale > 1, has_touch=scale > 1)
    page = ctx.new_page()
    page.goto(url + "/login")
    page.fill('input[name="password"]', PASSWORD)
    with page.expect_navigation():
        page.locator('input[name="password"]').press("Enter")
    if "/login" in page.url:
        raise SystemExit("sign-in failed")
    return ctx, page


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write every image into this folder instead of site/ and docs/")
    a = ap.parse_args()
    out = {n: (pathlib.Path(a.out) / n if a.out else ROOT / d / n) for d, n in (
        ("site", "board-desktop.png"), ("site", "board-phone.png"), ("site", "health-desktop.png"),
        ("site", "health-phone.png"), ("site", "og.png"), ("docs", "box-health-desktop.png"),
        ("docs", "sign-in.png"))}
    for p in out.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    chrome = os.environ.get("BOTROLL_CHROME") or ("/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None)
    with tempfile.TemporaryDirectory(prefix="botroll-shots-") as t, sync_playwright() as pw:
        tmp = pathlib.Path(t)
        browser = pw.chromium.launch(executable_path=chrome, headless=True)
        servers = []
        try:
            board, board_url = start(team(tmp, "board", health=False), health=False)
            box, box_url = start(team(tmp, "box", health=True), health=True)
            servers += [board, box]
            # The sign-in page, on a phone (a plain screenshot: the page is short).
            ctx = browser.new_context(viewport={"width": PHONE[0], "height": PHONE[1]}, device_scale_factor=2,
                                      color_scheme="light", is_mobile=True, has_touch=True)
            pg = ctx.new_page()
            pg.goto(board_url + "/login")
            pg.screenshot(path=str(out["sign-in.png"]))
            ctx.close()
            for url, size, scale, name in ((board_url, DESKTOP, 1, "board-desktop.png"),
                                           (board_url, PHONE, 2, "board-phone.png"),
                                           (box_url, WIDE, 1, "health-desktop.png"),
                                           (box_url, PHONE, 2, "health-phone.png")):
                ctx, pg = sign_in(browser, url, size, scale)
                pg.screenshot(path=str(out[name]), full_page=True)
                if name == "health-desktop.png":
                    pg.locator("section.health").screenshot(path=str(out["box-health-desktop.png"]))
                ctx.close()
            ctx = browser.new_context(viewport={"width": 1200, "height": 630}, color_scheme="light")
            pg = ctx.new_page()
            pg.set_content(OG)
            pg.screenshot(path=str(out["og.png"]))
            ctx.close()
        finally:
            browser.close()
            for s in servers:
                s.terminate()
                s.wait(10)
    shrink(out.values())
    for p in out.values():
        print(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)


def shrink(paths) -> None:
    """Re-save as 256-colour PNGs, like the images in the repo, when Pillow is installed. The board's flat
    colours survive this; it makes the files several times smaller."""
    try:
        from PIL import Image
    except ImportError:
        print("Pillow is not installed, so the images stay full-colour (larger files)", file=sys.stderr)
        return
    for p in paths:
        with Image.open(p) as im:
            small = im.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        small.save(p, optimize=True)


if __name__ == "__main__":
    main()
