# Decisions

Why BotRoll is built the way it is, and what was left out. Newest decisions go at the bottom.

## Plain files, one line per item

The prototype that came before this package ran for real across several bots, and its format held up, so it stays: one markdown file per bot, one line per item, ` · ` between fields. Bots and people can both read and edit it, `git diff` shows what changed, and a bot's whole list fits on a screen. A write rewrites only the item it changes. Every other line, whether an item or not, is kept byte for byte, so a human can write notes in the same file and their formatting survives.

The middle dot is reserved as the separator. The CLI turns any `·` in a title or note into `-` rather than escaping it.

## One lock per directory, atomic replace

Every write takes an exclusive `flock` on `<dir>/.lock`, re-reads the file, and replaces it with `os.replace`, so readers never see a half-written file and concurrent writers never lose an item. The test suite has four processes adding 25 items each to one file. The prototype used a lock file per bot, which put one extra file next to every bot's file. Writes are rare and quick, so one lock costs nothing in practice.

## No dependencies, Python 3.10+, Linux and macOS

Installing it should never conflict with anything a bot already runs. The standard library covers parsing, locking, `ps` and HTML. Tests use `unittest`. Windows is out because `fcntl` and `ps` are; WSL works.

## `waiting-human`, written with your name

The state is `waiting-human` in code and JSON. In files it's written as `waiting-<BOTROLL_HUMAN>`, so `waiting-sam` reads naturally to the bots and to you. Any state that starts with `waiting` parses as `waiting-human`, which keeps old prototype files (`waiting-saad`) working and forgives bots that write `waiting`.

## Local times without a zone

Times are `YYYY-MM-DD HH:MM` in the machine's local time, as the prototype had them, because people edit these lines by hand. The cost is that ages can be an hour out across a daylight-saving change. That doesn't matter for thresholds measured in hours.

## The watchdog reports, never acts

Killing the wrong process costs more than a report, and the bot that started a process is the one that knows whether it's safe to stop. The thresholds (6h, 12h, 24h, 90% CPU for 1h, capture over 30 minutes) are constants. They came from the prototype's real use. Making them configurable would add flags and a config file before anyone has needed a different number.

`ps -A -o pid=,ppid=,etime=,time=,args=` is the one form that works on both Linux and macOS. Both print elapsed and CPU time as `[dd-][hh:]mm:ss[.cc]`, which `watch.seconds` parses.

A capture process counts as tracked if it, or an ancestor, is a recorded PID on an open item. A recorded PID on a closed item is reported once, as a `pid` flag, not again as a `capture` flag.

## One snapshot, three views

`board.snapshot()` gathers everything once as plain data, and that data is the JSON output. A small `card()` function turns it into what each bot's card says, and both the HTML page and the terminal view render from that, so they can't disagree.

The HTML is one file with inline CSS, so it works opened from disk, synced to a phone, or served by anything. It makes no requests. It reloads every minute so a cron job can keep it live. Its only script is one line that collapses done items on narrow screens. Without JavaScript they simply stay open.

The design is direction B ("roll call") from the design review, with direction A's one-sentence summary on top. The rejected mockups stayed in the design pull request rather than in this repository.

## The look

The owner chose a monochrome, airy look modelled on a chat app's sidebar: white or near-black pages, warm off-white or dark grey panels separated by fill rather than borders, a neo-grotesk sans in regular weight with tight tracking, pill buttons, and colour only in small places. Each bot gets a round avatar in one of five soft hues, picked from a hash of its name so it never changes, and every status is a tinted chip: amber for waiting on you, teal for working, red-pink for blocked or stuck, grey for quiet, idle and done. BotRoll uses no logos, marks or names from the app that inspired it.

Every colour is a CSS variable set with `light-dark()`, so there is one list of tokens for both themes. `tests/test_board.py` reads them from `board.css` and checks every text colour against every surface it sits on for WCAG AA (4.5:1) in both themes. The faintest grey from the reference, `#A0A0A0`, fails that on white, so BotRoll doesn't use it for text. The font stack starts with Inter where it's installed and falls back to the system sans. Nothing is downloaded, because the page makes no requests and its CSP allows none.

The theme follows the device by default. On the served board, a System / Light / Dark switch posts a form to `/theme`, which sets a cookie, and the server writes the choice into `<html data-theme>`, so there is no flash on load and no script involved. The form carries the CSRF token like every other form. The static `board --html` file has no server to remember a choice, so it always follows the device.

Emails use the same light tokens inline, with system fonts, because mail clients drop `<style>` and don't support `light-dark()`.

## No MCP server

Every bot that can run a shell can run `botroll`, and the skill that teaches it is a page long. An MCP server would be a second interface to keep in step, a long-running process, and a dependency, for no new capability. If a bot platform appears that can call MCP tools but can't run commands, that would be the time to add one.

## Names and roles in front matter

The owner asked for every card to say who the bot is as well as what it does ("Ann · Chief of Staff"). Both live in optional front matter at the top of the bot's file (`name:` and `role:`), because that is where markdown tools already look for metadata, and it keeps the item lines untouched. The slug stays the file name and the identity in commands and ids (`assistant/T-8`), so renaming a bot never breaks a reference. Without front matter the board shows the slug. The earlier rule that the first `# Heading` was the display name is gone. Front matter replaces it, and two ways to say the same thing would be one too many.

## Self-hosting: push files, render on request

The owner wanted the board reachable from anywhere, on Railway first. The bots' files stay the source of truth on their own machine. `botroll push` uploads each roll file whole to `POST /api/roll/<bot>`, and the server validates the slug, parses the file and stores it with the same lock and atomic replace as any other write. The server renders the board from those files on every request, so there is no database and no cache to go stale. Pushing whole files rather than changes keeps the server simple, and a roll file is a few kilobytes.

The host's processes say nothing about the bots, so the server never runs `ps`. The watchdog's process flags (pid, cpu, capture) are computed on the bots' machine and pushed to `/api/watch` in the same `botroll push`, replacing the last set. Stale and state flags are recomputed on the server from the files. One command for both means one cron line, and the flags can't drift from the files they describe. The last push time is the newest stored file's modification time. After 30 minutes without one, the board says it may be out of date, because a quiet board and a broken cron job otherwise look the same.

A push replaces files but never deletes them, so a bot deleted locally stays on the board until its file is removed from the volume. Deleting on push would mean that pushing from the wrong directory wipes the board.

Everything is standard library: `http.server` with a thread per request, `hashlib.scrypt` for the password, HMAC-SHA256 for cookies. The server is meant for one owner and a handful of requests a minute, not for traffic.

## One owner, signed cookies, fail closed

Sign-in is one password, at least 12 characters with 5 different ones, and different from the push token and the secret. The session is a cookie holding an expiry time and its HMAC. The HMAC key is the secret mixed with a stable id of the password and a version stored in `.session` next to the rolls. Signing out bumps that version, so a copied cookie dies with it. Changing the password (or `BOTROLL_PASSWORD_HASH`) changes the id, which does the same. A restart without those changes does not sign anyone out. Forms carry a CSRF token: an HMAC of a random value in a second cookie, checked on every POST. Cookies are HttpOnly, Secure and SameSite=Lax. `--insecure-http` drops Secure for a plain-http LAN, and prints a warning. Each page's Content-Security-Policy allows the hashes of the known board CSS and the one collapse script, not whatever `<style>` or `<script>` happens to be in the body, so an escaping bug cannot whitelist itself.

Sign-in attempts are counted per IP address in memory, before the password is checked so that parallel guesses can't outrun the count, and cleared on success: 5 in 15 minutes, then 429 until the oldest expires. At most four scrypt checks run at once, since each takes 16 MB. A restart forgets the counts, which is acceptable at one password per server and scrypt's cost per guess. The address is the socket peer unless `--behind-proxy` is set **and** `--client-ip-header` / `BOTROLL_CLIENT_IP_HEADER` names a header a trusted edge overwrites (on Railway, `X-Real-IP`). `--behind-proxy` alone only reads `X-Forwarded-Proto` for HSTS. The Docker image does not imply a trusted client-IP header. Private networking and a TCP proxy can forge `X-Real-IP` and `X-Forwarded-For` if they are trusted blindly.

The push token is separate from the password and compared in constant time, and bodies over 256 KB are refused unread. Without a token the push API is off. The server refuses to start without a password and a 32-character secret. An open board would show everything your bots are doing to anyone with the address, so the only way to run without sign-in is `--insecure-local`, which also binds to 127.0.0.1 and answers only requests addressed to localhost, so a web page can't reach it through DNS rebinding.

`botroll push` never follows a redirect, because urllib would resend the push token to wherever it points, over plain http too.

The container starts as root so a Railway volume at `/data` can be chowned, then `serve()` drops to uid 1000 and checks that the data directory is writable. It handles SIGTERM so a stop or redeploy does not wait for a kill. The image copies only the package files; `*.env` is ignored, so a `botroll.env` next to `docker build .` is not baked into a layer.

## Email: two kinds, off by default

The owner asked for email, but only two kinds, so a busy day can't flood an inbox. "Needs you" goes out when an ask newly appears. It waits for a minute without new asks, so a bot that asks three things in a row sends one email, and it sends at most one every 15 minutes. If asks keep arriving, it still sends within five minutes of the oldest waiting one, so a trickle cannot starve the inbox. The digest goes out once a day at a set hour, and not at all on an empty day. Stale, blocked and process findings go in the digest, not in their own emails, because they rarely need an answer within the hour.

The server decides what to send by comparing each minute's board with `.notify.json` in the data directory: the asks already emailed, the ones waiting for the next email, and the day of the last digest. Keeping that file next to the roll files means a restart or redeploy, which keeps the volume, never resends. A file that is valid JSON but not a state object (`[]`, `42`, wrong-typed fields) is moved aside to `.notify.json.bad-<ts>` and replaced with a fresh state, so it cannot crash-loop `railway.toml`'s `ON_FAILURE` restart or silence email forever. An ask that stops waiting is dropped from the file, so asking again is news again. The cost is that turning email on the first time sends everything already waiting, in one email, which is also the moment the owner most wants to see it.

Each provider is one function that turns a message into one request: JSON over `urllib` for Resend and Cloudflare's REST API, `smtplib` for SMTP. There is no SDK and no dependency. Sends are retried twice within seconds, then backed off from 2 minutes up to an hour, with every failure logged. The check runs in a background thread that catches everything, so email trouble never reaches the board.

Bot text is untrusted in an email in a way it isn't on the board, because a mail client turns anything that looks like a link into one. Subjects carry only counts. Bodies escape bot text, strip control, invisible and direction-changing characters, and defang `://`, anything shaped like a domain in any script (`evil[.]example`, `аpple[.]com`), full-width dots (folded by NFKC), bare IPv4, IPv6, hex IPv4 (`0x7f000001`), dotted hex (`0x7f.0.0.1`) and integer IPv4 (`2130706433`). The one link is to the server's `BOTROLL_URL`, which must be https. On the bots' machine the same name is an alias for `BOTROLL_PUSH_URL`. Defanging also catches harmless names like `notes.md`. That was judged a fair price, since the alternative is guessing which domains are real.

`BOTROLL_TZ` sets the zone for the digest hour, and for the board's clock too, so "done today" in the digest and on the board are the same day.

## Cut from the prototype

- `sync-sheet`, which wrote rows to a Google Sheet. `botroll board --json` gives the same data to any script.
- Writing `pid: none` on every new item. An absent field says the same thing in less space.
- `list --stale`. `list` now marks stale items with `!` and the reason, so one command answers both questions.
- The separate `waiting:` field. `note:` covers it. A hand-written `waiting:` is still kept as written.
- The hard-coded timezone and directory. These are now the machine's local time and `BOTROLL_DIR`.

## Actions: the board wakes the bot, the bot updates the roll

Optional, off with no config, one small module. The owner clicks Approve / Decline / Reply / Nudge; BotRoll POSTs a signed JSON event to a URL that bot (or any webhook) already accepts. The board never writes an item line. The bot does the work, updates its roll, and the next push refreshes the card. That keeps the board a view, not a second source of truth, and keeps the feature generic: Grok routines, Zapier, n8n, or a five-line HTTP handler.

HTTPS only, no redirects, a 10s absolute deadline, non-global addresses blocked (`not ip.is_global`, so CGNAT too), HMAC Stripe-style plus an optional static header for receivers that cannot verify a signature. A click is locked and written as pending to `.actions.json` (keyed by id) before the POST, then marked done or failed, so a double-click or a restart does not send twice. The outbound socket is pinned to the validated IP with Host and TLS SNI set to the hostname.

## Not built

These were all considered and left out until someone needs them: a config file beyond the optional actions map, more than one user on a server, editing item state from the web board, `botroll init`, deleting items, editing titles from the CLI, and per-item history.
