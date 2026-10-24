from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loop_trigger.planner import desired_followups, desired_items, diff, norm_command, parse_key, render

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


# ───────── follow-up (/collect در چت دوم) ─────────
FU_CONF = {**CONF, "triggers": [{"time": "10:00", "command": "/wl"}],
           "follow_up": {"command": "/collect", "delays_minutes": [30, 55]}}


def test_followup_two_per_main_message():
    now = datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    main = desired_items(FU_CONF, now)
    fu = desired_followups(FU_CONF, now)
    got = {i.when for i in fu}
    for d in (30, 55):
        assert {i.when + timedelta(minutes=d) for i in main} <= got
    assert all(i.key == "/collect" for i in fu)
    # rebuild 04:30 already fired 1 min ago, but both follow-ups (05:00, 05:25) are still ahead
    for hhmm in ((5, 0), (5, 25)):
        assert datetime(2026, 10, 18, *hhmm, tzinfo=TZ).astimezone(timezone.utc) in got


def test_followup_legacy_single_delay_still_works():
    now = datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    conf = {**FU_CONF, "follow_up": {"command": "/collect", "delay_minutes": 55}}
    main = desired_items(conf, now)
    got = {i.when for i in desired_followups(conf, now)}
    assert {i.when + timedelta(minutes=55) for i in main} <= got
    assert datetime(2026, 10, 18, 5, 0, tzinfo=TZ).astimezone(timezone.utc) not in got


def test_followup_exclude_and_same_minute_dedupe():
    now = datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    conf = {**FU_CONF, "follow_up": {"delays_minutes": [30, 55], "exclude": ["/rebuild"]}}
    fu = {i.when for i in desired_followups(conf, now)}
    for hhmm in ((5, 0), (5, 25)):
        assert datetime(2026, 10, 18, *hhmm, tzinfo=TZ).astimezone(timezone.utc) not in fu
    twin = {**FU_CONF, "triggers": [{"time": "10:00", "command": "/a"}, {"time": "10:00", "command": "/b"}]}
    whens = [i.when for i in desired_followups(twin, now)]
    assert len(whens) == len(set(whens))


def test_followup_truncated_to_cap_earliest_first():
    now = datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    conf = {**FU_CONF, "follow_up": {**FU_CONF["follow_up"], "max_scheduled": 10}}
    fu = desired_followups(conf, now)
    full = desired_followups({**conf, "follow_up": {**conf["follow_up"], "max_scheduled": 95}}, now)
    assert len(fu) == 10 and [i.when for i in fu] == [i.when for i in full][:10]


def test_followup_full_old_queue_gets_near_slots():
    """صف پر از /collect های قدیمی (+55) نباید جلوی اضافه شدن +30 های نزدیک را بگیرد."""
    now = datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    old = {**FU_CONF, "follow_up": {"command": "/collect", "delay_minutes": 55}}
    q = Q()
    d, a, _ = diff(q.msgs, desired_followups(old, now), TOL, 95, "/rebuild")
    q.apply(d, a)
    d, a, _ = diff(q.msgs, desired_followups(FU_CONF, now), TOL, 95, "/rebuild")
    q.apply(d, a)
    assert len(q.msgs) <= 95
    whens = {m["date"] for m in q.msgs}
    assert datetime(2026, 10, 18, 5, 0, tzinfo=TZ).astimezone(timezone.utc) in whens


def test_followup_queue_steady_state_no_churn():
    q, now = Q(), datetime(2026, 10, 18, 4, 31, tzinfo=TZ)
    for _ in range(20):
        d, a, _ = diff(q.msgs, desired_followups(FU_CONF, now), TOL, 95, "/rebuild")
        q.apply(d, a)
        d2, a2, _ = diff(q.msgs, desired_followups(FU_CONF, now), TOL, 95, "/rebuild")
        assert not d2 and not a2          # idempotent
        now += timedelta(days=1)
        fire(q, now)
    assert len(q.msgs) <= 95


def test_followup_real_config_fits_cap():
    import json, pathlib
    conf = json.loads((pathlib.Path(__file__).parent / "config" / "loop.json").read_text(encoding="utf-8"))
    n = len(desired_followups(conf, datetime(2026, 10, 18, 4, 31, tzinfo=TZ)))
    assert 0 < n <= conf["max_scheduled"], n


def test_followup_real_config_fits_cap():
    import json, pathlib
    conf = json.loads((pathlib.Path(__file__).parent / "config" / "loop.json").read_text(encoding="utf-8"))
    n = len(desired_followups(conf, datetime(2026, 10, 18, 4, 31, tzinfo=TZ)))
    assert 0 < n <= conf["max_scheduled"], n


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
