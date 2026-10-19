from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loop_trigger.planner import desired_items, diff, render

log = logging.getLogger("loop_trigger")
ROOT = Path(__file__).resolve().parents[1]


def load_conf() -> dict:
    return json.loads((ROOT / "config" / "loop.json").read_text(encoding="utf-8"))


async def _entity(client, chat_id: int):
    try:
        return await client.get_entity(chat_id)
    except ValueError:
        await client.get_dialogs()
        return await client.get_entity(chat_id)


async def sync(conf: dict, dry: bool) -> int:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl import functions

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized")
        entity = await _entity(client, int(os.environ["LOOP_CHAT_ID"].strip()))

        res = await client(functions.messages.GetScheduledHistoryRequest(peer=entity, hash=0))
        existing = []
        for m in getattr(res, "messages", None) or []:
            d = getattr(m, "date", None)
            if d is None:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            existing.append({"id": int(m.id), "date": d, "text": getattr(m, "message", "") or ""})

        now = datetime.now(timezone.utc)
        want = desired_items(conf, now)
        tol = timedelta(minutes=int(conf["tolerance_minutes"]))
        delete_ids, add, reserve = diff(existing, want, tol, int(conf["max_scheduled"]))
        log.info("queue=%s desired=%s delete=%s add=%s reserve_days=%s",
                 len(existing), len(want), len(delete_ids), len(add), reserve)

        if dry:
            for i in add:
                log.info("DRY add %s %s", i.when.isoformat(timespec="minutes"), i.key)
            return 0

        if delete_ids:
            await client(functions.messages.DeleteScheduledMessagesRequest(peer=entity, id=delete_ids))
        failed = 0
        for i in add:
            try:
                await client.send_message(entity, render(i.key), schedule=i.when)
                await asyncio.sleep(1.0)
            except Exception as exc:
                failed += 1
                log.error("could not schedule %s %s: %s", i.when.isoformat(timespec="minutes"), i.key, exc)
                if "SCHEDULE_TOO_MUCH" in str(exc):
                    break
        log.info("done: added=%s failed=%s", len(add) - failed, failed)
        return 1 if failed else 0
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Refill the scheduled trigger queue")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    dry = args.dry_run or os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}
    try:
        return asyncio.run(sync(load_conf(), dry))
    except Exception:
        log.exception("loop failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
