# Examples

`tasks/` is a made-up team of six bots, with fictional names and roles: Ann (Chief of Staff), Ravi (Research), Sasha (Social), Max (Engineering), Kai (QA) and Vera (Video). It's set on Saturday 10 October 2026 at 15:30. It has 20 items in every state: four asks waiting on you (one past 12 hours), a bot that went quiet 8 hours ago, a blocked item and its blocker, a todo that was never started, done items from today and from earlier days, a dropped item, and recorded PIDs.

Pin the clock to that moment so the ages read as intended:

```sh
export BOTROLL_NOW="2026-10-10 15:30"
botroll --dir examples/tasks board
botroll --dir examples/tasks board --html board.html
botroll --dir examples/tasks list
botroll --dir examples/tasks watch
```

## Process flags

The PIDs in these files are made up, so on your machine the process flags (`pid`, `cpu`, `capture`) won't appear unless those PIDs happen to exist. [`watch-findings.json`](watch-findings.json) has three invented flags, in the form `botroll push` sends them, for the screenshots:

- a `cpu` flag on Kai's (QA) open item `T-22`,
- a `pid` flag on Vera's (Video) done item `T-2`, whose recorded ffmpeg is still running,
- a `capture` flag in the "No owner" card, for an ffmpeg no item claims.

## Box health

[`box-health.json`](box-health.json) describes an invented machine, `demo-box`: 8 cores, 16 GB, browsers for Ravi, Sasha and Vera, a stale helper, an ffmpeg job, and 24 hours of load in 30-minute steps. Nothing in it comes from a real machine.

## Screenshots

`python3 scripts/screenshots.py` rebuilds every image in `site/` and `docs/` from these files. It runs `botroll serve` on localhost with the clock pinned to 15:30, a throwaway password, secrets that start with `FAKE_TOKEN_DO_NOT_USE`, and action webhooks that are never called, then signs in with headless Chrome (Playwright). `--out DIR` writes the images somewhere else.

## Running from cron

```cron
MAILTO=you@example.com
*/10 * * * * BOTROLL_DIR=$HOME/botroll botroll watch
*    * * * * BOTROLL_DIR=$HOME/botroll botroll board --html $HOME/Sync/board.html >/dev/null
```

Outside a terminal, `botroll watch` prints nothing when all is well, so the first line sends you mail only when it flags something. The second line keeps the board fresh.
