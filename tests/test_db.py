import sqlite3

from leadmap.db import DB, LEGACY_SEGMENT


def test_migrates_pre_segment_db(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE tiles (id TEXT PRIMARY KEY, city TEXT NOT NULL, seq INTEGER NOT NULL, row INTEGER NOT NULL,
            col INTEGER NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, zoom INTEGER NOT NULL,
            min_lat REAL, min_lng REAL, max_lat REAL, max_lng REAL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, results_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT);
        CREATE INDEX tiles_city_status ON tiles(city, status, seq);
        CREATE TABLE places (place_id TEXT PRIMARY KEY, name TEXT, phone_raw TEXT, phone TEXT,
            is_mobile INTEGER NOT NULL DEFAULT 0, address TEXT, lat REAL, lng REAL, category TEXT, rating REAL,
            maps_url TEXT, city TEXT, tile_id TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            details_checked_at TEXT);
        CREATE INDEX places_first_seen ON places(first_seen);
        INSERT INTO tiles(id, city, seq, row, col, lat, lng, zoom, status) VALUES ('کرج:0:1', 'کرج', 0, 0, 1, 1, 1, 16, 'done');
        INSERT INTO tiles(id, city, seq, row, col, lat, lng, zoom) VALUES ('کرج:0:2', 'کرج', 1, 0, 2, 1, 1, 16);
        INSERT INTO places(place_id, name, phone, city, tile_id, first_seen, last_seen)
            VALUES ('p1', 'x', '09121112233', 'کرج', 'کرج:0:1', 't', 't');
    """)
    c.close()
    db = DB(path)
    assert db.tile_counts("کرج", LEGACY_SEGMENT) == {"done": 1, "pending": 1}
    assert db.next_tile("کرج", LEGACY_SEGMENT)["id"] == f"{LEGACY_SEGMENT}/کرج:0:2"
    p = db.conn.execute("SELECT * FROM places").fetchone()
    assert (p["segment"], p["tile_id"], p["phone"]) == (LEGACY_SEGMENT, f"{LEGACY_SEGMENT}/کرج:0:1", "09121112233")
    assert db.jobs() == [("کرج", LEGACY_SEGMENT)]
    db.close()
    DB(path).close()  # idempotent


def test_places_are_per_segment(tmp_path):
    db = DB(tmp_path / "t.db")
    base = {"place_id": "p1", "name": "میوه‌فروشی نمونه", "city": "قم"}
    assert db.upsert_place({**base, "segment": "میوه فروشی", "phone": "09121112233", "is_mobile": True})
    assert db.upsert_place({**base, "segment": "سوپرمارکت"})          # same shop, other segment: new lead
    assert not db.upsert_place({**base, "segment": "سوپرمارکت"})
    assert db.place_counts("قم", "سوپرمارکت")["total"] == 1
    assert db.known_phone("p1")["phone"] == "09121112233"            # no detail page needed for the 2nd one
    assert not db.needs_details("p1")
