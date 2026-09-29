"""Central hub: a queue of cities shared by many scraper servers (workers).

Each worker claims one city at a time, reports progress and pushes its places in heartbeats,
and reports the city finished before claiming the next. The hub keeps a merged copy of all
places (deduped by place id) and exports it to Excel. Served by the dashboard process.
"""
import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import now_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cities (
    name        TEXT PRIMARY KEY,
    position    REAL NOT NULL,                 -- queue order (lower first)
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued/active/done/error
    worker_id   INTEGER,
    assigned_at TEXT,
    finished_at TEXT,
    last_error  TEXT,
    tiles       TEXT,                          -- JSON {status: n} last reported by the worker
    added_at    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen  TEXT,
    ip         TEXT,
    hostname   TEXT,
    paused     INTEGER NOT NULL DEFAULT 0,
    revoked    INTEGER NOT NULL DEFAULT 0,
    report     TEXT                            -- JSON: last heartbeat (phase, backoff, tile, ...)
);

CREATE TABLE IF NOT EXISTS places (
    place_id   TEXT PRIMARY KEY,
    name       TEXT,
    phone_raw  TEXT,
    phone      TEXT,
    is_mobile  INTEGER NOT NULL DEFAULT 0,
    address    TEXT,
    lat        REAL,
    lng        REAL,
    category   TEXT,
    rating     REAL,
    maps_url   TEXT,
    city       TEXT,
    tile_id    TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    worker_id  INTEGER
);
CREATE INDEX IF NOT EXISTS hub_places_city ON places(city);
CREATE INDEX IF NOT EXISTS hub_places_first_seen ON places(first_seen);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    type      TEXT NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

PLACE_FIELDS = ("place_id", "name", "phone_raw", "phone", "is_mobile", "address", "lat", "lng",
                "category", "rating", "maps_url", "city", "tile_id", "first_seen", "last_seen")
MAX_BATCH = 1000
MAX_CITY_LEN = 100


class HubError(Exception):
    """A request the hub refuses; `code` is the HTTP status."""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class HubDB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def _tx(self):
        """Write transaction that takes the lock up front (claims must not race)."""
        conn = self.conn

        class _T:
            def __enter__(self):
                conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc, *_):
                conn.execute("ROLLBACK" if exc else "COMMIT")
        return _T()

    def event(self, type_: str, detail: str = ""):
        self.conn.execute("INSERT INTO events(timestamp, type, detail) VALUES(?, ?, ?)",
                          (now_iso(), type_, detail[:2000]))

    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key, value):
        self.conn.execute("INSERT INTO state(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value, ensure_ascii=False)))

    # --- workers -------------------------------------------------------------
    def add_worker(self, name: str) -> str:
        """Create a worker; returns its token (only the hash is stored)."""
        name = (name or "").strip()
        if not name or len(name) > 60:
            raise HubError(400, "worker name must be 1–60 characters")
        token = secrets.token_urlsafe(32)
        try:
            self.conn.execute("INSERT INTO workers(name, token_hash, created_at) VALUES(?, ?, ?)",
                              (name, hash_token(token), now_iso()))
        except sqlite3.IntegrityError:
            raise HubError(409, f"worker {name!r} already exists") from None
        self.event("worker", f"added {name}")
        return token

    def worker_by_token(self, token: str):
        if not token:
            return None
        row = self.conn.execute("SELECT * FROM workers WHERE token_hash=?",
                                (hash_token(token),)).fetchone()
        return None if row is None or row["revoked"] else row

    def worker_action(self, worker_id: int, action: str):
        w = self.conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        if w is None:
            raise HubError(404, "no such worker")
        if action in ("pause", "resume"):
            self.conn.execute("UPDATE workers SET paused=? WHERE id=?", (int(action == "pause"), worker_id))
        elif action == "revoke":
            with self._tx():
                self.conn.execute("UPDATE workers SET revoked=1 WHERE id=?", (worker_id,))
                self._requeue_worker_cities(worker_id)
        else:
            raise HubError(400, f"unknown action {action!r}")
        self.event("worker", f"{action} {w['name']}")

    def _requeue_worker_cities(self, worker_id: int):
        self.conn.execute("UPDATE cities SET status='queued', worker_id=NULL, assigned_at=NULL, "
                          "updated_at=? WHERE worker_id=? AND status='active'", (now_iso(), worker_id))

    def _seen(self, w, ip: str, info: dict):
        self.conn.execute("UPDATE workers SET last_seen=?, ip=?, hostname=COALESCE(?, hostname) WHERE id=?",
                          (now_iso(), ip, _s(info.get("hostname"), 100), w["id"]))

    # --- cities --------------------------------------------------------------
    def add_cities(self, names: list[str]) -> list[str]:
        added = []
        with self._tx():
            pos = self.conn.execute("SELECT COALESCE(MAX(position), 0) FROM cities").fetchone()[0]
            for n in names:
                n = " ".join((n or "").split())
                if not n or len(n) > MAX_CITY_LEN:
                    continue
                pos += 1
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO cities(name, position, added_at, updated_at) VALUES(?, ?, ?, ?)",
                    (n, pos, now_iso(), now_iso()))
                if cur.rowcount:
                    added.append(n)
        if added:
            self.event("city", "queued " + "، ".join(added))
        return added

    def city_action(self, name: str, action: str):
        with self._tx():
            c = self.conn.execute("SELECT * FROM cities WHERE name=?", (name,)).fetchone()
            if c is None:
                raise HubError(404, "no such city")
            if action in ("up", "down", "top"):
                self._move(c, action)
            elif action == "release":        # active → back to the queue (worker drops it)
                if c["status"] != "active":
                    raise HubError(409, "city is not active")
                self._set_city(name, status="queued", worker_id=None, assigned_at=None)
            elif action == "requeue":        # done/error → queue again (remaining/failed tiles)
                if c["status"] not in ("done", "error"):
                    raise HubError(409, "only finished or failed cities can be re-queued")
                self._set_city(name, status="queued", finished_at=None, last_error=None)
            elif action == "delete":
                self.conn.execute("DELETE FROM cities WHERE name=?", (name,))
            else:
                raise HubError(400, f"unknown action {action!r}")
        self.event("city", f"{action} {name}")

    def _move(self, c, action):
        rows = self.conn.execute("SELECT name, position FROM cities ORDER BY position").fetchall()
        if action == "top":
            first = rows[0]["position"] if rows else 0
            self._set_city(c["name"], position=first - 1)
            return
        idx = next(i for i, r in enumerate(rows) if r["name"] == c["name"])
        j = idx - 1 if action == "up" else idx + 1
        if 0 <= j < len(rows):
            other = rows[j]
            self._set_city(c["name"], position=other["position"])
            self._set_city(other["name"], position=c["position"])

    def _set_city(self, name, **fields):
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE cities SET {cols} WHERE name=?", (*fields.values(), name))

    def assignment(self, worker_id: int):
        row = self.conn.execute("SELECT name FROM cities WHERE worker_id=? AND status='active' "
                                "ORDER BY assigned_at LIMIT 1", (worker_id,)).fetchone()
        return row["name"] if row else None

    # --- worker API ------------------------------------------------------------
    def claim(self, w, ip: str, info: dict) -> dict:
        """Current assignment, else a preferred queued city (one the worker already started), else the queue head."""
        with self._tx():
            self._seen(w, ip, info)
            if w["paused"]:
                return {"city": self.assignment(w["id"]), "paused": True}
            city = self.assignment(w["id"])
            if city is None:
                prefer = [p for p in (info.get("prefer") or []) if isinstance(p, str)][:50]
                row = None
                for p in prefer:
                    row = self.conn.execute("SELECT name FROM cities WHERE name=? AND status='queued'",
                                            (p,)).fetchone()
                    if row:
                        break
                row = row or self.conn.execute(
                    "SELECT name FROM cities WHERE status='queued' ORDER BY position LIMIT 1").fetchone()
                if row:
                    city = row["name"]
                    self._set_city(city, status="active", worker_id=w["id"], assigned_at=now_iso(),
                                   last_error=None)
                    self.event("assign", f"{city} → {w['name']}")
        return {"city": city, "paused": False}

    def heartbeat(self, w, ip: str, info: dict) -> dict:
        places = info.get("places") or []
        if not isinstance(places, list) or len(places) > MAX_BATCH:
            raise HubError(400, f"places must be a list of at most {MAX_BATCH}")
        with self._tx():
            self._seen(w, ip, info)
            report = {k: info.get(k) for k in ("phase", "city", "tile", "backoff", "progress",
                                               "version", "tiles_last_hour", "sync_backlog")}
            self.conn.execute("UPDATE workers SET report=? WHERE id=?",
                              (json.dumps(report, ensure_ascii=False)[:5000], w["id"]))
            city = info.get("city")
            if isinstance(city, str) and isinstance(info.get("tiles"), dict):
                self.conn.execute("UPDATE cities SET tiles=?, updated_at=? WHERE name=? AND worker_id=?",
                                  (json.dumps(_tile_counts(info["tiles"])), now_iso(), city, w["id"]))
            accepted = sum(self._upsert_place(p, w["id"]) for p in places)
            assignment = self.assignment(w["id"])
        return {"assignment": assignment, "paused": bool(w["paused"]), "accepted": accepted}

    def complete(self, w, ip: str, info: dict) -> dict:
        city = info.get("city")
        with self._tx():
            self._seen(w, ip, info)
            c = self.conn.execute("SELECT * FROM cities WHERE name=?", (city,)).fetchone()
            if c and c["worker_id"] == w["id"] and c["status"] == "active":
                tiles = _tile_counts(info.get("tiles") or {})
                self._set_city(city, status="done", finished_at=now_iso(), tiles=json.dumps(tiles))
                self.event("done", f"{city} by {w['name']}: {tiles}")
        return {"ok": True}

    def fail(self, w, ip: str, info: dict) -> dict:
        """The worker cannot scrape this city at all (e.g. OSM has no such place)."""
        city = info.get("city")
        err = _s(info.get("error"), 500) or "unknown error"
        with self._tx():
            self._seen(w, ip, info)
            c = self.conn.execute("SELECT * FROM cities WHERE name=?", (city,)).fetchone()
            if c and c["worker_id"] == w["id"] and c["status"] == "active":
                self._set_city(city, status="error", last_error=err, finished_at=now_iso())
                self.event("error", f"{city} by {w['name']}: {err}")
        return {"ok": True}

    def _upsert_place(self, p, worker_id: int) -> int:
        if not isinstance(p, dict) or not isinstance(p.get("place_id"), str) or not p["place_id"]:
            return 0
        rec = {k: p.get(k) for k in PLACE_FIELDS}
        for k in ("lat", "lng", "rating"):
            rec[k] = _f(rec[k])
        for k in ("place_id", "name", "phone_raw", "phone", "address", "category", "maps_url",
                  "city", "tile_id", "first_seen", "last_seen"):
            rec[k] = _s(rec[k], 1000)
        rec["is_mobile"] = int(bool(rec["is_mobile"]))
        rec["first_seen"] = rec["first_seen"] or now_iso()
        rec["last_seen"] = rec["last_seen"] or rec["first_seen"]
        rec["worker_id"] = worker_id
        self.conn.execute(
            "INSERT INTO places(place_id, name, phone_raw, phone, is_mobile, address, lat, lng, category, "
            "rating, maps_url, city, tile_id, first_seen, last_seen, worker_id) VALUES "
            "(:place_id, :name, :phone_raw, :phone, :is_mobile, :address, :lat, :lng, :category, "
            ":rating, :maps_url, :city, :tile_id, :first_seen, :last_seen, :worker_id) "
            "ON CONFLICT(place_id) DO UPDATE SET name=COALESCE(excluded.name, name), "
            "phone_raw=COALESCE(excluded.phone_raw, phone_raw), phone=COALESCE(excluded.phone, phone), "
            "is_mobile=CASE WHEN excluded.phone IS NULL THEN is_mobile ELSE excluded.is_mobile END, "
            "address=COALESCE(excluded.address, address), lat=COALESCE(excluded.lat, lat), "
            "lng=COALESCE(excluded.lng, lng), category=COALESCE(excluded.category, category), "
            "rating=COALESCE(excluded.rating, rating), maps_url=COALESCE(excluded.maps_url, maps_url), "
            "city=COALESCE(city, excluded.city), tile_id=COALESCE(tile_id, excluded.tile_id), "
            "first_seen=MIN(first_seen, excluded.first_seen), last_seen=MAX(last_seen, excluded.last_seen)",
            rec)
        return 1

    # --- panel -----------------------------------------------------------------
    def overview(self, stale_after_s: int = 600) -> dict:
        now = datetime.now(timezone.utc)
        stats = {r["city"]: dict(r) for r in self.conn.execute(
            "SELECT city, COUNT(*) places, SUM(phone IS NOT NULL) phones, SUM(is_mobile) mobiles "
            "FROM places GROUP BY city")}
        names = {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM workers")}
        cities = []
        for r in self.conn.execute("SELECT * FROM cities ORDER BY status='done', position"):
            s = stats.get(r["name"], {})
            cities.append({
                "name": r["name"], "status": r["status"], "worker": names.get(r["worker_id"]),
                "assigned_at": r["assigned_at"], "finished_at": r["finished_at"],
                "last_error": r["last_error"], "tiles": json.loads(r["tiles"] or "{}"),
                "places": s.get("places") or 0, "phones": s.get("phones") or 0,
                "mobiles": s.get("mobiles") or 0, "updated_at": r["updated_at"],
            })
        workers = []
        for r in self.conn.execute("SELECT * FROM workers WHERE revoked=0 ORDER BY id"):
            seen = r["last_seen"]
            age = (now - datetime.fromisoformat(seen)).total_seconds() if seen else None
            placed = self.conn.execute("SELECT COUNT(*) FROM places WHERE worker_id=?", (r["id"],)).fetchone()[0]
            workers.append({
                "id": r["id"], "name": r["name"], "hostname": r["hostname"], "ip": r["ip"],
                "last_seen": seen, "age_s": age, "online": age is not None and age < stale_after_s,
                "paused": bool(r["paused"]), "city": self.assignment(r["id"]),
                "report": json.loads(r["report"] or "{}"), "places": placed,
            })
        t = self.conn.execute("SELECT COUNT(*) n, SUM(phone IS NOT NULL) p, SUM(is_mobile) m FROM places").fetchone()
        events = [dict(r) for r in self.conn.execute(
            "SELECT timestamp, type, detail FROM events ORDER BY id DESC LIMIT 15")]
        return {"cities": cities, "workers": workers, "events": events,
                "totals": {"places": t["n"] or 0, "phones": t["p"] or 0, "mobiles": t["m"] or 0}}


def _s(v, n):
    return None if v is None else str(v)[:n]


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _tile_counts(d: dict) -> dict:
    return {str(k)[:20]: int(v) for k, v in d.items() if isinstance(v, int)}


# --- export ------------------------------------------------------------------------
def safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)


def export_hub(cfg, hub: HubDB, day: date | None = None) -> str:
    """exports/hub/: all_leads.xlsx, one file per city, and leads_<day>.xlsx (first seen that day)."""
    from .export import COLUMNS, _frame, _write
    cols = ["city"] + COLUMNS
    tz = ZoneInfo(cfg["pacing"]["timezone"])
    day = day or datetime.now(tz).date()
    out = cfg.path("exports") / "hub"
    out.mkdir(parents=True, exist_ok=True)

    allp = _frame(hub, tz, columns=cols)
    _write(allp, out / "all_leads.xlsx", columns=cols)
    for (city,) in hub.conn.execute("SELECT DISTINCT city FROM places WHERE city IS NOT NULL"):
        _write(_frame(hub, tz, "WHERE city=?", (city,), columns=cols),
               out / f"city_{safe_filename(city)}.xlsx", columns=cols)
    start = datetime.combine(day, time(0), tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    daily = _frame(hub, tz, "WHERE first_seen >= ? AND first_seen < ?",
                   (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")), columns=cols)
    msg = f"hub export: all_leads.xlsx {len(allp)} places"
    if not daily.empty:
        _write(daily, out / f"leads_{day.isoformat()}.xlsx", columns=cols)
        msg += f"; leads_{day.isoformat()}.xlsx {len(daily)} new"
    log.info(msg)
    hub.event("export", msg)
    return msg
