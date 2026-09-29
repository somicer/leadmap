"""SQLite storage (WAL mode)."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    id            TEXT PRIMARY KEY,          -- "<city>:<row>:<col>"
    city          TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS tiles_city_status ON tiles(city, status, seq);

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
    details_checked_at TEXT          -- detail page opened (only for places without list phone)
);
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
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(places)")}
        if "details_checked_at" not in cols:
            self.conn.execute("ALTER TABLE places ADD COLUMN details_checked_at TEXT")

    def close(self):
        self.conn.close()

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
                "INSERT OR IGNORE INTO tiles(id, city, seq, row, col, lat, lng, zoom, "
                "min_lat, min_lng, max_lat, max_lng, updated_at) VALUES "
                "(:id, :city, :seq, :row, :col, :lat, :lng, :zoom, "
                ":min_lat, :min_lng, :max_lat, :max_lng, :updated_at)",
                [{**t, "updated_at": now_iso()} for t in tiles],
            )
        return self.conn.total_changes - before

    def next_tile(self, city: str):
        return self.conn.execute(
            "SELECT * FROM tiles WHERE city=? AND status='pending' ORDER BY seq LIMIT 1",
            (city,),
        ).fetchone()

    def get_tile(self, tile_id: str):
        return self.conn.execute("SELECT * FROM tiles WHERE id=?", (tile_id,)).fetchone()

    def update_tile(self, tile_id: str, **fields):
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE tiles SET {cols} WHERE id=?", (*fields.values(), tile_id))

    def neighbour_had_results(self, tile) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM tiles WHERE city=? AND results_count>0 "
            "AND ABS(row-?)<=1 AND ABS(col-?)<=1 AND id<>? LIMIT 1",
            (tile["city"], tile["row"], tile["col"], tile["id"]),
        ).fetchone()
        return row is not None

    def reset_failed(self, city: str | None = None) -> int:
        q = "UPDATE tiles SET status='pending', attempts=0, last_error=NULL, updated_at=? WHERE status='failed'"
        args = [now_iso()]
        if city:
            q += " AND city=?"
            args.append(city)
        return self.conn.execute(q, args).rowcount

    def tile_counts(self, city: str | None = None) -> dict:
        q = "SELECT status, COUNT(*) n FROM tiles"
        args = []
        if city:
            q += " WHERE city=?"
            args.append(city)
        q += " GROUP BY status"
        return {r["status"]: r["n"] for r in self.conn.execute(q, args)}

    # --- places ------------------------------------------------------------
    def upsert_place(self, p: dict) -> bool:
        """Insert or update a place. Returns True if it was new."""
        ts = now_iso()
        existing = self.conn.execute(
            "SELECT phone FROM places WHERE place_id=?", (p["place_id"],)
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO places(place_id, name, phone_raw, phone, is_mobile, address, lat, lng, "
                "category, rating, maps_url, city, tile_id, first_seen, last_seen) VALUES "
                "(:place_id, :name, :phone_raw, :phone, :is_mobile, :address, :lat, :lng, "
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
            "maps_url=COALESCE(:maps_url, maps_url), last_seen=:ts WHERE place_id=:place_id",
            {**_place_defaults(), **p, "is_mobile": int(bool(p.get("is_mobile"))), "ts": ts},
        )
        return False

    def needs_details(self, place_id: str) -> bool:
        row = self.conn.execute(
            "SELECT phone, details_checked_at FROM places WHERE place_id=?", (place_id,)
        ).fetchone()
        return row is not None and row["phone"] is None and row["details_checked_at"] is None

    def mark_details_checked(self, place_id: str):
        self.conn.execute("UPDATE places SET details_checked_at=? WHERE place_id=?",
                          (now_iso(), place_id))

    def place_counts(self, city: str | None = None) -> dict:
        q = "SELECT COUNT(*) total, SUM(is_mobile) mobiles, SUM(phone IS NOT NULL) phones FROM places"
        args = []
        if city:
            q += " WHERE city=?"
            args.append(city)
        r = self.conn.execute(q, args).fetchone()
        return {"total": r["total"] or 0, "mobiles": r["mobiles"] or 0, "phones": r["phones"] or 0}


def _place_defaults():
    return {k: None for k in ("name", "phone_raw", "phone", "address", "lat", "lng",
                              "category", "rating", "maps_url", "city", "tile_id")}
