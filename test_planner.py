from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loop_trigger.planner import desired_items, diff, norm_command, parse_key, render

TZ = ZoneInfo("Asia/Tehran")
CONF = {"timezone": "Asia/Tehran", "rebuild_command": "/rebuild", "rebuild_time": "04:30", "horizon_days": 9,
        "lead_minutes": 3, "tolerance_minutes": 1, "max_scheduled": 95, "triggers": [],
        "reserved_commands": ["start", "help", "cancel", "setsecret", "id", "panel"]}
TOL = timedelta(minutes=1)


class Q:
    """Fake scheduled queue that applies a diff the way Telegram would."""
    def __init__(self): self.msgs, self.n = [], 0
    def apply(self, dele, add):
        self.msgs = [m for m in self.msgs if m["id"] not in dele]
        for i in add:
            self.n += 1
            self.msgs.append({"id": self.n, "date": i.when, "text": render(i.key)})


def step(q, conf, now):
    d, a, r = diff(q.msgs, desired_items(conf, now), TOL, conf["max_scheduled"], conf["rebuild_command"])
    q.apply(d, a)
    return d, a, r


def fire(q, now):
    q.msgs = [m for m in q.msgs if m["date"] > now.astimezone(timezone.utc)]


def test_parse_and_render():
    assert render("/rebuild") == "/rebuild"
    assert parse_key("/Rebuild") == "/rebuild"
    assert parse_key("hello") is None and parse_key("/a b") is None and parse_key("my note") is None
    assert norm_command("wl") == "/wl"
    try:
        norm_command("/bad word"); assert False
    except ValueError:
        pass


def test_fresh_fill_then_idempotent():
    q, now = Q(), datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    d, a, r = step(q, CONF, now)
    assert not d and r == 9 and len(a) == 9, (len(a), r)
    d, a, r = step(q, CONF, now)
    assert not d and not a and r == 9


def test_one_rebuild_per_day_forever():
    q, now = Q(), datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    for _ in range(40):
        step(q, CONF, now)
        days = {m["date"].astimezone(TZ).date() for m in q.msgs if parse_key(m["text"]) == "/rebuild"}
        assert len(days) == 9 and len(q.msgs) == 9
        now += timedelta(days=1)
        fire(q, now)


def test_survives_missed_rebuilds():
    q, now = Q(), datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    step(q, CONF, now)
    for _ in range(8):
        now += timedelta(days=1)
        fire(q, now)
    assert any(parse_key(m["text"]) == "/rebuild" for m in q.msgs)
    step(q, CONF, now)
    assert sum(parse_key(m["text"]) == "/rebuild" for m in q.msgs) == 9


def test_heals_missing_and_duplicates():
    q, now = Q(), datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    step(q, CONF, now)
    dup = dict(q.msgs[0]); dup["id"] = 999
    q.msgs.append(dup)
    q.msgs.pop(3)
    d, a, r = step(q, CONF, now)
    assert d == [999] and len(a) == 1 and r == 9


def test_triggers_added_moved_removed_and_foreign_ignored():
    q, now = Q(), datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    conf = {**CONF, "triggers": [{"time": "10:00", "command": "/wl"}]}
    step(q, conf, now)
    assert sum(parse_key(m["text"]) == "/wl" for m in q.msgs) == 10
    q.msgs.append({"id": 5000, "date": now.astimezone(timezone.utc) + timedelta(days=1), "text": "my own note"})
    conf["triggers"][0]["time"] = "11:00"
    d, a, _ = step(q, conf, now)
    assert len(d) == 10 and len(a) == 10 and 5000 not in d
    conf["triggers"] = []
    d, a, _ = step(q, conf, now)
    assert len(d) == 10 and not a


def test_reserved_trigger_rejected():
    now = datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    for bad in ("/id", "/start", "help"):
        try:
            desired_items({**CONF, "triggers": [{"time": "10:00", "command": bad}]}, now)
            assert False, bad
        except ValueError:
            pass


def test_cap_respected():
    conf = {**CONF, "triggers": [{"time": f"{h:02d}:00", "command": f"/c{h}"} for h in range(5, 14)]}
    q = Q(); step(q, conf, datetime(2026, 10, 18, 4, 31, tzinfo=TZ))
    assert len(q.msgs) <= 95


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
