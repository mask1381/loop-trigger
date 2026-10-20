from __future__ import annotations

"""Pure logic (no Telegram): what the queue SHOULD contain and how to get there.

A trigger message is exactly one command: "/word" (nothing else). You define the words in
config/loop.json; the serverless worker maps each word to an action.
    rebuild_command  -> the daily message that re-runs this repo (default "/rebuild")
    triggers[]       -> {"time": "10:00", "command": "/yourword"}
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CMD_RE = re.compile(r"^/[a-z0-9_]{1,32}$")  # Telegram command rules


@dataclass(frozen=True)
class Item:
    when: datetime  # aware, UTC
    key: str        # the command, e.g. "/rebuild"


def norm_command(cmd: str) -> str:
    c = str(cmd or "").strip().lower()
    if not c.startswith("/"):
        c = "/" + c
    if not CMD_RE.match(c):
        raise ValueError(f"Bad command {cmd!r}: use /letters_digits_underscore (max 32)")
    return c


def parse_key(text: str | None) -> str | None:
    """Owned message = exactly one '/word'. Anything else (your own notes) is foreign and untouched."""
    tokens = (text or "").split()
    if len(tokens) == 1 and CMD_RE.match(tokens[0].lower()):
        return tokens[0].lower()
    return None


def render(key: str) -> str:
    return key


def _hhmm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


# نام‌هایی که بات آن‌ها را دستور داخلی خودش می‌گیرد و هرگز به workflow نمی‌رسد.
# منبع اصلی، کلید reserved_commands در config/loop.json است؛ این فقط fallback است.
RESERVED_FALLBACK = frozenset({"start", "help", "cancel", "setsecret", "id", "panel"})


def reserved_names(conf: dict) -> set[str]:
    names = conf.get("reserved_commands")
    if not isinstance(names, list) or not names:
        names = RESERVED_FALLBACK
    return {str(n).strip().lstrip("/").lower() for n in names}


def desired_items(conf: dict, now: datetime) -> list[Item]:
    """One rebuild command per day + the configured triggers, from today through today+horizon_days,
    only slots still in the future, sorted by time."""
    tz = ZoneInfo(conf["timezone"])
    now = now.astimezone(tz)
    start = now + timedelta(minutes=int(conf["lead_minutes"]))
    reserved = reserved_names(conf)

    def slot(cmd: str, hhmm: str, label: str) -> tuple[str, str]:
        key = norm_command(cmd)
        if key.lstrip("/") in reserved:
            raise ValueError(f"{label}: {key} is reserved by the bot and would never dispatch")
        return key, hhmm

    slots: list[tuple[str, str]] = [slot(conf.get("rebuild_command", "/rebuild"), conf["rebuild_time"], "rebuild")]
    for i, t in enumerate(conf.get("triggers") or []):
        slots.append(slot(t["command"], t["time"], f"triggers[{i}]"))

    out: list[Item] = []
    for day in range(int(conf["horizon_days"]) + 1):
        d = (now + timedelta(days=day)).date()
        for key, hhmm in slots:
            h, m = _hhmm(hhmm)
            dt = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            if dt >= start:
                out.append(Item(dt.astimezone(timezone.utc), key))
    out.sort(key=lambda i: i.when)
    return out


def diff(existing: list[dict], desired: list[Item], tol: timedelta, cap: int, rebuild_key: str = "/rebuild"):
    """existing: [{"id", "date" (aware), "text"}].

    Returns (delete_ids, add_items, reserve_days).
    - owned ('/word') message with no matching desired slot (duplicate, moved or removed trigger) -> delete
    - desired slot with no owned message -> add
    - foreign messages are ignored but still count against the cap
    """
    owned, foreign = [], 0
    for m in existing:
        key = parse_key(m["text"])
        if key:
            owned.append((m, key))
        else:
            foreign += 1

    used: set[int] = set()
    missing: list[Item] = []
    for item in desired:
        hit = next(
            (m for m, k in owned if m["id"] not in used and k == item.key and abs(m["date"] - item.when) <= tol),
            None,
        )
        if hit:
            used.add(hit["id"])
        else:
            missing.append(item)

    delete_ids = [m["id"] for m, _ in owned if m["id"] not in used]
    room = max(0, cap - foreign - len(used))
    add = missing[:room]  # chronological: the farthest days are the ones cut
    kept_rebuilds = sum(1 for m, k in owned if m["id"] in used and k == rebuild_key)
    reserve_days = kept_rebuilds + sum(1 for i in add if i.key == rebuild_key)
    return delete_ids, add, reserve_days
