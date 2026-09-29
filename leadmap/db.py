"""SQLite storage (WAL mode)."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LEGACY_SEGMENT = "املاک"   # data collected before segments existed was all real estate

PLACES_DDL = """
CREATE TABLE IF NOT EXISTS places (
    place_id   TEXT NOT NULL,
    segment    TEXT NOT NULL DEFAULT '',     -- the same shop can be a lead in several segments
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
    details_checked_at TEXT,         -- detail page opened (only for places without list phone)
    PRIMARY KEY (place_id, segment)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    id            TEXT PRIMARY KEY,          -- "<segment>/<city>:<row>:<col>"
    city          TEXT NOT NULL,
    segment       TEXT NOT NULL DEFAULT '',  -- صنف: which keyword set this tile is searched with
    seq           INTEGER NOT NULL,          -- snake order
    row           INTEGER NOT NULL,
    col           INTEGER NOT NULL,
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    zoom          INTEGER NOT NULL,
    min_lat REAL, min_lng REAL, max_lat REAL, max_lng REAL,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending/done/failed/empty
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    results_count INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);

""" + PLACES_DDL + """
CREATE INDEX IF NOT EXISTS places_first_seen ON places(first_seen);

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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.execute("CREATE INDEX IF NOT EXISTS tiles_job_status ON tiles(city, segment, status, seq)")
        self.conn.execute("DROP INDEX IF EXISTS tiles_city_status")

    def _migrate(self):
        """Bring a pre-segment database up to date (its data becomes segment LEGACY_SEGMENT)."""
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "places" in tables:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(places)")}
            if "details_checked_at" not in cols:
                self.conn.execute("ALTER TABLE places ADD COLUMN details_checked_at TEXT")
            if "segment" not in cols:  # new primary key (place_id, segment): rebuild the table
                with self._tx():
                    self.conn.execute("ALTER TABLE places RENAME TO places_old")
                    self.conn.execute("DROP INDEX IF EXISTS places_first_seen")
                    self.conn.execute(PLACES_DDL)
                    self.conn.execute(
                        "INSERT INTO places(place_id, segment, name, phone_raw, phone, is_mobile, address, lat, "
                        "lng, category, rating, maps_url, city, tile_id, first_seen, last_seen, "
                        "details_checked_at) SELECT place_id, ?, name, phone_raw, phone, is_mobile, address, "
                        "lat, lng, category, rating, maps_url, city, ? || '/' || tile_id, first_seen, "
                        "last_seen, details_checked_at FROM places_old", (LEGACY_SEGMENT, LEGACY_SEGMENT))
                    self.conn.execute("DROP TABLE places_old")
        if "tiles" in tables:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tiles)")}
            if "segment" not in cols:
                with self._tx():
                    self.conn.execute("ALTER TABLE tiles ADD COLUMN segment TEXT NOT NULL DEFAULT ''")
                    self.conn.execute("UPDATE tiles SET segment=?, id=? || '/' || id",
                                      (LEGACY_SEGMENT, LEGACY_SEGMENT))

    def close(self):
        self.conn.close()

    def _tx(self):
        conn = self.conn

        class _T:
            def __enter__(self):
                conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc, *_):
                conn.execute("ROLLBACK" if exc else "COMMIT")
        return _T()

    # --- state -------------------------------------------------------------
    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key, value):
        self.conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # --- events ------------------------------------------------------------
    def event(self, type_: str, detail: str = ""):
        self.conn.execute(
            "INSERT INTO events(timestamp, type, detail) VALUES(?, ?, ?)",
            (now_iso(), type_, detail[:4000]),
        )

    # --- tiles -------------------------------------------------------------
    def insert_tiles(self, tiles: list[dict]) -> int:
        before = self.conn.total_changes
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO tiles(id, city, segment, seq, row, col, lat, lng, zoom, "
                "min_lat, min_lng, max_lat, max_lng, updated_at) VALUES "
                "(:id, :city, :segment, :seq, :row, :col, :lat, :lng, :zoom, "
                ":min_lat, :min_lng, :max_lat, :max_lng, :updated_at)",
                [{**t, "updated_at": now_iso()} for t in tiles],
            )
        return self.conn.total_changes - before

    def next_tile(self, city: str, segment: str):
        return self.conn.execute(
            "SELECT * FROM tiles WHERE city=? AND segment=? AND status='pending' ORDER BY seq LIMIT 1",
            (city, segment),
        ).fetchone()

    def get_tile(self, tile_id: str):
        return self.conn.execute("SELECT * FROM tiles WHERE id=?", (tile_id,)).fetchone()

    def update_tile(self, tile_id: str, **fields):
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE tiles SET {cols} WHERE id=?", (*fields.values(), tile_id))

    def neighbour_had_results(self, tile) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM tiles WHERE city=? AND segment=? AND results_count>0 "
            "AND ABS(row-?)<=1 AND ABS(col-?)<=1 AND id<>? LIMIT 1",
            (tile["city"], tile["segment"], tile["row"], tile["col"], tile["id"]),
        ).fetchone()
        return row is not None

    def reset_failed(self, city: str | None = None, segment: str | None = None) -> int:
        where, args = _job_filter(city, segment)
        q = "UPDATE tiles SET status='pending', attempts=0, last_error=NULL, updated_at=? WHERE status='failed'"
        q += where.replace(" WHERE ", " AND ", 1)
        return self.conn.execute(q, [now_iso(), *args]).rowcount

    def tile_counts(self, city: str | None = None, segment: str | None = None) -> dict:
        where, args = _job_filter(city, segment)
        q = f"SELECT status, COUNT(*) n FROM tiles{where} GROUP BY status"
        return {r["status"]: r["n"] for r in self.conn.execute(q, args)}

    def jobs(self) -> list[tuple[str, str]]:
        """(city, segment) pairs that have tiles, most recently worked first."""
        return [(r["city"], r["segment"]) for r in self.conn.execute(
            "SELECT city, segment, MAX(updated_at) u FROM tiles GROUP BY city, segment ORDER BY u DESC")]

    # --- places ------------------------------------------------------------
    def upsert_place(self, p: dict) -> bool:
        """Insert or update a place (per segment). Returns True if it was new."""
        ts = now_iso()
        p = {**p, "segment": p.get("segment") or ""}
        existing = self.conn.execute(
            "SELECT phone FROM places WHERE place_id=? AND segment=?", (p["place_id"], p["segment"])
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO places(place_id, segment, name, phone_raw, phone, is_mobile, address, lat, lng, "
                "category, rating, maps_url, city, tile_id, first_seen, last_seen) VALUES "
                "(:place_id, :segment, :name, :phone_raw, :phone, :is_mobile, :address, :lat, :lng, "
                ":category, :rating, :maps_url, :city, :tile_id, :ts, :ts)",
                {**_place_defaults(), **p, "is_mobile": int(bool(p.get("is_mobile"))), "ts": ts},
            )
            return True
        # Keep existing non-null values when the new record lacks them.
        self.conn.execute(
            "UPDATE places SET name=COALESCE(:name, name), phone_raw=COALESCE(:phone_raw, phone_raw), "
            "phone=COALESCE(:phone, phone), "
            "is_mobile=CASE WHEN :phone IS NULL THEN is_mobile ELSE :is_mobile END, "
            "address=COALESCE(:address, address), lat=COALESCE(:lat, lat), lng=COALESCE(:lng, lng), "
            "category=COALESCE(:category, category), rating=COALESCE(:rating, rating), "
            "maps_url=COALESCE(:maps_url, maps_url), last_seen=:ts WHERE place_id=:place_id AND segment=:segment",
            {**_place_defaults(), **p, "is_mobile": int(bool(p.get("is_mobile"))), "ts": ts},
        )
        return False

    def needs_details(self, place_id: str) -> bool:
        """No phone known and its detail page never opened (in any segment)."""
        row = self.conn.execute(
            "SELECT COUNT(*) n, MAX(phone) phone, MAX(details_checked_at) checked FROM places WHERE place_id=?",
            (place_id,)).fetchone()
        return row["n"] > 0 and row["phone"] is None and row["checked"] is None

    def known_phone(self, place_id: str):
        """A phone found for this place in another segment (saves a detail-page visit)."""
        row = self.conn.execute("SELECT phone_raw, phone, is_mobile FROM places WHERE place_id=? "
                                "AND phone IS NOT NULL LIMIT 1", (place_id,)).fetchone()
        return dict(row) if row else None

    def mark_details_checked(self, place_id: str):
        self.conn.execute("UPDATE places SET details_checked_at=? WHERE place_id=?",
                          (now_iso(), place_id))

    def place_counts(self, city: str | None = None, segment: str | None = None) -> dict:
        where, args = _job_filter(city, segment)
        q = f"SELECT COUNT(*) total, SUM(is_mobile) mobiles, SUM(phone IS NOT NULL) phones FROM places{where}"
        r = self.conn.execute(q, args).fetchone()
        return {"total": r["total"] or 0, "mobiles": r["mobiles"] or 0, "phones": r["phones"] or 0}


def _job_filter(city, segment) -> tuple[str, list]:
    conds, args = [], []
    if city:
        conds.append("city=?")
        args.append(city)
    if segment is not None:
        conds.append("segment=?")
        args.append(segment)
    return (" WHERE " + " AND ".join(conds) if conds else ""), args


def _place_defaults():
    return {k: None for k in ("name", "phone_raw", "phone", "address", "lat", "lng",
                              "category", "rating", "maps_url", "city", "tile_id")}
