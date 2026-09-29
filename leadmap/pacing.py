"""Human-like pacing: random delays, hourly caps, active-hours window. All sleeps are interruptible."""
import logging
import random
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class Stopped(Exception):
    """Raised inside sleeps when shutdown was requested."""


class Pacer:
    def __init__(self, cfg, db, stop: threading.Event, tick=None):
        self.p = cfg["pacing"]
        self.db = db
        self.stop = stop
        self.tick = tick or (lambda: None)  # called periodically during long sleeps
        self.tz = ZoneInfo(self.p["timezone"])

    # --- sleeping ------------------------------------------------------------
    def sleep(self, seconds: float, reason: str = ""):
        if seconds <= 0:
            return
        if seconds >= 60:
            log.info("Sleeping %s%s", _fmt(seconds), f" ({reason})" if reason else "")
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            if self.stop.wait(min(left, 30)):
                raise Stopped()
            if seconds >= 60:
                self.tick()

    def rand(self, key: str) -> float:
        lo, hi = self.p[key]
        return random.uniform(lo, hi)

    def scroll_delay(self):
        self.sleep(self.rand("scroll_delay_s"))
        if random.random() < self.p["long_pause_chance"]:
            self.sleep(self.rand("long_pause_s"), "long pause")

    def detail_delay(self):
        self.sleep(self.rand("detail_delay_s"))

    def query_pause(self):
        self.sleep(self.rand("query_pause_s"), "between queries")

    def tile_pause(self):
        self.sleep(self.rand("tile_pause_s"), "between tiles")

    def maybe_break(self):
        """20–40 min break every 8–12 tiles (counter persisted)."""
        st = self.db.get_state("pacing", {}) or {}
        since = st.get("tiles_since_break", 0) + 1
        target = st.get("break_after") or random.randint(*self.p["break_every_tiles"])
        if since >= target:
            self.db.set_state("pacing", {"tiles_since_break": 0,
                                         "break_after": random.randint(*self.p["break_every_tiles"])})
            self.sleep(self.rand("break_s"), f"break after {since} tiles")
        else:
            self.db.set_state("pacing", {"tiles_since_break": since, "break_after": target})

    # --- hourly caps ---------------------------------------------------------
    def _window(self, key):
        cutoff = time.time() - 3600
        return [t for t in (self.db.get_state(key, []) or []) if t > cutoff]

    def record(self, key: str):
        w = self._window(key)
        w.append(time.time())
        self.db.set_state(key, w)

    def count_last_hour(self, key: str) -> int:
        return len(self._window(key))

    def wait_for_cap(self, key: str, cap: int):
        w = self._window(key)
        if len(w) >= cap:
            self.sleep(min(w) + 3600 - time.time() + random.uniform(5, 60), f"hourly cap {key}={cap}")

    def detail_allowed(self) -> bool:
        return self.count_last_hour("detail_opens") < self.p["max_details_per_hour"]

    # --- active hours --------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def seconds_until_active(self, now: datetime | None = None) -> float:
        now = now or self.now()
        start, end = (_hm(s) for s in self.p["active_hours"])
        return seconds_until_window(now, start, end)

    def wait_active_hours(self):
        s = self.seconds_until_active()
        if s > 0:
            self.sleep(s + random.uniform(0, 600), "outside active hours")


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def seconds_until_window(now: datetime, start, end) -> float:
    """0 if `now` is inside [start, end) (window may wrap midnight), else seconds until start."""
    t = (now.hour, now.minute)
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if inside:
        return 0.0
    nxt = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _fmt(s: float) -> str:
    return f"{s / 3600:.1f}h" if s >= 3600 else f"{s / 60:.1f}min"
