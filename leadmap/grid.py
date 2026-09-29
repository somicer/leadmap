"""City boundary (OSM Nominatim) → snake-ordered square tiles."""
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_last_request = 0.0


def fetch_boundary(city: str, cfg) -> dict:
    """Return the Nominatim result for `city` (cached on disk forever)."""
    cache_dir: Path = cfg.path("cache")
    key = hashlib.sha1(f"{city}|{cfg['country_code']}".encode()).hexdigest()[:16]
    cache_file = cache_dir / f"nominatim_{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    global _last_request
    wait = cfg["nominatim"]["min_interval_s"] - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    params = urllib.parse.urlencode({
        "q": city, "format": "jsonv2", "polygon_geojson": 1, "limit": 5,
        "countrycodes": cfg["country_code"], "accept-language": "fa",
    })
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}",
                                 headers={"User-Agent": cfg["nominatim"]["user_agent"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        results = json.loads(r.read().decode("utf-8"))
    _last_request = time.monotonic()
    if not results:
        raise RuntimeError(f"Nominatim returned nothing for {city!r}")
    result = pick_result(results)
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def pick_result(results: list[dict]) -> dict:
    """Prefer an areal city/town boundary over points (e.g. a county or a POI)."""
    def score(r):
        geom = (r.get("geojson") or {}).get("type", "")
        areal = geom in ("Polygon", "MultiPolygon")
        kind = r.get("addresstype") or r.get("type") or ""
        return (areal, kind in ("city", "town"), r.get("importance") or 0)
    return max(results, key=score)


def boundary_polygon(result: dict):
    """Shapely geometry (lng/lat) of the result; bbox fallback with a warning."""
    gj = result.get("geojson") or {}
    if gj.get("type") in ("Polygon", "MultiPolygon"):
        return shape(gj), False
    s, n, w, e = map(float, result["boundingbox"])
    log.warning("No polygon for %s — falling back to bounding box", result.get("display_name"))
    return box(w, s, e, n), True


def utm_epsg(lng: float, lat: float) -> int:
    zone = int((lng + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def build_tiles(poly, city: str, tile_size_m: float, zoom: int) -> list[dict]:
    """Square tiles intersecting `poly` (lng/lat), in snake order."""
    c = poly.centroid
    epsg = utm_epsg(c.x, c.y)
    fwd = Transformer.from_crs(4326, epsg, always_xy=True).transform
    inv = Transformer.from_crs(epsg, 4326, always_xy=True).transform
    p = transform(fwd, poly)
    minx, miny, maxx, maxy = p.bounds
    n_rows = int((maxy - miny) // tile_size_m) + 1
    n_cols = int((maxx - minx) // tile_size_m) + 1

    tiles = []
    for row in range(n_rows):
        cols = range(n_cols) if row % 2 == 0 else range(n_cols - 1, -1, -1)
        for col in cols:
            x0 = minx + col * tile_size_m
            y0 = maxy - (row + 1) * tile_size_m  # row 0 = northernmost
            cell = box(x0, y0, x0 + tile_size_m, y0 + tile_size_m)
            if not cell.intersects(p):
                continue
            geo = transform(inv, cell)
            w, s, e, n = geo.bounds
            cx, cy = inv(x0 + tile_size_m / 2, y0 + tile_size_m / 2)
            tiles.append({
                "id": f"{city}:{row}:{col}", "city": city, "seq": len(tiles),
                "row": row, "col": col, "lat": round(cy, 6), "lng": round(cx, 6), "zoom": zoom,
                "min_lat": s, "min_lng": w, "max_lat": n, "max_lng": e,
            })
    return tiles


def padded_contains(tile, lat: float, lng: float, pad_ratio: float) -> bool:
    dlat = (tile["max_lat"] - tile["min_lat"]) * pad_ratio
    dlng = (tile["max_lng"] - tile["min_lng"]) * pad_ratio
    return (tile["min_lat"] - dlat <= lat <= tile["max_lat"] + dlat
            and tile["min_lng"] - dlng <= lng <= tile["max_lng"] + dlng)


def load_city_polygon(city: str, cfg):
    """Polygon from the Nominatim cache (None if init was never run)."""
    try:
        return boundary_polygon(fetch_boundary(city, cfg))[0]
    except Exception:
        return None


def init_city(cfg, db, city: str) -> dict:
    """Fetch the boundary and insert the city's tiles (idempotent). Used by `init` and hub workers."""
    result = fetch_boundary(city, cfg)
    poly, fallback = boundary_polygon(result)
    tiles = build_tiles(poly, city, cfg["grid"]["tile_size_m"], cfg["grid"]["zoom"])
    added = db.insert_tiles(tiles)
    db.event("init", f"{city}: {len(tiles)} tiles, {added} new, bbox_fallback={fallback}")
    return {"result": result, "poly": poly, "fallback": fallback, "tiles": len(tiles), "added": added}
