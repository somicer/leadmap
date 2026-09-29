"""Central hub: a queue of jobs (city × segment) shared by many scraper servers (workers).

A segment (صنف) is a name plus the keywords searched on every tile, e.g.
«میوه فروشی» → میوه فروشی، میوه، میوه فروش، تره بار. Each worker claims one job at a time,
reports progress and pushes its places in heartbeats, and reports the job finished before
claiming the next. The hub keeps a merged copy of all places (deduped per place and segment)
and exports it to Excel. Served by the dashboard process.
"""
import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import LEGACY_SEGMENT, now_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    name       TEXT PRIMARY KEY,
    queries    TEXT NOT NULL,                  -- JSON list of keywords
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    city        TEXT NOT NULL,
    segment     TEXT NOT NULL,
    position    REAL NOT NULL,                 -- queue order (lower first)
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued/active/done/error
    worker_id   INTEGER,
    assigned_at TEXT,
    finished_at TEXT,
    last_error  TEXT,
    tiles       TEXT,                          -- JSON {status: n} last reported by the worker
    added_at    TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (city, segment)
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
    place_id   TEXT NOT NULL,
    segment    TEXT NOT NULL DEFAULT '',
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
    worker_id  INTEGER,
    PRIMARY KEY (place_id, segment)
);
CREATE INDEX IF NOT EXISTS hub_places_job ON places(segment, city);
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

PLACE_FIELDS = ("place_id", "segment", "name", "phone_raw", "phone", "is_mobile", "address", "lat", "lng",
                "category", "rating", "maps_url", "city", "tile_id", "first_seen", "last_seen")
MAX_BATCH = 1000
MAX_NAME_LEN = 100
MAX_KEYWORDS = 20


class HubError(Exception):
    """A request the hub refuses; `code` is the HTTP status."""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _clean(name) -> str:
    return " ".join(str(name or "").split())


class HubDB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
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

    def _migrate(self):
        """Hub databases from before segments: `cities` → `jobs` of LEGACY_SEGMENT, places keyed per segment."""
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "cities" not in tables:
            return
        with self._tx():
            places_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(places)")}
            if "segment" not in places_cols:
                self.conn.execute("ALTER TABLE places RENAME TO places_old")
                self.conn.execute("DROP INDEX IF EXISTS hub_places_city")
                self.conn.execute("DROP INDEX IF EXISTS hub_places_first_seen")
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
            if "segment" not in places_cols:
                self.conn.execute(
                    "INSERT INTO places(place_id, segment, name, phone_raw, phone, is_mobile, address, lat, lng, "
                    "category, rating, maps_url, city, tile_id, first_seen, last_seen, worker_id) "
                    "SELECT place_id, ?, name, phone_raw, phone, is_mobile, address, lat, lng, category, rating, "
                    "maps_url, city, tile_id, first_seen, last_seen, worker_id FROM places_old", (LEGACY_SEGMENT,))
                self.conn.execute("DROP TABLE places_old")
            self.conn.execute(
                "INSERT OR IGNORE INTO segments(name, queries, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (LEGACY_SEGMENT, json.dumps(["املاک", "مشاور املاک"], ensure_ascii=False), now_iso(), now_iso()))
            self.conn.execute(
                "INSERT OR IGNORE INTO jobs(city, segment, position, status, worker_id, assigned_at, finished_at, "
                "last_error, tiles, added_at, updated_at) SELECT name, ?, position, status, worker_id, assigned_at, "
                "finished_at, last_error, tiles, added_at, updated_at FROM cities", (LEGACY_SEGMENT,))
            self.conn.execute("DROP TABLE cities")
        log.info("hub.db migrated to segments")

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

    # --- segments ------------------------------------------------------------
    def set_segment(self, name: str, keywords: list[str]):
        """Create a segment or replace its keywords (active workers pick them up on their next heartbeat)."""
        name = _clean(name)
        kws = []
        for k in keywords if isinstance(keywords, list) else []:
            k = _clean(k)
            if k and k not in kws and len(k) <= MAX_NAME_LEN:
                kws.append(k)
        if not name or len(name) > MAX_NAME_LEN:
            raise HubError(400, "segment name must be 1–100 characters")
        if not kws or len(kws) > MAX_KEYWORDS:
            raise HubError(400, f"a segment needs 1–{MAX_KEYWORDS} keywords")
        self.conn.execute(
            "INSERT INTO segments(name, queries, created_at, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET queries=excluded.queries, updated_at=excluded.updated_at",
            (name, json.dumps(kws, ensure_ascii=False), now_iso(), now_iso()))
        self.event("segment", f"{name}: {'، '.join(kws)}")
        return kws

    def delete_segment(self, name: str):
        with self._tx():
            busy = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE segment=? AND status IN ('queued','active')",
                                     (name,)).fetchone()[0]
            if busy:
                raise HubError(409, "this segment still has queued or active jobs")
            if not self.conn.execute("DELETE FROM segments WHERE name=?", (name,)).rowcount:
                raise HubError(404, "no such segment")
        self.event("segment", f"deleted {name}")

    def queries(self, segment: str) -> list[str] | None:
        row = self.conn.execute("SELECT queries FROM segments WHERE name=?", (segment,)).fetchone()
        return json.loads(row["queries"]) if row else None

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
                self.conn.execute("UPDATE jobs SET status='queued', worker_id=NULL, assigned_at=NULL, "
                                  "updated_at=? WHERE worker_id=? AND status='active'", (now_iso(), worker_id))
        else:
            raise HubError(400, f"unknown action {action!r}")
        self.event("worker", f"{action} {w['name']}")

    def _seen(self, w, ip: str, info: dict):
        self.conn.execute("UPDATE workers SET last_seen=?, ip=?, hostname=COALESCE(?, hostname) WHERE id=?",
                          (now_iso(), ip, _s(info.get("hostname"), 100), w["id"]))

    # --- jobs (city × segment) ---------------------------------------------------
    def add_jobs(self, cities: list[str], segment: str) -> list[str]:
        segment = _clean(segment)
        if self.queries(segment) is None:
            raise HubError(404, f"no segment {segment!r}; create it first")
        added = []
        with self._tx():
            pos = self.conn.execute("SELECT COALESCE(MAX(position), 0) FROM jobs").fetchone()[0]
            for c in cities:
                c = _clean(c)
                if not c or len(c) > MAX_NAME_LEN:
                    continue
                pos += 1
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO jobs(city, segment, position, added_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                    (c, segment, pos, now_iso(), now_iso()))
                if cur.rowcount:
                    added.append(c)
        if added:
            self.event("job", f"queued {segment}: " + "، ".join(added))
        return added

    def job_action(self, job_id: int, action: str):
        with self._tx():
            j = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if j is None:
                raise HubError(404, "no such job")
            if action in ("up", "down", "top"):
                self._move(j, action)
            elif action == "release":        # active → back to the queue (worker drops it)
                if j["status"] != "active":
                    raise HubError(409, "job is not active")
                self._set_job(job_id, status="queued", worker_id=None, assigned_at=None)
            elif action == "requeue":        # done/error → queue again (remaining/failed tiles)
                if j["status"] not in ("done", "error"):
                    raise HubError(409, "only finished or failed jobs can be re-queued")
                self._set_job(job_id, status="queued", finished_at=None, last_error=None)
            elif action == "delete":
                self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            else:
                raise HubError(400, f"unknown action {action!r}")
        self.event("job", f"{action} {j['city']} / {j['segment']}")

    def _move(self, j, action):
        rows = self.conn.execute("SELECT id, position FROM jobs WHERE status='queued' ORDER BY position").fetchall()
        if action == "top":
            first = rows[0]["position"] if rows else 0
            self._set_job(j["id"], position=first - 1)
            return
        idx = next((i for i, r in enumerate(rows) if r["id"] == j["id"]), None)
        if idx is None:
            return
        k = idx - 1 if action == "up" else idx + 1
        if 0 <= k < len(rows):
            other = rows[k]
            self._set_job(j["id"], position=other["position"])
            self._set_job(other["id"], position=j["position"])

    def _set_job(self, job_id, **fields):
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def _job_dict(self, row) -> dict | None:
        if row is None:
            return None
        return {"city": row["city"], "segment": row["segment"], "queries": self.queries(row["segment"]) or []}

    def assignment(self, worker_id: int):
        """The worker's active job as {city, segment, queries}, or None."""
        return self._job_dict(self.conn.execute(
            "SELECT * FROM jobs WHERE worker_id=? AND status='active' ORDER BY assigned_at LIMIT 1",
            (worker_id,)).fetchone())

    def _active_job(self, w, info):
        return self.conn.execute("SELECT * FROM jobs WHERE city=? AND segment=? AND worker_id=? AND status='active'",
                                 (info.get("city"), info.get("segment"), w["id"])).fetchone()

    # --- worker API ------------------------------------------------------------
    def claim(self, w, ip: str, info: dict) -> dict:
        """Current job, else a preferred queued job (one the worker already started), else the queue head."""
        with self._tx():
            self._seen(w, ip, info)
            if w["paused"]:
                return {"job": self.assignment(w["id"]), "paused": True}
            job = self.assignment(w["id"])
            if job is None:
                row = None
                for p in (info.get("prefer") or [])[:50]:
                    if isinstance(p, list) and len(p) == 2:
                        row = self.conn.execute(
                            "SELECT * FROM jobs WHERE city=? AND segment=? AND status='queued'", tuple(p)).fetchone()
                        if row:
                            break
                row = row or self.conn.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY position LIMIT 1").fetchone()
                if row:
                    self._set_job(row["id"], status="active", worker_id=w["id"], assigned_at=now_iso(),
                                  last_error=None)
                    self.event("assign", f"{row['city']} / {row['segment']} → {w['name']}")
                    job = self._job_dict(row)
        return {"job": job, "paused": False}

    def heartbeat(self, w, ip: str, info: dict) -> dict:
        places = info.get("places") or []
        if not isinstance(places, list) or len(places) > MAX_BATCH:
            raise HubError(400, f"places must be a list of at most {MAX_BATCH}")
        with self._tx():
            self._seen(w, ip, info)
            report = {k: info.get(k) for k in ("phase", "city", "segment", "tile", "backoff", "progress",
                                               "version", "tiles_last_hour", "sync_backlog")}
            self.conn.execute("UPDATE workers SET report=? WHERE id=?",
                              (json.dumps(report, ensure_ascii=False)[:5000], w["id"]))
            job = self._active_job(w, info)
            if job and isinstance(info.get("tiles"), dict):
                self._set_job(job["id"], tiles=json.dumps(_tile_counts(info["tiles"])))
            accepted = sum(self._upsert_place(p, w["id"]) for p in places)
            assignment = self.assignment(w["id"])
        return {"assignment": assignment, "paused": bool(w["paused"]), "accepted": accepted}

    def complete(self, w, ip: str, info: dict) -> dict:
        with self._tx():
            self._seen(w, ip, info)
            job = self._active_job(w, info)
            if job:
                tiles = _tile_counts(info.get("tiles") or {})
                self._set_job(job["id"], status="done", finished_at=now_iso(), tiles=json.dumps(tiles))
                self.event("done", f"{job['city']} / {job['segment']} by {w['name']}: {tiles}")
        return {"ok": True}

    def fail(self, w, ip: str, info: dict) -> dict:
        """The worker cannot scrape this job at all (e.g. OSM has no such place)."""
        err = _s(info.get("error"), 500) or "unknown error"
        with self._tx():
            self._seen(w, ip, info)
            job = self._active_job(w, info)
            if job:
                self._set_job(job["id"], status="error", last_error=err, finished_at=now_iso())
                self.event("error", f"{job['city']} / {job['segment']} by {w['name']}: {err}")
        return {"ok": True}

    def _upsert_place(self, p, worker_id: int) -> int:
        if not isinstance(p, dict) or not isinstance(p.get("place_id"), str) or not p["place_id"]:
            return 0
        rec = {k: p.get(k) for k in PLACE_FIELDS}
        for k in ("lat", "lng", "rating"):
            rec[k] = _f(rec[k])
        for k in ("place_id", "segment", "name", "phone_raw", "phone", "address", "category", "maps_url",
                  "city", "tile_id", "first_seen", "last_seen"):
            rec[k] = _s(rec[k], 1000)
        rec["segment"] = rec["segment"] or ""
        rec["is_mobile"] = int(bool(rec["is_mobile"]))
        rec["first_seen"] = rec["first_seen"] or now_iso()
        rec["last_seen"] = rec["last_seen"] or rec["first_seen"]
        rec["worker_id"] = worker_id
        self.conn.execute(
            "INSERT INTO places(place_id, segment, name, phone_raw, phone, is_mobile, address, lat, lng, category, "
            "rating, maps_url, city, tile_id, first_seen, last_seen, worker_id) VALUES "
            "(:place_id, :segment, :name, :phone_raw, :phone, :is_mobile, :address, :lat, :lng, :category, "
            ":rating, :maps_url, :city, :tile_id, :first_seen, :last_seen, :worker_id) "
            "ON CONFLICT(place_id, segment) DO UPDATE SET name=COALESCE(excluded.name, name), "
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
        stats = {(r["city"], r["segment"]): dict(r) for r in self.conn.execute(
            "SELECT city, segment, COUNT(*) places, SUM(phone IS NOT NULL) phones, SUM(is_mobile) mobiles "
            "FROM places GROUP BY city, segment")}
        names = {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM workers")}
        jobs = []
        for r in self.conn.execute("SELECT * FROM jobs ORDER BY status='done', status='error', position"):
            s = stats.get((r["city"], r["segment"]), {})
            jobs.append({
                "id": r["id"], "city": r["city"], "segment": r["segment"], "status": r["status"],
                "worker": names.get(r["worker_id"]), "assigned_at": r["assigned_at"],
                "finished_at": r["finished_at"], "last_error": r["last_error"],
                "tiles": json.loads(r["tiles"] or "{}"), "places": s.get("places") or 0,
                "phones": s.get("phones") or 0, "mobiles": s.get("mobiles") or 0, "updated_at": r["updated_at"],
            })
        seg_stats = {r["segment"]: dict(r) for r in self.conn.execute(
            "SELECT segment, COUNT(*) places, SUM(is_mobile) mobiles FROM places GROUP BY segment")}
        segments = [{"name": r["name"], "queries": json.loads(r["queries"]),
                     "places": (seg_stats.get(r["name"]) or {}).get("places") or 0,
                     "mobiles": (seg_stats.get(r["name"]) or {}).get("mobiles") or 0}
                    for r in self.conn.execute("SELECT * FROM segments ORDER BY created_at")]
        workers = []
        for r in self.conn.execute("SELECT * FROM workers WHERE revoked=0 ORDER BY id"):
            seen = r["last_seen"]
            age = (now - datetime.fromisoformat(seen)).total_seconds() if seen else None
            placed = self.conn.execute("SELECT COUNT(*) FROM places WHERE worker_id=?", (r["id"],)).fetchone()[0]
            workers.append({
                "id": r["id"], "name": r["name"], "hostname": r["hostname"], "ip": r["ip"],
                "last_seen": seen, "age_s": age, "online": age is not None and age < stale_after_s,
                "paused": bool(r["paused"]), "job": self.assignment(r["id"]),
                "report": json.loads(r["report"] or "{}"), "places": placed,
            })
        t = self.conn.execute("SELECT COUNT(*) n, SUM(phone IS NOT NULL) p, SUM(is_mobile) m FROM places").fetchone()
        events = [dict(r) for r in self.conn.execute(
            "SELECT timestamp, type, detail FROM events ORDER BY id DESC LIMIT 15")]
        return {"segments": segments, "jobs": jobs, "workers": workers, "events": events,
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
    """exports/hub/: all_leads.xlsx, one file per segment and city, and leads_<day>.xlsx (first seen that day)."""
    from .export import COLUMNS, _frame, _write
    cols = ["segment", "city"] + COLUMNS
    tz = ZoneInfo(cfg["pacing"]["timezone"])
    day = day or datetime.now(tz).date()
    out = cfg.path("exports") / "hub"
    out.mkdir(parents=True, exist_ok=True)

    allp = _frame(hub, tz, columns=cols)
    _write(allp, out / "all_leads.xlsx", columns=cols)
    for seg, city in hub.conn.execute("SELECT DISTINCT segment, city FROM places WHERE city IS NOT NULL"):
        _write(_frame(hub, tz, "WHERE segment=? AND city=?", (seg, city), columns=cols),
               out / f"{safe_filename(seg)}__{safe_filename(city)}.xlsx", columns=cols)
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
