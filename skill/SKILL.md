---
name: botroll
description: Use whenever you accept, start, finish, drop, or get blocked on a task from your human, and whenever you start a background process. Keeps your items in your BotRoll file so nothing lives only in chat.
---
# BotRoll

Your items live in a shared file, one per bot: `$BOTROLL_DIR/<your-slug>.md` (default `~/.botroll`). Use the `botroll` command. Hand edits are allowed.

The file starts with front matter giving your `name:` and `role:`, such as `name: Kai` and `role: QA`. Your human shows these on the board. Leave them as they are unless your human asks you to change them. Commands and ids always use your slug, the file name, as in `qa/T-22`.

## Rules

1. **Record it in the same turn you accept it.** Run `botroll add <slug> "<what>"` before you start the work. Add `--state todo` if it's queued for later.
2. **One line per part.** If the ask has three parts, add three lines, even when the parts are small. A part that blocks must not stop the others from starting.
3. **Update on every state change.** Use `botroll set <slug> <id> <state> --note "<why>"`. Use `botroll note <slug> <id> "<text>"` for progress that matters.
4. **Record the PID of anything you start in the background**, such as a recording, ffmpeg, a server, or a long script: `botroll set <slug> <id> doing --pid <pid>`. Stop it before you close the item.
5. **Mark done only after delivery**, meaning your human has the thing, not just that it was built: `botroll done <slug> <id> --note "<where it went>"`.
6. **When you need your human**, use `waiting-human` with a note that says exactly what you need, as a question they can answer in one line. Ask in chat as well. Keep working on the parts that aren't blocked. When you are blocked on anyone or anything else, use `blocked` and name the blocker, such as `--note "needs qa/T-22"`.
7. **When your human drops something**, use `dropped`. Don't delete the line.
8. **At the start of a turn**, run `botroll list <slug>` to see your open items. A `!` before the reason means the watchdog flagged it: doing or blocked with no update for 6h, waiting-human for 12h, or todo for 24h. Clear a flag by updating or finishing the item.

States: `todo`, `doing`, `waiting-human`, `blocked`, `done`, `dropped`. `waiting-<name>` and `waiting` also work. Refer to another bot's item as `<bot>/T-<n>`.

## Actions from the board

If your human has configured an action URL for your slug, they can click Approve, Decline, Reply or Nudge on the live board. You then receive a POST of JSON `{event:"botroll.action", action, bot, item_id, item_title, state, note, actor:"owner", board_url, sent_at, id}`. The signature is `X-BotRoll-Signature: t=<unix>,v1=<hex>` (HMAC-SHA256 of `{t}.{raw body}` with the shared secret), and/or a static header they configured. Treat `id` as an idempotency key: if you see the same `id` again, do not do the work twice.

**Do the action, then update your roll.** The board does not change the item for you. After you act:

1. `botroll set <slug> <id> <new-state> --note "<what you did>"` (or `botroll note` / `botroll done`).
2. If they self-host, the next `botroll push` (usually cron) refreshes the board. You do not call the server yourself.

Treat Approve as permission to continue, Decline as stop (usually `dropped` or `blocked`), Reply as an answer in `note`, and Nudge as "still on this?" — update if you have progress, or say you're stuck.
