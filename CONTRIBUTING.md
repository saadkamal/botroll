# Contributing

Thanks for helping. BotRoll aims to stay small, so the best change is often the one that removes something.

## Setup

You need Python 3.10 or newer on Linux or macOS. There is nothing to install for development:

```sh
git clone https://github.com/saadkamal/botroll && cd botroll
python3 -m unittest discover -s tests        # the tests
python3 -m unittest tests.test_serve         # one module (or tests.test_serve.Class.test_name)
ruff check .                                 # lint (pip install ruff)
python3 -m botroll --dir examples/tasks board   # run from the checkout
```

To install your checkout as the `botroll` command, run `pipx install -e .` or `uv tool install -e .`.

## Layout

| File | What it holds |
| --- | --- |
| `botroll/__init__.py` | the line format, reading and locked writing, staleness |
| `botroll/watch.py` | the watchdog: `ps` parsing and flags |
| `botroll/board.py` | the snapshot (JSON) and the terminal and HTML views |
| `botroll/board.css` | the board's styles, inlined into the page |
| `botroll/serve.py` | the self-hosted server, its sign-in and push API, and the push client |
| `botroll/notify.py` | optional email: what to send and when, the message text, and one small function per provider |
| `botroll/cli.py` | the command line |
| `Dockerfile`, `railway.toml` | the container and its Railway settings |
| `skill/SKILL.md` | the rules bots follow |
| `site/` | the botroll.co landing page (static HTML, no build) |

## Ages and the example team

Everything shown is relative to the current time. Set `BOTROLL_NOW="YYYY-MM-DD HH:MM"` to pin it. The example team in `examples/tasks` is written for `2026-10-10 15:30`, and the board tests use it.

When you change the board, render the examples and look at them at desktop and phone widths, in light and dark. Colours live in the `light-dark()` tokens at the top of `board.css`, and a test checks that every text colour still meets WCAG AA in both themes:

```sh
BOTROLL_NOW="2026-10-10 15:30" python3 -m botroll --dir examples/tasks board --html /tmp/board.html
```

The screenshots in `docs/` and `site/` come from `python3 scripts/screenshots.py`, which serves the example team (plus the invented flags and Box health data in `examples/`) and takes them with headless Chrome. Run it again after a visible change to the board. [examples/README.md](examples/README.md) explains what it uses.

## The website

`site/` is served as static files by a Cloudflare Worker, set up in `site/wrangler.jsonc`. To publish it, deploy from a clean export of `main` so local files don't get uploaded:

```sh
d=$(mktemp -d) && git archive main | tar -x -C "$d" && cd "$d/site"
CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=<account-id> npx wrangler deploy
```

`CLOUDFLARE_ACCOUNT_ID` is only needed when the token can't list your accounts. Files named in `site/.assetsignore` are not uploaded. Browsers cache the images for up to a day, so returning visitors may see the old ones for a while.

## Rules of thumb

- No runtime dependencies.
- Keep the file format backwards compatible. Old files must keep parsing, and lines BotRoll doesn't understand must survive a write.
- The watchdog reports. It never kills, renices or deletes.
- Add a test for every behaviour you add or fix. Tests use the standard library only.
- Write docs in plain prose.
- Record anything with a trade-off in [DECISIONS.md](DECISIONS.md), and user-facing changes in [CHANGELOG.md](CHANGELOG.md).

## Pull requests

CI runs `ruff check` (the rules are in `pyproject.toml`), then the tests on Linux and macOS for Python 3.10 and 3.14, then renders the example board. Please make sure `python3 -m unittest discover -s tests` passes before you open one.

## Conduct and security

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report security problems privately as described in [SECURITY.md](SECURITY.md), not in an issue.
