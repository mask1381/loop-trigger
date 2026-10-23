from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loop_trigger.planner import desired_followups, desired_items, diff, norm_command, parse_key, render

log = logging.getLogger("loop_trigger")
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "queue.json"
FOLLOW_STATE_PATH = ROOT / "state" / "queue_collect.json"


def write_state(conf: dict, messages: list[dict], stats: dict, ok: bool, path: Path = STATE_PATH) -> None:
    """آینه‌ی صف تلگرام برای بات. بات خودش تلگرام را نمی‌بیند؛ این فایل تنها راه
    فهمیدن این است که واقعاً چه پیام‌هایی در صف هستند. کامیت روزانه‌اش هم
    heartbeat گیت‌هاب است تا workflow زمان‌بندی‌شده غیرفعال نشود.
    (صف چت دوم، queue_collect.json، فقط برای دیدن/عیب‌یابی است؛ بات نمی‌خواندش.)"""
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    try:
        shown = path.relative_to(ROOT)
    except ValueError:
        shown = path
    log.info("state written: %s (%s messages)", shown, len(messages))


def load_conf() -> dict:
    return json.loads((ROOT / "config" / "loop.json").read_text(encoding="utf-8"))


async def _entity(client, chat_id: int):
    try:
        return await client.get_entity(chat_id)
    except ValueError:
        await client.get_dialogs()
        return await client.get_entity(chat_id)


def _utc(d: datetime) -> datetime:
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _existing(client, entity) -> list[dict]:
    from telethon.tl import functions

    r = await client(functions.messages.GetScheduledHistoryRequest(peer=entity, hash=0))
    out = []
    for m in getattr(r, "messages", None) or []:
        d = getattr(m, "date", None)
        if d is None:
            continue
        out.append({"id": int(m.id), "date": _utc(d), "text": getattr(m, "message", "") or ""})
    return out


async def _snapshot(client, entity) -> list[dict]:
    out = [{"id": m["id"], "at": _iso(m["date"]), "text": m["text"]} for m in await _existing(client, entity)]
    out.sort(key=lambda x: x["at"])
    return out


async def _reconcile(client, entity, want, conf: dict, cap: int, dry: bool, label: str):
    """Bring one chat's scheduled queue to `want`. Returns None for a dry run,
    else (final_snapshot, stats, failed, missing_total)."""
    from telethon.tl import functions

    tol = timedelta(minutes=int(conf["tolerance_minutes"]))
    existing = await _existing(client, entity)
    delete_ids, add, reserve = diff(
        existing, want, tol, cap, norm_command(conf.get("rebuild_command", "/rebuild"))
    )
    log.info("[%s] queue=%s desired=%s delete=%s add=%s reserve_days=%s",
             label, len(existing), len(want), len(delete_ids), len(add), reserve)

    if dry:
        for i in add:
            log.info("DRY [%s] add %s %s", label, i.when.isoformat(timespec="minutes"), i.key)
        return None

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
                    log.warning("[%s] retry scheduling %s %s: %s", label, i.when.isoformat(timespec="minutes"), i.key, exc)
                    await asyncio.sleep(3)
            await asyncio.sleep(1.0)
        except Exception as exc:
            failed += 1
            log.error("[%s] could not schedule %s %s: %s", label, i.when.isoformat(timespec="minutes"), i.key, exc)
            if "SCHEDULE_TOO_MUCH" in str(exc):
                break
    log.info("[%s] done: added=%s failed=%s", label, len(add) - failed, failed)

    # وضعیت نهایی صف، همان چیزی که واقعاً در تلگرام هست
    final = await _snapshot(client, entity)
    stats = {
        "existing_before": len(existing),
        "desired": len(want),
        "deleted": len(delete_ids),
        "added": len(add) - failed,
        "failed": failed,
        "reserve_days": reserve,
        "at": _iso(datetime.now(timezone.utc)),
    }

    # تایید نهایی: هر آیتمی که فرستادیم باید در صف زمان‌بندی تلگرام دیده شود
    missing_total = 0
    if add and not failed:
        seen = [(parse_key(m["text"]), datetime.fromisoformat(m["at"].replace("Z", "+00:00"))) for m in final]
        missing = [i for i in add if not any(k == i.key and abs(d - i.when) <= tol for k, d in seen)]
        if missing:
            missing_total = len(missing)
            for i in missing[:10]:
                log.error("[%s] MISSING after send %s %s", label, i.when.isoformat(timespec="minutes"), i.key)
            log.error("[%s] %s scheduled messages are missing after sync", label, len(missing))
    return final, stats, failed, missing_total


async def _sync_followups(client, conf: dict, now: datetime, dry: bool) -> int:
    """چت دوم (COLLECT_CHAT_ID): بعد از هر پیام صف اصلی، delay_minutes بعدش یک /collect.
    خطای این بخش هیچ‌وقت صف اصلی را خراب نمی‌کند (صف اصلی قبلاً کامل شده)."""
    fu = conf.get("follow_up")
    if not fu or fu.get("enabled", True) is False:
        return 0
    chat = os.environ.get("COLLECT_CHAT_ID", "").strip()
    if not chat:
        log.error("follow_up is configured but the COLLECT_CHAT_ID secret is empty")
        return 1
    try:
        entity = await _entity(client, int(chat))
        cap = int(fu.get("max_scheduled", conf["max_scheduled"]))
        res = await _reconcile(client, entity, desired_followups(conf, now), conf, cap, dry, "collect")
        if res is None:
            return 0
        final, stats, failed, missing = res
        write_state(conf, final, stats, ok=(failed == 0 and missing == 0), path=FOLLOW_STATE_PATH)
        return 1 if (failed or missing) else 0
    except Exception:
        log.exception("follow-up sync failed")
        return 1


async def sync(conf: dict, dry: bool) -> int:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized")
        now = datetime.now(timezone.utc)
        code = 0

        entity = await _entity(client, int(os.environ["LOOP_CHAT_ID"].strip()))
        res = await _reconcile(client, entity, desired_items(conf, now), conf, int(conf["max_scheduled"]), dry, "main")
        if res is not None:
            final, stats, failed, missing = res
            write_state(conf, final, stats, ok=(failed == 0 and missing == 0))
            if failed or missing:
                code = 1

        code = max(code, await _sync_followups(client, conf, now, dry))
        return code
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
