# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

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
