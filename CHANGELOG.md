# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## 0.1.2, unreleased

- The app reports version 0.1.2 (`/health`, the `X-BotRoll-Version` header, `botroll --version`, the package metadata). The v0.1.1 tag (7c4c0f5) reported 0.1.0 by mistake because `__version__` wasn't bumped; v0.1.1 is left as is, and this release gets a fresh v0.1.2 tag.

## Unreleased

- The website and README screenshots are regenerated for 0.1.2 by `scripts/screenshots.py`, from the example team plus invented process flags (`examples/watch-findings.json`) and Box health data (`examples/box-health.json`). The site copy now covers board actions, Sync now, "Pushed … ago" and the stale-push banner, watchdog flags, Box health alerts and busiest periods, and `/health` showing the version and commit.
- Fixed: a page larger than the connection's send buffer (in practice a busy signed-in board of about 40 KB or more behind Railway's proxy) was cut short. The response writer did a single `send()` and ignored a partial write, so the body stopped early while `Content-Length` promised all of it, and browsers hung. Every response write now sends all its bytes.
- `botroll health --every` survives a bad iteration: a sample that fails or times out (such as a hung `ps`), a push or command poll that drops with any error (including `RemoteDisconnected`), and the start-up command fetch are logged, and the next sample follows. Before, any of these could end the collector.
- Close/Stop command polls and result reports go through the same sender as pushes: no redirects (the push token is never resent to another address) and dropped connections retried. A command whose report fails every retry is not run again when the next poll returns it; its result is reported again.
- Every line the collector writes to stderr starts with an ISO 8601 time with the UTC offset.
- The collector logs `stalled N s` when a sample starts more than twice `--every` late, with how far the monotonic clock moved, to tell a paused machine from a stuck process.
- The per-install health salt is opened with `O_NONBLOCK`, so a FIFO at `~/.botroll/health-salt` can no longer hang `botroll health`; a non-regular file there is replaced, and an existing `~/.botroll` you own is tightened to `0700`.
- PIDs on the Box health panel are set in a small monospace pill, so they stand apart from the profile label.
- Test modules can be run on their own by dotted name (`python3 -m unittest tests.test_serve`), and `test_push_url_alias` no longer depends on `BOTROLL_*` variables in your environment.
- `/health` reports the running version and commit (`ok`, then `version: 0.1.0` and `commit: <id>`) and sends `X-BotRoll-Version: 0.1.0+<12-char commit>`. The commit is baked in at build time: `git archive` stamps it via `export-subst`, the Dockerfile takes `--build-arg GIT_SHA=…` (or Railway's commit variable), and `BOTROLL_COMMIT` overrides it. The first line is still `ok`, so existing health checks keep working.
- The Railway steps deploy a `git archive` of a named commit with `railway up <dir> -p <project-id>`, and a missing line continuation in the `railway variables` example is fixed.
- `GET /api/health/commands` no longer changes anything. Commands queued while Box health control was on are cancelled when the server starts with it off (also when `BOTROLL_HEALTH` is off), which is the only time control can change.
- Every response has a `Permissions-Policy` header that turns off browser features the board doesn't use.
- Opt-in `BOTROLL_HOST_COOKIES=1` gives the cookies the `__Host-` prefix. It needs https, and turning it on signs everyone out once.
- Opt-in `BOTROLL_HEALTH_TOKEN`: when set, the only token the `/api/health` routes accept, and refused everywhere else. `botroll health` sends it ahead of `BOTROLL_PUSH_TOKEN`.
- `botroll push --prune --dry-run` never deletes, and warns and exits 3 when anything would be removed. Plain `--prune` is unchanged.
- **Behaviour change:** a time with a trailing `Z` or UTC offset (`2026-10-14T11:30:00Z`, `+11:00`, `-0500`, `+05`) is now converted to `BOTROLL_TZ` (or the machine's zone) instead of having the suffix dropped. A bot that writes UTC stamps no longer looks hours old or hours in the future. Times without a zone, including everything in the file format, read exactly as before, and so does a date on its own. `BOTROLL_TZ_OFFSETS=ignore` restores the old behaviour, for bots that wrote local times with a stray `Z`.
- Housekeeping: test files named by topic instead of by review round, dead code removed (`Handler.signing_key`, `board._today`, `health.hourly_peaks`, which was a wrapper for `bucket_peaks(..., bucket_min=60)`), `ruff check` in CI with a minimal rule set, the `Private :: Do Not Upload` classifier dropped, and the board screenshots kept once, under `site/`.
- `botroll push` and `botroll health` retry a dropped connection: always when it dropped before the request was sent, and after sending only for roll files and watchdog flags (never `/api/prune` or health samples). `BOTROLL_RETRIES` (default 3) and `BOTROLL_RETRY_BACKOFF` (default 1 second, doubling, capped at 30) tune it. A drop after sending now fails with a plain error instead of a traceback, so `botroll health --every` keeps running.
- Public-repo housekeeping: SECURITY.md, CODE_OF_CONDUCT.md, issue and pull request templates, README badges, features, a settings index and a security section.
- Examples, tests, screenshots and the site use neutral demo names (Ann, Max, Kai, Zoe) and generic paths.
- The default Box health `helper_match` is now `playwright-runtime` (a substring of the old default, so existing setups still match).

## 0.1.0, unreleased

The first version, built from the prototype the author ran across their bots.

- A file format of one markdown file per bot and one line per item. A write rewrites only the line it changes, so hand edits survive.
- `botroll add`, `set`, `note`, `done` and `list`, with locked atomic writes.
- The `waiting-human` state, written as `waiting-<BOTROLL_HUMAN>`.
- `botroll watch`, which reports stale items, live PIDs on closed items, CPU hogs, untracked capture processes and unknown states. It never kills anything.
- `botroll board`, a human view as a terminal summary, a static HTML page (`--html`) or JSON (`--json`).
- Optional `name:` and `role:` front matter in each bot's file. The board's cards, the "Needs you" strip, the summary sentence and `botroll list` show bots as "Name · Role", falling back to the slug.
- `botroll serve`, which hosts the live board for its owner behind a password, with signed session cookies, CSRF protection, rate-limited sign-in and strict security headers. It refuses to start without a password and a secret. A Dockerfile and `railway.toml` deploy it to Railway with a volume.
- `botroll push`, which uploads every roll file and the watchdog's process flags to a server with a push token. The served board shows when the last push arrived and warns when it's over 30 minutes old.
- `botroll hash-password`, for `BOTROLL_PASSWORD_HASH`.
- Optional email from `botroll serve` through Resend, Cloudflare Email Sending or SMTP. A "Needs you" email goes out when a bot starts waiting on you, batched to at most one every 15 minutes. A daily digest of what got done, what's stuck, quiet bots and watchdog findings goes out at `BOTROLL_DIGEST_HOUR`. What was sent is remembered across restarts, and bot text is escaped and its links defanged. `botroll email test` sends a test email.
- `BOTROLL_TZ`, the timezone the board's clock and the digest hour use.
- A monochrome look in light and dark: round avatars, tinted status chips, pill buttons and a centred, minimal sign-in page. The served board has a System / Light / Dark switch, kept in a cookie and rendered by the server. The emails match.
- Optional box health: `botroll health` samples memory headroom, load against the core count, browsers per bot (by `--user-data-dir`), DevTools tabs, stale helper processes, overlapping heavy jobs and stalls, keeps 24 hours, and pushes to `POST /api/health`. The board shows a Box health panel only when samples exist, with thresholds from `BOTROLL_HEALTH_*`. The server accepts samples only with `BOTROLL_HEALTH=1`, validates them strictly, rejects future, stale and replayed ones, rate-limits them to 6 a minute and caps the store at 4 MiB. Command lines are redacted to the executable and a label, tab origins lose any userinfo, and tab titles are cut to 60 characters (`tab_titles: false` drops them).
- Optional **Sync now** button on the served board: a signed `sync` action to a board-level endpoint (`"*"` in `actions.json` or `BOTROLL_SYNC_URL`), using the same delivery, dedupe and rate limit as item actions.
- `botroll push --prune` and `POST /api/prune`: list server roll files for bots no longer in the pushed directory (a dry run); `--prune --yes` deletes them, at most 3 an hour across calls (kept in `.prune.json`), never below half the bots at the start of the hour, never more than half in one call and never the last bot (409 over that).
- Box health: optional `profile_resolve` command hook and `browsers` map for profile→bot names; optional owner Close/Stop (`BOTROLL_HEALTH_CONTROL` + collector `control`) via pull-model commands; chart bucket minutes via `BOTROLL_HEALTH_BUCKET_MIN` (default 30).
- Board summary: rolling last-24h for done items (not calendar midnight); more robust time parsing. Done items up to 10 minutes in the future still count; an invalid `BOTROLL_TZ` falls back to UTC with a warning instead of failing requests.
- Box health: when two browsers share a name, the header and the Close confirm also show the profile; with the same folder name too, a short profile id (an HMAC of the path with a per-install salt in `~/.botroll/health-salt`), and `pid N` on any label still duplicated, so a copied id or a folder named like a label can't impersonate another browser.
- Box health hardening (QA round 2 lows): name files are opened non-blocking with O_NOFOLLOW and must be regular files; invisible and bidi characters are stripped from names on the collector and the board; Close needs the full `--user-data-dir` path in `browsers`; a node launcher's preload (`-r`/`--require`/`--import`) is never taken as the script, and `node -e`/`-p` has no script.
- Box health names without setup: `names_file` (`profile<TAB>name`), per-display name files in `display_names_dir` (default `~/.botroll/displays/<N>`, N from the browser's DISPLAY or a `-N` profile folder), then Chrome's own profile name as a labelled fallback.
- Box health control hardening: start-time tolerance 1s; Close only for browsers listed in `browsers` (exact profile match, no substring); Stop job matches `argv[0]` against a `stop_jobs` allowlist instead of a keyword anywhere; the collector refuses expired commands, checks targets against its own latest sample and refuses everything with `control` off; the server cancels pending commands when control is off; results must echo a per-command nonce; Close/Stop stays on screen at phone widths.
- An agent skill, `skill/SKILL.md`, which also works as an `AGENTS.md` snippet.
- The marketing site at `site/`, for [botroll.co](https://botroll.co).
- Session cookies bind the password and a version bumped on sign-out, so logout and a password change revoke a copied cookie.
- `--behind-proxy` reads `X-Forwarded-Proto` for HSTS. Client IP is the socket peer unless `BOTROLL_CLIENT_IP_HEADER` / `--client-ip-header` is set. `--insecure-http` drops the Secure cookie flag for a plain-http LAN.
- Each connection has a 20s absolute deadline covering headers and body. Unauthenticated traffic is capped so `/health` and signed-in requests stay available.
- Passwords reject whitespace-only values, a small list of common strings, fewer than 5 distinct characters, and equality with `BOTROLL_SECRET`. `botroll push` exits 1 when a roll is skipped.
- Optional board actions: the owner can Approve, Decline, Reply or Nudge an item; BotRoll POSTs a signed JSON event to that bot's URL. Off with no config. The board never changes item state. A click is recorded pending before send and keyed by id; outbound connects to the validated IP with a 10s absolute deadline.
- A bad `.notify.json` is moved aside instead of crash-looping the server. "Needs you" emails send within five minutes even if asks keep arriving.
- The Docker image copies only the package, ignores `*.env`, drops to uid 1000, and handles SIGTERM.
- `BOTROLL_PUSH_URL` is the push target; `BOTROLL_URL` remains an alias. Passwords are at least 12 characters and must differ from the push token and the secret.
