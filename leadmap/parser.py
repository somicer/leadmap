"""Parsing of Google Maps search data.

Structure verified against real responses (see debug/, 2026-09):
  /search?tbm=map body = ")]}'\\n" + JSON   (paginated ones are wrapped as {"c":0,"d":"<same>"}/*""*/)
  results  = data[64] -> list of [meta, place]
  place[10]  hex feature id "0x..:0x.."        place[78]  ChIJ place id
  place[11]  name                              place[9]   [_, _, lat, lng]
  place[13]  categories (list)                 place[4]   [..7 nulls.., rating]
  place[39]  full address (localized)          place[178] [[intl phone, [[local, 1], ...], _, e164, ...]]
"""
import json
import re
import urllib.parse

from .phone import is_mobile, normalize_phone

_BIDI = re.compile(r"[‎‏‪-‮⁦-⁩]")


class ParseError(Exception):
    pass


def _get(x, *path):
    for k in path:
        try:
            x = x[k]
        except (IndexError, TypeError, KeyError):
            return None
    return x


def clean(s):
    if not isinstance(s, str):
        return None
    s = re.sub(r"\s+", " ", _BIDI.sub("", s)).strip()
    return s or None


def decode_body(text: str):
    """Decode a /search?tbm=map body (plain or {"c","d"}-wrapped) into JSON."""
    text = text.lstrip()
    if text.startswith("{"):
        text = json.JSONDecoder().raw_decode(text)[0]["d"]
    if text.startswith(")]}'"):
        text = text[text.index("\n") + 1:]
    return json.JSONDecoder().raw_decode(text)[0]


def maps_url(place_id: str) -> str:
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


def make_place(place_id, name, phone_raw, address, lat, lng, category, rating, url=None) -> dict:
    phone_raw = clean(phone_raw)
    phone = normalize_phone(phone_raw)
    return {
        "place_id": place_id, "name": clean(name), "phone_raw": phone_raw, "phone": phone,
        "is_mobile": is_mobile(phone), "address": clean(address), "lat": lat, "lng": lng,
        "category": clean(category), "rating": rating, "maps_url": url or maps_url(place_id),
    }


def parse_place(p) -> dict | None:
    if not isinstance(p, list):
        return None
    pid = _get(p, 78) or _get(p, 227, 0, 4) or _get(p, 10)
    name = _get(p, 11)
    if not pid or not name:
        return None
    phone_raw = _get(p, 178, 0, 1, 0, 0) or _get(p, 178, 0, 0)
    rating = _get(p, 4, 7)
    cats = _get(p, 13) or []
    place = make_place(
        pid, name, phone_raw, _get(p, 39) or _get(p, 18),
        _get(p, 9, 2), _get(p, 9, 3),
        "، ".join(c for c in cats if isinstance(c, str)) or None,
        float(rating) if isinstance(rating, (int, float)) else None,
    )
    place["hex_id"] = _get(p, 10)  # used to dedupe against DOM cards; not stored
    return place


def parse_search_body(text: str) -> list[dict]:
    try:
        data = decode_body(text)
    except (ValueError, KeyError, TypeError) as e:
        raise ParseError(f"undecodable search body: {e}") from e
    items = _get(data, 64)
    if items is None:
        return []  # valid response without a results block (e.g. no results)
    if not isinstance(items, list):
        raise ParseError("data[64] is not a list")
    places = [pl for it in items if (pl := parse_place(_get(it, 1)))]
    if items and not places:
        raise ParseError(f"{len(items)} result items but none parsed — structure changed?")
    return places


_HREF_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_PID_RE = re.compile(r"!19s(ChIJ[\w-]+)")
_HEX_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
_PHONE_IN_TEXT = re.compile(r"(?:\+98|0)[\d۰-۹ \-]{8,14}\d|[۰0][۹9][\d۰-۹ ]{9,11}")


def parse_dom_card(aria: str, href: str, text: str) -> dict | None:
    """Fallback: one result card from the feed (anchor aria-label/href + card text)."""
    if not href:
        return None
    href = urllib.parse.unquote(href)
    m_pid = _PID_RE.search(href) or _HEX_RE.search(href)
    m_ll = _HREF_RE.search(href)
    if not m_pid or not aria:
        return None
    lat, lng = (float(m_ll.group(1)), float(m_ll.group(2))) if m_ll else (None, None)
    m_ph = _PHONE_IN_TEXT.search(_BIDI.sub("", text or ""))
    rating = None
    m_r = re.search(r"^\s*([0-5][.,٫]\d|[0-5])\s*$", text or "", re.M)
    if m_r:
        rating = float(m_r.group(1).replace(",", ".").replace("٫", "."))
    return make_place(m_pid.group(1), aria, m_ph.group(0) if m_ph else None, None,
                      lat, lng, None, rating)
