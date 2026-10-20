# loop-trigger

Keeps a dedicated Telegram chat filled with scheduled messages. Each message is a bare `/word` (you define the words) and is a trigger
that a serverless bot webhook turns into a GitHub `workflow_dispatch`.

- One `/rebuild` (name set by `rebuild_command`) per day at `rebuild_time` re-runs this repo, which refills the
  queue 9 days ahead. One surviving rebuild heals everything.
- `config/loop.json` -> `triggers` is empty for now. Entry: `{"time": "10:00", "command": "/yourword"}`.
  Commands: lowercase letters, digits, `_`, max 32. Message text is exactly the command.
- The queue is mirrored to `state/queue.json` after every run (see below). Any message that is not a single `/word` is ignored.
- Use a chat only for this (Telegram cap: 100 scheduled messages per chat; we stop at 95).

## How the loop closes

This repo is one half of a two-server loop; the Telegram chat is the middle.

1. This repo fills the chat's scheduled queue with bare `/word` messages.
2. The bot (`mask1381/triggeraidabot`, tgcloud) receives each message and turns it into a
   GitHub `workflow_dispatch`.
3. `/rebuild` dispatches this repo's `Loop` workflow, which refills the queue 9 days ahead.

The bot mirrors `rebuild_command`/`rebuild_time` from `config/loop.json`, so a missed
`/rebuild` message is recovered by its watchdog instead of silently breaking the loop; it
dispatches this repo's `loop.yml` even if `/rebuild` was never registered in its panel.

As a backstop that does not depend on Telegram at all, `loop.yml` also runs on a daily
cron (`00:30 UTC` = `04:00` Tehran, half an hour before the rebuild slot). If the bot, the
session, or the scheduled messages all stop, GitHub still refills the queue every day.
The publish step also commits `state/heartbeat.json` on every run, which keeps the repo
active so GitHub does not disable the cron in a public repo after ~60 days of inactivity.

## Queue mirror for the bot

The bot cannot see inside Telegram. After each run this repo publishes:

- `state/queue.json` — the real scheduled queue: `generated_at`, `timezone`, `rebuild`,
  `stats`, `total` / `owned` / `foreign`, and every message as `{id, at, text}`.
- `state/heartbeat.json` — `{"heartbeat_utc": "..."}`, written even when the refill fails.

The bot reads both from `main` every 5 minutes, shows them in its panel (🩺 سلامت زمان‌بندی)
and warns admins when the heartbeat is stale (workflow/cron dead) or when the heartbeat is
fresh but the queue is stale (Telegram session/chat access problem). A fresh heartbeat with
an empty queue also triggers a warning.

## One-time GitHub setup

1. **Secrets** — Settings → Secrets and variables → Actions:
   `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `LOOP_CHAT_ID`.
2. **Write permission for the commit step** — Settings → Actions → General →
   Workflow permissions → **Read and write permissions**. Without it the state push fails.
   If `main` is protected, allow the Actions bot to push (or the mirror will not publish).
3. **Enable the workflow** — Actions tab → `Loop` → if disabled, "Enable workflow".
   The `schedule` trigger only appears after the workflow file is on the default branch.
4. **Run it once by hand** — Actions → `Loop` → Run workflow (dry_run off). That creates
   `state/queue.json` and `state/heartbeat.json`, and the bot can see the mirror immediately.
5. **Bot side** — the tgcloud bot needs a GitHub token with *Contents: read* (private repo)
   and *Actions: read & write*: send `/setsecret gh <TOKEN>` in its private chat.

`config/loop.json` -> `reserved_commands` lists names the bot handles internally
(`start`, `help`, `cancel`, `setsecret`, `id`, `panel`). A trigger using one of those is
rejected here because it would never dispatch.

Secrets (GitHub Actions): `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `LOOP_CHAT_ID`.

    python -m loop_trigger --dry-run
    python test_planner.py
