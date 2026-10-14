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

The PIDs in these files are made up, so on your machine the process flags (`pid`, `cpu`, `capture`) won't appear unless those PIDs happen to exist. The screenshots in `docs/` used real processes on the machine that took them:

- a busy Python loop recorded on Kai's (QA) open item, which became a `cpu` flag after an hour,
- an ffmpeg recorded on Vera's (Video) done item `T-2`, which became a `pid` flag,
- an ffmpeg recorded on nothing, which became a `capture` flag in the "No owner" card after 30 minutes,
- an ffmpeg recorded on Vera's open item `T-3`, which is tracked, so it isn't flagged.

To do the same, copy `tasks/` somewhere, start those processes, put their PIDs in the `pid:` fields, and wait.

## Running from cron

```cron
MAILTO=you@example.com
*/10 * * * * BOTROLL_DIR=$HOME/botroll botroll watch
*    * * * * BOTROLL_DIR=$HOME/botroll botroll board --html $HOME/Sync/board.html >/dev/null
```

Outside a terminal, `botroll watch` prints nothing when all is well, so the first line sends you mail only when it flags something. The second line keeps the board fresh.
