"""Scraping one tile: search each query, scroll the feed, collect places, open detail pages."""
import logging
import random
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from shapely.geometry import Point
from shapely.prepared import prep

from .grid import padded_contains
from .pacing import Stopped
from .parser import ParseError, clean, make_place, parse_dom_card, parse_search_body
from .phone import is_mobile, normalize_phone

log = logging.getLogger(__name__)

BLOCK_TEXTS = ("unusual traffic", "ترافیک غیرعادی", "ترافیک غیرمعمول", "not a robot", "ربات نیستید")
END_TEXTS = ("به انتهای فهرست رسیده‌اید", "reached the end of the list")

# Collect result cards from the feed (fallback path).
DOM_CARDS_JS = """
() => Array.from(document.querySelectorAll('div[role="feed"] a[href*="/maps/place/"]')).map(a => {
  let card = a.parentElement;
  return {aria: a.getAttribute('aria-label') || '', href: a.href, text: card ? card.innerText : ''};
})
"""


class BlockDetected(Exception):
    pass


class TransientError(Exception):
    pass


class TileScraper:
    def __init__(self, cfg, db, browser, pacer, city_poly=None, dump_dir: Path | None = None,
                 queries: list[str] | None = None):
        self.cfg = cfg
        self.queries = queries  # the segment's keywords
        self.db = db
        self.browser = browser
        self.pacer = pacer
        self.city_poly = prep(city_poly) if city_poly is not None else None
        self.dump_dir = dump_dir
        self._dump_n = 0
        self._saw_429 = False
        self._detail_cap_logged = False

    # --- public --------------------------------------------------------------
    def scrape_tile(self, tile) -> dict:
        s = self.cfg["search"]
        stats = {"raw": 0, "kept": 0, "new": 0, "phones": 0, "mobiles": 0, "details": 0,
                 "source": set(), "places": {}}
        queries = self.queries or s.get("queries") or next(iter(s["segments"].values()))
        for i, q in enumerate(queries):
            if i:
                self.pacer.query_pause()
            places, source = self.scrape_query(tile, q)
            stats["raw"] += len(places)
            stats["source"].add(source)
            for p in places:
                if not self._in_area(tile, p):
                    continue
                p.update(city=tile["city"], segment=tile["segment"], tile_id=tile["id"])
                is_new = self.db.upsert_place(p)
                if p["place_id"] not in stats["places"]:
                    stats["new"] += is_new
                stats["places"][p["place_id"]] = p
        if s.get("open_details_for_missing_phone"):
            stats["details"] = self.fill_missing_phones(list(stats["places"].values()))
        ps = stats["places"].values()
        stats["kept"] = len(stats["places"])
        stats["phones"] = sum(1 for p in ps if p.get("phone"))
        stats["mobiles"] = sum(1 for p in ps if p.get("is_mobile"))
        return stats

    # --- one search ----------------------------------------------------------
    def scrape_query(self, tile, query: str) -> tuple[list[dict], str]:
        page = self.browser.page
        s = self.cfg["search"]
        bodies: list[str] = []
        self._saw_429 = False

        def on_response(r):
            try:
                if r.status == 429 and "google." in r.url:
                    self._saw_429 = True
                if "/search?" in r.url and "tbm=map" in r.url and r.status == 200:
                    text = r.text()
                    bodies.append(text)
                    self._dump(f"search_{_slug(query)}", r.url + "\n\n" + text)
            except Exception as e:  # body may be gone after navigation
                log.debug("response read failed: %s", e)

        url = (f"https://www.google.com/maps/search/{urllib.parse.quote(query)}/"
               f"@{tile['lat']},{tile['lng']},{tile['zoom']}z?hl={s['hl']}&gl={s['gl']}")
        page.on("response", on_response)
        try:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                raise TransientError(f"navigation: {e}") from e
            from .browser import handle_consent
            handle_consent(page)
            self.check_block(page)
            time.sleep(random.uniform(3, 6))
            layout = self._wait_results(page)
            self.check_block(page)
            if layout == "single":
                p = self._parse_place_page(page)
                return ([p] if p else []), "single"
            if layout == "feed":
                self._scroll_feed(page, s)
            dom_cards = page.evaluate(DOM_CARDS_JS) if layout == "feed" else []
            if self.dump_dir:
                self._dump(f"page_{_slug(query)}", page.content(), ext="html")
        finally:
            page.remove_listener("response", on_response)

        return self._merge(bodies, dom_cards, layout, query)

    def _wait_results(self, page) -> str:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if page.locator('div[role="feed"]').count():
                return "feed"
            if "/maps/place/" in page.url and page.locator("h1").count():
                return "single"
            self.check_block(page)
            time.sleep(1)
        return "none"

    def _scroll_feed(self, page, s):
        feed = page.locator('div[role="feed"]').first
        box = feed.bounding_box()
        prev, idle = -1, 0
        for _ in range(s["max_scrolls"]):
            if self._at_end(page):
                log.debug("end of list marker")
                break
            if box:
                page.mouse.move(box["x"] + box["width"] * random.uniform(0.3, 0.7),
                                box["y"] + box["height"] * random.uniform(0.3, 0.7), steps=5)
            for _ in range(random.randint(2, 4)):
                page.mouse.wheel(0, random.randint(350, 700))
                time.sleep(random.uniform(0.15, 0.5))
            self.pacer.scroll_delay()
            self.check_block(page)
            n = page.locator('div[role="feed"] a[href*="/maps/place/"]').count()
            idle = idle + 1 if n == prev else 0
            prev = n
            if idle >= s["max_idle_scrolls"]:
                log.debug("no new results after %d scrolls", idle)
                break

    def _at_end(self, page) -> bool:
        if page.locator("span.HlvSq").count():
            return True
        tail = page.evaluate("() => { const f = document.querySelector('div[role=\"feed\"]');"
                             " return f ? f.innerText.slice(-300) : ''; }")
        return any(t in tail for t in END_TEXTS)

    def _merge(self, bodies, dom_cards, layout, query) -> tuple[list[dict], str]:
        places: dict[str, dict] = {}
        parse_errors = []
        for b in bodies:
            try:
                for p in parse_search_body(b):
                    places.setdefault(p["place_id"], p)
            except ParseError as e:
                parse_errors.append(str(e))
                self._dump("parse_error", b, force=True)
        net_n = len(places)
        # DOM fallback: cards whose place id the network layer did not give us.
        # Network ids are ChIJ…; DOM hrefs carry both ChIJ (!19s) and hex ids, so compare on ChIJ.
        known = set(places) | {p.get("hex_id") for p in places.values()}
        dom_added = 0
        for c in dom_cards:
            p = parse_dom_card(c["aria"], c["href"], c["text"])
            if p and p["place_id"] not in known:
                places[p["place_id"]] = p
                dom_added += 1
        if parse_errors:
            log.warning("parse errors (%s): %s", query, parse_errors[0])
            if not places:
                raise TransientError(f"parse error: {parse_errors[0]}")
        if layout == "none" and not bodies:
            raise TransientError("no results feed and no search data")
        source = "network" if net_n and not dom_added else ("dom" if dom_added and not net_n else "mixed")
        if not places:
            source = "empty"
        log.info("  q=%s: %d places (network %d, dom +%d, %d responses)",
                 query, len(places), net_n, dom_added, len(bodies))
        return list(places.values()), source

    # --- detail pages --------------------------------------------------------
    def fill_missing_phones(self, places: list[dict]) -> int:
        opened = failures = 0
        for p in places:
            if p.get("phone"):
                continue
            known = self.db.known_phone(p["place_id"])  # found under another segment already
            if known:
                p.update(known)
                self.db.upsert_place(p)
                continue
            if not self.db.needs_details(p["place_id"]):
                continue
            if not self.pacer.detail_allowed():
                if not self._detail_cap_logged:
                    log.info("detail-page hourly cap reached; remaining phoneless places later")
                    self._detail_cap_logged = True
                break
            self.pacer.record("detail_opens")
            opened += 1
            # A failed detail page must not fail the tile (the searches already succeeded).
            # It stays unchecked and is retried when the place shows up again.
            try:
                phone_raw = self.open_detail(p["maps_url"])
            except TransientError as e:
                failures += 1
                log.warning("  detail failed for %s: %s", p["name"], str(e).splitlines()[0])
                if failures >= 3:
                    log.warning("  %d detail failures in a row; skipping the rest for this tile", failures)
                    break
                self.pacer.detail_delay()
                continue
            failures = 0
            self.db.mark_details_checked(p["place_id"])
            if phone_raw:
                phone = normalize_phone(phone_raw)
                p.update(phone_raw=clean(phone_raw), phone=phone, is_mobile=is_mobile(phone))
                self.db.upsert_place(p)
                log.info("  detail: %s → %s", p["name"], phone)
            self.pacer.detail_delay()
        return opened

    def open_detail(self, url: str) -> str | None:
        page = self.browser.context.new_page()
        try:
            try:
                page.goto(url + "&hl=fa", wait_until="domcontentloaded")
            except Exception as e:
                raise TransientError(f"detail navigation: {e}") from e
            self.check_block(page)
            try:
                page.wait_for_selector("h1", timeout=15000)
            except Exception:
                self.check_block(page)
                return None
            time.sleep(random.uniform(2, 4))
            self.check_block(page)
            return _phone_from_page(page)
        finally:
            page.close()

    def _parse_place_page(self, page) -> dict | None:
        """The search jumped straight to a single place page."""
        name = page.locator("h1").first.inner_text()
        p = parse_dom_card(name, page.url, "")
        if p:
            raw = _phone_from_page(page)
            if raw:
                p.update(make_place(p["place_id"], p["name"], raw, None, p["lat"], p["lng"], None, None))
        return p

    # --- blocks --------------------------------------------------------------
    def check_block(self, page):
        if "/sorry/" in page.url:
            raise BlockDetected(f"sorry page: {page.url[:120]}")
        if self._saw_429:
            raise BlockDetected("HTTP 429")
        if page.locator('iframe[src*="recaptcha"]').count():
            raise BlockDetected("recaptcha iframe")
        try:
            text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 3000) : ''")
        except Exception:
            return
        low = text.lower()
        for t in BLOCK_TEXTS:
            if t in low:
                raise BlockDetected(f"page text: {t!r}")

    # --- helpers -------------------------------------------------------------
    def _in_area(self, tile, p) -> bool:
        if p.get("lat") is None or p.get("lng") is None:
            return False
        if padded_contains(tile, p["lat"], p["lng"], self.cfg["grid"]["pad_ratio"]):
            return True
        return bool(self.city_poly and self.city_poly.contains(Point(p["lng"], p["lat"])))

    def _dump(self, name: str, content: str, ext: str = "txt", force: bool = False):
        d = self.dump_dir or (self.cfg.path("debug") / "errors" if force else None)
        if d is None:
            return
        d.mkdir(parents=True, exist_ok=True)
        self._dump_n += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (d / f"{ts}_{self._dump_n:03d}_{name}.{ext}").write_text(content, encoding="utf-8")


def _phone_from_page(page) -> str | None:
    btn = page.locator('[data-item-id^="phone:tel:"]').first
    if not btn.count():
        return None
    item = btn.get_attribute("data-item-id") or ""
    label = btn.get_attribute("aria-label") or ""
    # aria-label is "تلفن: 026 1234 5678" (local format); data-item-id is "phone:tel:+98…"
    local = label.split(":", 1)[-1].strip() if ":" in label else ""
    return local or item.removeprefix("phone:tel:") or None


def _slug(s: str) -> str:
    """Safe file-name part: keywords come from config or the hub and may contain / or .."""
    return re.sub(r"[^\w\-]+", "_", s).strip("_")[:60] or "q"


__all__ = ["TileScraper", "BlockDetected", "TransientError", "Stopped"]
