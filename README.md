# loop-trigger

Keeps a dedicated Telegram chat filled with scheduled messages. Each message is a bare `/word` (you define the words) and is a trigger
that a serverless bot webhook turns into a GitHub `workflow_dispatch`.

- One `/rebuild` (name set by `rebuild_command`) per day at `rebuild_time` re-runs this repo, which refills the
  queue 9 days ahead. One surviving rebuild heals everything.
- `config/loop.json` -> `triggers` is empty for now. Entry: `{"time": "10:00", "command": "/yourword"}`.
  Commands: lowercase letters, digits, `_`, max 32. Message text is exactly the command.
- The queue itself is the state (no files committed). Any message that is not a single `/word` is ignored.
- Use a chat only for this (Telegram cap: 100 scheduled messages per chat; we stop at 95).

Secrets (GitHub Actions): `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `LOOP_CHAT_ID`.

    python -m loop_trigger --dry-run
    python test_planner.py
