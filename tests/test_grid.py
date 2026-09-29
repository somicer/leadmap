from shapely.geometry import Polygon, box

from leadmap.grid import boundary_polygon, build_tiles, padded_contains, pick_result


def square(lng, lat, d):
    return box(lng, lat, lng + d, lat + d)


def test_tiles_cover_polygon_and_are_unique():
    poly = square(50.95, 35.80, 0.05)  # ~4.5 x 5.5 km near Karaj
    tiles = build_tiles(poly, "t", 800, 16)
    ids = [t["id"] for t in tiles]
    assert len(ids) == len(set(ids))
    assert [t["seq"] for t in tiles] == list(range(len(tiles)))
    assert 30 <= len(tiles) <= 60
    union = None
    for t in tiles:
        b = box(t["min_lng"], t["min_lat"], t["max_lng"], t["max_lat"])
        union = b if union is None else union.union(b)
    assert union.buffer(1e-9).covers(poly)


def test_tiles_only_intersecting():
    # L-shaped polygon: the empty top-right corner must produce no tiles.
    poly = Polygon([(51.0, 35.8), (51.05, 35.8), (51.05, 35.82), (51.02, 35.82),
                    (51.02, 35.85), (51.0, 35.85)])
    tiles = build_tiles(poly, "t", 800, 16)
    for t in tiles:
        assert box(t["min_lng"], t["min_lat"], t["max_lng"], t["max_lat"]).intersects(poly)
    assert not any(t["lat"] > 35.83 and t["lng"] > 51.035 for t in tiles)


def test_snake_order():
    tiles = build_tiles(square(50.95, 35.80, 0.03), "t", 800, 16)
    rows = {}
    for t in tiles:
        rows.setdefault(t["row"], []).append(t["col"])
    for r, cols in rows.items():
        assert cols == (sorted(cols) if r % 2 == 0 else sorted(cols, reverse=True))
    # consecutive tiles are neighbours
    for a, b in zip(tiles, tiles[1:]):
        assert abs(a["row"] - b["row"]) + abs(a["col"] - b["col"]) <= 2


def test_tile_size_roughly_800m():
    t = build_tiles(square(50.95, 35.80, 0.03), "t", 800, 16)[0]
    dlat_m = (t["max_lat"] - t["min_lat"]) * 111_000
    assert 750 < dlat_m < 850


def test_deterministic():
    poly = square(50.95, 35.80, 0.03)
    assert build_tiles(poly, "t", 800, 16) == build_tiles(poly, "t", 800, 16)


def test_bbox_fallback():
    geom, fallback = boundary_polygon({"boundingbox": ["35.7", "35.9", "50.8", "51.1"],
                                       "geojson": {"type": "Point", "coordinates": [51, 35.8]}})
    assert fallback and geom.bounds == (50.8, 35.7, 51.1, 35.9)


def test_pick_result_prefers_polygon():
    pt = {"geojson": {"type": "Point"}, "importance": 0.9, "addresstype": "city"}
    pg = {"geojson": {"type": "Polygon"}, "importance": 0.5, "addresstype": "city"}
    assert pick_result([pt, pg]) is pg


def test_padded_contains():
    t = {"min_lat": 35.0, "max_lat": 35.01, "min_lng": 51.0, "max_lng": 51.01}
    assert padded_contains(t, 35.005, 51.005, 0.25)
    assert padded_contains(t, 35.012, 51.005, 0.25)
    assert not padded_contains(t, 35.02, 51.005, 0.25)


def test_insert_tiles_idempotent(tmp_path):
    from leadmap.db import DB
    db = DB(tmp_path / "x.db")
    tiles = build_tiles(square(50.95, 35.80, 0.03), "t", 800, 16)
    assert db.insert_tiles(tiles) == len(tiles)
    assert db.insert_tiles(tiles) == 0
    assert sum(db.tile_counts("t").values()) == len(tiles)
