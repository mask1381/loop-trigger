# loop-trigger

Keeps a dedicated Telegram chat filled with scheduled messages. Each message is a trigger
that a serverless bot webhook turns into a GitHub `workflow_dispatch`.

- One `/rebuild <word>` per day (default 04:30) re-runs this repo, which refills the queue
  to `horizon_days` (9) ahead. One surviving rebuild is enough to heal everything.
- `config/loop.json` -> `triggers` is empty for now. Entry shape:
  `{"time": "10:00", "target": "waifu-lab", "mode": "plan"}`
- The queue itself is the state (no files committed). Foreign messages in the chat are ignored.
- Use a chat only for this (Telegram cap: 100 scheduled messages per chat; we stop at 95).

Secrets (GitHub Actions): `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `LOOP_CHAT_ID`.

    python -m loop_trigger --dry-run
    python -m tests.test_planner
