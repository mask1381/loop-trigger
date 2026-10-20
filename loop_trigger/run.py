from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loop_trigger.planner import desired_items, diff, norm_command, parse_key, render

log = logging.getLogger("loop_trigger")
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "queue.json"


def write_state(conf: dict, messages: list[dict], stats: dict, ok: bool) -> None:
    """آینه‌ی صف تلگرام برای بات. بات خودش تلگرام را نمی‌بیند؛ این فایل تنها راه
    فهمیدن این است که واقعاً چه پیام‌هایی در صف هستند. کامیت روزانه‌اش هم
    heartbeat گیت‌هاب است تا workflow زمان‌بندی‌شده غیرفعال نشود."""
    owned = [m for m in messages if parse_key(m["text"])]
    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ok": ok,
        "timezone": conf.get("timezone", "Asia/Tehran"),
        "rebuild": {
            "command": norm_command(conf.get("rebuild_command", "/rebuild")),
            "time": conf.get("rebuild_time"),
        },
        "horizon_days": conf.get("horizon_days"),
        "cap": conf.get("max_scheduled"),
        "stats": stats,
        "total": len(messages),
        "owned": len(owned),
        "foreign": len(messages) - len(owned),
        "messages": messages,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    try:
        shown = STATE_PATH.relative_to(ROOT)
    except ValueError:
        shown = STATE_PATH
    log.info("state written: %s (%s messages)", shown, len(messages))


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

        async def snapshot() -> list[dict]:
            r = await client(functions.messages.GetScheduledHistoryRequest(peer=entity, hash=0))
            out = []
            for m in getattr(r, "messages", None) or []:
                d = getattr(m, "date", None)
                if d is None:
                    continue
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                out.append({
                    "id": int(m.id),
                    "at": d.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "text": getattr(m, "message", "") or "",
                })
            out.sort(key=lambda x: x["at"])
            return out

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
        delete_ids, add, reserve = diff(
            existing, want, tol, int(conf["max_scheduled"]), norm_command(conf.get("rebuild_command", "/rebuild"))
        )
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
                for attempt in range(2):
                    try:
                        await client.send_message(entity, render(i.key), schedule=i.when)
                        break
                    except Exception as exc:
                        # یک بار retry برای خطاهای گذرا (timeout / flood)
                        if attempt == 1 or "SCHEDULE_TOO_MUCH" in str(exc):
                            raise
                        log.warning("retry scheduling %s %s: %s", i.when.isoformat(timespec="minutes"), i.key, exc)
                        await asyncio.sleep(3)
                await asyncio.sleep(1.0)
            except Exception as exc:
                failed += 1
                log.error("could not schedule %s %s: %s", i.when.isoformat(timespec="minutes"), i.key, exc)
                if "SCHEDULE_TOO_MUCH" in str(exc):
                    break
        log.info("done: added=%s failed=%s", len(add) - failed, failed)

        # وضعیت نهایی صف، همان چیزی که واقعاً در تلگرام هست
        final = await snapshot()
        stats = {
            "existing_before": len(existing),
            "desired": len(want),
            "deleted": len(delete_ids),
            "added": len(add) - failed,
            "failed": failed,
            "reserve_days": reserve,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

        # تایید نهایی: هر آیتمی که فرستادیم باید در صف زمان‌بندی تلگرام دیده شود
        missing_total = 0
        if add and not failed:
            now_ids = [(parse_key(m["text"]), datetime.fromisoformat(m["at"].replace("Z", "+00:00"))) for m in final]
            missing = [
                i for i in add
                if not any(k == i.key and abs(d - i.when) <= tol for k, d in now_ids)
            ]
            if missing:
                missing_total = len(missing)
                for i in missing[:10]:
                    log.error("MISSING after send %s %s", i.when.isoformat(timespec="minutes"), i.key)
                log.error("%s scheduled messages are missing after sync", len(missing))

        write_state(conf, final, stats, ok=(failed == 0 and missing_total == 0))
        return 1 if (failed or missing_total) else 0
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
