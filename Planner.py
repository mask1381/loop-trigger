from __future__ import annotations

"""Pure logic (no Telegram): what the queue SHOULD contain and how to get there.

Message formats (the serverless worker parses the first tokens, the rest is filler):
    /rebuild <word>                 -> run the loop repo again
    /t <target> <mode> <word>       -> dispatch <target> repo with <mode>
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WORDS = [
    "amber", "maple", "comet", "lotus", "ember", "velvet", "breeze", "coral", "mochi", "sakura",
    "pearl", "aurora", "cinder", "willow", "honey", "frost", "meadow", "twilight", "ruby", "petal",
    "sparrow", "ivory", "cobalt", "zephyr", "lunar", "ginger", "starlit", "dewdrop", "indigo", "pixel",
    "nectar", "harbor", "violet", "cloud", "tulip", "topaz", "river", "echo", "plum", "dawn",
]


@dataclass(frozen=True)
class Item:
    when: datetime  # aware, UTC
    key: str        # "rebuild" | "t:<target>:<mode>"


def parse_key(text: str | None) -> str | None:
    """Key of a message we own, else None (foreign messages are never touched)."""
    tokens = (text or "").split()
    if not tokens:
        return None
    if tokens[0] == "/rebuild":
        return "rebuild"
    if tokens[0] == "/t" and len(tokens) >= 3:
        return f"t:{tokens[1]}:{tokens[2]}"
    return None


def render(key: str, rng: random.Random | None = None) -> str:
    word = (rng or random).choice(WORDS)
    if key == "rebuild":
        return f"/rebuild {word}"
    _, target, mode = key.split(":", 2)
    return f"/t {target} {mode} {word}"


def _hhmm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


def desired_items(conf: dict, now: datetime) -> list[Item]:
    """Everything the queue should hold: one rebuild per day + the configured triggers.

    Calendar-day based: from today through today+horizon_days, only slots still in the future.
    Sorted by time and cut at max_scheduled by diff(), so the farthest days drop first.
    """
    tz = ZoneInfo(conf["timezone"])
    now = now.astimezone(tz)
    start = now + timedelta(minutes=int(conf["lead_minutes"]))
    slots: list[tuple[str, str]] = [("rebuild", conf["rebuild_time"])]
    for t in conf.get("triggers") or []:
        slots.append((f"t:{t['target']}:{t['mode']}", t["time"]))

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


def diff(existing: list[dict], desired: list[Item], tol: timedelta, cap: int):
    """existing: [{"id", "date" (aware), "text"}].

    Returns (delete_ids, add_items, reserve_days).
    - owned message with no matching desired slot (duplicate, moved or removed trigger) -> delete
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
    kept_rebuilds = sum(1 for m, k in owned if m["id"] in used and k == "rebuild")
    reserve_days = kept_rebuilds + sum(1 for i in add if i.key == "rebuild")
    return delete_ids, add, reserve_days
