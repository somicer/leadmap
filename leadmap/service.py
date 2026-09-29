"""Long-running service: tile loop, backoff, retries, daily export, graceful shutdown."""
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .browser import Browser, prune_dir
from .grid import init_city, load_city_polygon
from .hubclient import HubUnavailable
from .pacing import Pacer, Stopped
from .scrape import BlockDetected, TileScraper

log = logging.getLogger(__name__)
_UNSET = object()


# --- backoff state (persisted in DB) -------------------------------------------
def backoff_status(db) -> str:
    st = db.get_state("backoff") or {}
    until = st.get("until")
    if until and datetime.fromisoformat(until) > datetime.now(timezone.utc):
        left = datetime.fromisoformat(until) - datetime.now(timezone.utc)
        return (f"ACTIVE level {st.get('level')} until {until} "
                f"({left.total_seconds() / 3600:.1f}h left) — {st.get('reason', '')}")
    return f"none (level {st.get('level', 0)})"


def enter_backoff(cfg, db, reason: str) -> float:
    hours_list = cfg["blocks"]["backoff_hours"]
    st = db.get_state("backoff") or {}
    level = st.get("level", 0) + 1
    hours = hours_list[min(level - 1, len(hours_list) - 1)]
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    db.set_state("backoff", {"level": level, "until": until.isoformat(timespec="seconds"),
                             "reason": reason})
    db.event("block", f"{reason} → backoff level {level}, {hours}h")
    log.warning("BLOCK: %s → backing off %sh (level %d)", reason, hours, level)
    return hours * 3600


def reset_backoff(db):
    st = db.get_state("backoff") or {}
    if st.get("level"):
        db.set_state("backoff", {"level": 0, "until": None})
        log.info("Backoff reset after successful tile")


def backoff_remaining(db) -> float:
    until = (db.get_state("backoff") or {}).get("until")
    if not until:
        return 0.0
    return max(0.0, (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds())


def dump_page(cfg, browser, sub: str, tag: str):
    """Screenshot + HTML of the current page into debug/<sub>/."""
    d = cfg.path("debug") / sub
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page = browser.page
        page.screenshot(path=str(d / f"{ts}_{tag}.png"))
        (d / f"{ts}_{tag}.html").write_text(page.content(), encoding="utf-8")
        (d / f"{ts}_{tag}.url").write_text(page.url, encoding="utf-8")
    except Exception as e:
        log.warning("could not dump page: %s", e)
    prune_dir(cfg.path("debug"), cfg["paths"]["debug_max_mb"])


def summarize(tile, stats) -> str:
    return (f"tile {tile['id']} (#{tile['seq']}): kept {stats['kept']} "
            f"(raw {stats['raw']}), new {stats['new']}, phones {stats['phones']}, "
            f"mobiles {stats['mobiles']}, details {stats['details']}, "
            f"src {'/'.join(sorted(stats['source']))}")


class Service:
    def __init__(self, cfg, db, city: str | None, headed: bool = False, hub=None):
        """`hub` (HubClient): take cities from the central queue instead of a fixed `city`."""
        self.cfg = cfg
        self.db = db
        self.city = city
        self.hub = hub
        self.stop = threading.Event()
        self.browser = Browser(cfg, headed=headed)
        self.pacer = Pacer(cfg, db, self.stop, tick=self.tick)
        self.city_poly = load_city_polygon(city, cfg) if city else None
        self.empty_streak: list[str] = []
        self.phase = "starting"
        self.current_tile = None
        self.hub_assignment = _UNSET   # last assignment the hub reported
        self.hub_paused = False
        self._hub_warned = 0.0

    # --- lifecycle -----------------------------------------------------------
    def _on_signal(self, signum, _frame):
        log.info("Signal %s received — finishing current step and shutting down", signum)
        self.stop.set()

    def run(self):
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        if self.hub:
            log.info("Service started in hub mode (%s)", self.hub.url)
            self.db.event("start", f"hub={self.hub.url}")
        elif not sum(self.db.tile_counts(self.city).values()):
            log.error("No tiles for %s — run: python main.py init --city %s", self.city, self.city)
            return
        else:
            self.db.event("start", f"city={self.city}")
            log.info("Service started for %s. %s", self.city, self._progress())
        try:
            while not self.stop.is_set():
                self.loop_once()
        except Stopped:
            pass
        finally:
            self.browser.close()
            self.db.event("stop", "graceful")
            log.info("Service stopped cleanly")

    def loop_once(self):
        self.tick()
        if self.hub and not self.hub_ready():
            return
        wait = backoff_remaining(self.db)
        if wait > 0:
            self.phase = "backoff"
            self.browser.close()
            self.pacer.sleep(wait, f"backoff: {backoff_status(self.db)}")
            return
        if self.pacer.seconds_until_active() > 0:
            self.phase = "outside active hours"
            self.browser.close()
            self.pacer.wait_active_hours()
            return
        tile = self.db.next_tile(self.city)
        if tile is None:
            self.browser.close()
            if self.hub:
                self.finish_city()
                return
            log.info("No pending tiles. %s", self._progress())
            self.pacer.sleep(3600, "all tiles processed; idling (daily export still runs)")
            return
        self.phase = "hourly cap"
        self.pacer.wait_for_cap("tile_runs", self.cfg["pacing"]["max_tiles_per_hour"])
        if self.pacer.seconds_until_active() > 0:
            return
        self.ensure_browser()
        self.phase, self.current_tile = "scraping", tile["id"]
        if self.hub:
            self.hub_heartbeat(force=True)  # the panel shows which tile is being scraped
        try:
            self.process_tile(tile)
        finally:
            self.phase, self.current_tile = "pause", None

    # --- hub mode ------------------------------------------------------------
    def hub_ready(self) -> bool:
        """Make sure we hold a city from the hub; False means "slept instead, loop again"."""
        if self.hub_paused:
            self.phase = "paused"
            self.browser.close()
            self.pacer.sleep(300, "paused from the hub panel")
            return False
        if self.city is not None and self.hub_assignment not in (_UNSET, self.city):
            log.warning("hub: %s was taken off this server (assignment now: %s)",
                        self.city, self.hub_assignment or "none")
            self.db.event("hub", f"dropped {self.city}")
            self.set_city(None)
        if self.city is not None:
            return True
        self.browser.close()
        try:
            reply = self.hub.claim()
        except HubUnavailable as e:
            cached = self.db.get_state("hub_city")
            if cached:
                log.warning("hub unreachable (%s); continuing with %s", e, cached)
                return self.start_city(cached)
            log.warning("hub unreachable (%s); retrying in 5 min", e)
            self.phase = "hub unreachable"
            self.pacer.sleep(300, "hub unreachable")
            return False
        self.hub_assignment = reply.get("city")
        if reply.get("paused"):
            self.hub_paused = True
            return False
        if not reply.get("city"):
            self.phase = "idle (queue empty)"
            self.pacer.sleep(self.cfg["hub"].get("idle_poll_s", 900), "hub queue empty")
            return False
        return self.start_city(reply["city"])

    def start_city(self, city: str) -> bool:
        resuming = city == self.db.get_state("hub_city")
        self.phase = "building grid"
        try:
            r = init_city(self.cfg, self.db, city)
        except RuntimeError as e:  # Nominatim knows no such place: the city itself is bad
            log.error("hub: cannot build grid for %s: %s", city, e)
            try:
                self.hub.fail(city, str(e))
            except HubUnavailable as he:
                log.warning("hub: could not report failure: %s", he)
            self.pacer.sleep(60, "city rejected")
            return False
        except Exception as e:  # network trouble: keep the assignment, try again later
            log.warning("hub: grid for %s failed (%s: %s); retrying in 10 min", city, type(e).__name__, e)
            self.pacer.sleep(600, "grid retry")
            return False
        if not resuming:
            n = self.db.reset_failed(city)  # a re-queued city retries its failed tiles
            if n:
                log.info("hub: %d failed tiles of %s reset to pending", n, city)
        self.set_city(city, r["poly"])
        log.info("hub: working on %s (%d tiles, %d new). %s",
                 city, r["tiles"], r["added"], self._progress())
        self.db.event("hub", f"{'resumed' if resuming else 'claimed'} {city}")
        self.hub_heartbeat(force=True)
        return True

    def set_city(self, city, poly=None):
        self.city, self.city_poly = city, poly
        self.empty_streak = []
        self.db.set_state("hub_city", city)

    def finish_city(self):
        self.phase = "finishing city"
        try:
            self.hub.complete(self.city)
        except HubUnavailable as e:
            log.warning("hub: could not report %s finished (%s); retrying in 5 min", self.city, e)
            self.pacer.sleep(300, "hub unreachable")
            return
        log.info("hub: finished %s. %s", self.city, self._progress())
        self.db.event("hub", f"finished {self.city}")
        self.set_city(None)

    def hub_report(self) -> dict:
        return {"phase": self.phase, "city": self.city, "tile": self.current_tile,
                "backoff": backoff_status(self.db),
                "progress": self._progress() if self.city else None,
                "tiles_last_hour": self.pacer.count_last_hour("tile_runs")}

    def hub_heartbeat(self, force=False):
        try:
            reply = self.hub.heartbeat(self.hub_report(), force=force)
        except HubUnavailable as e:
            if time.monotonic() - self._hub_warned > 1800:
                log.warning("hub heartbeat failed: %s", e)
                self._hub_warned = time.monotonic()
            return
        except Exception:
            log.exception("hub heartbeat failed")
            return
        if reply is not None:
            self.hub_assignment = reply.get("assignment")
            self.hub_paused = bool(reply.get("paused"))

    def ensure_browser(self):
        if self.browser.alive and self.browser.age_hours() >= self.cfg["pacing"]["browser_restart_hours"]:
            log.info("Restarting browser (age %.1fh)", self.browser.age_hours())
            self.browser.close()
            self.pacer.sleep(60, "browser restart")
        if not self.browser.alive:
            self.browser.start()

    # --- one tile ------------------------------------------------------------
    def process_tile(self, tile):
        scraper = TileScraper(self.cfg, self.db, self.browser, self.pacer, self.city_poly)
        log.info("Tile %s (#%d) @ %.5f,%.5f — attempt %d",
                 tile["id"], tile["seq"], tile["lat"], tile["lng"], tile["attempts"] + 1)
        self.pacer.record("tile_runs")
        try:
            stats = scraper.scrape_tile(tile)
        except Stopped:
            log.info("Interrupted during tile %s; it stays pending", tile["id"])
            raise
        except BlockDetected as e:
            self.on_block(tile, str(e))
            return
        except Exception as e:  # never let one tile kill the service
            if self.stop.is_set():  # browser died because we are shutting down
                log.info("Interrupted during tile %s (%s); it stays pending",
                         tile["id"], type(e).__name__)
                raise Stopped() from e
            self.on_error(tile, e)
            return

        status = "done" if stats["kept"] else "empty"
        self.db.update_tile(tile["id"], status=status, results_count=stats["kept"],
                            attempts=tile["attempts"] + 1, last_error=None)
        log.info("%s | %s", summarize(tile, stats), self._progress())

        if status == "empty" and self.db.neighbour_had_results(tile):
            self.empty_streak.append(tile["id"])
            if len(self.empty_streak) >= self.cfg["blocks"]["empty_streak_threshold"]:
                for tid in self.empty_streak:
                    self.db.update_tile(tid, status="pending", results_count=0)
                reason = f"{len(self.empty_streak)} empty tiles in a row next to productive ones"
                self.empty_streak = []
                dump_page(self.cfg, self.browser, "blocks", "soft_block")
                self.browser.close()
                enter_backoff(self.cfg, self.db, reason)
                return
        else:
            self.empty_streak = []
            reset_backoff(self.db)

        self.pacer.maybe_break()
        self.pacer.tile_pause()

    def on_block(self, tile, reason: str):
        dump_page(self.cfg, self.browser, "blocks", "block")
        self.browser.close()
        self.db.update_tile(tile["id"], status="pending", last_error=f"block: {reason}")
        self.empty_streak = []
        enter_backoff(self.cfg, self.db, reason)

    def on_error(self, tile, e: Exception):
        attempts = tile["attempts"] + 1
        err = f"{type(e).__name__}: {e}"[:500]
        log.exception("Tile %s failed (attempt %d): %s", tile["id"], attempts, err)
        dump_page(self.cfg, self.browser, "errors", f"tile_{tile['row']}_{tile['col']}")
        max_attempts = self.cfg["retries"]["max_attempts"]
        status = "failed" if attempts >= max_attempts else "pending"
        self.db.update_tile(tile["id"], status=status, attempts=attempts, last_error=err)
        self.db.event("error", f"{tile['id']} attempt {attempts}: {err}")
        self.browser.close()  # start fresh next time
        if status == "pending":
            delays = self.cfg["retries"]["retry_delay_s"]
            self.pacer.sleep(delays[min(attempts - 1, len(delays) - 1)], "retry delay")
        else:
            self.pacer.tile_pause()

    # --- periodic ------------------------------------------------------------
    def tick(self):
        """Called between tiles and during long sleeps: runs the daily export when due."""
        try:
            now = self.pacer.now()
            hh, mm = map(int, self.cfg["export"]["daily_time"].split(":"))
            today = now.date().isoformat()
            if (now.hour, now.minute) >= (hh, mm) and self.db.get_state("last_export_date") != today:
                from .export import export_all
                export_all(self.cfg, self.db, now.date())
                self.db.set_state("last_export_date", today)
        except Exception:
            log.exception("daily export failed")
        if self.hub:
            self.hub_heartbeat()

    def _progress(self) -> str:
        tc = self.db.tile_counts(self.city)
        pc = self.db.place_counts(self.city)
        total = sum(tc.values())
        finished = tc.get("done", 0) + tc.get("empty", 0)
        return (f"progress {finished}/{total} tiles (failed {tc.get('failed', 0)}), "
                f"places {pc['total']}, mobiles {pc['mobiles']}")


# --- test-tile -----------------------------------------------------------------
def test_tile(cfg, db, city: str, tile_id: str | None, headed: bool = False):
    poly = load_city_polygon(city, cfg)
    if tile_id:
        tile = db.get_tile(tile_id)
    else:
        c = poly.centroid
        tile = min(db.conn.execute("SELECT * FROM tiles WHERE city=?", (city,)),
                   key=lambda t: (t["lat"] - c.y) ** 2 + (t["lng"] - c.x) ** 2, default=None)
    if tile is None:
        print("No such tile — run init first.")
        return
    wait = backoff_remaining(db)
    if wait > 0:
        print(f"Backoff active: {backoff_status(db)} — not scraping.")
        return
    dump_dir = cfg.path("debug") / f"test_tile_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    pacer = Pacer(cfg, db, stop)
    browser = Browser(cfg, headed=headed)
    print(f"Tile {tile['id']} @ {tile['lat']},{tile['lng']} z{tile['zoom']} — raw dumps → {dump_dir}")
    t0 = time.monotonic()
    try:
        browser.start()
        stats = TileScraper(cfg, db, browser, pacer, poly, dump_dir=dump_dir).scrape_tile(tile)
    except BlockDetected as e:
        dump_page(cfg, browser, "blocks", "block")
        db.update_tile(tile["id"], status="pending", last_error=f"block: {e}")
        enter_backoff(cfg, db, str(e))
        print(f"BLOCKED: {e}. Backoff recorded; see debug/blocks/")
        return
    except Stopped:
        print("Interrupted.")
        return
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {str(e).splitlines()[0]}")
        print("Places parsed before the failure are already saved; see `python main.py status`.")
        return
    finally:
        browser.close()
    db.update_tile(tile["id"], status="done" if stats["kept"] else "empty",
                   results_count=stats["kept"], attempts=tile["attempts"] + 1, last_error=None)
    reset_backoff(db)
    _print_results(tile, stats, time.monotonic() - t0, dump_dir)


def _print_results(tile, stats, secs, dump_dir: Path):
    places = sorted(stats["places"].values(), key=lambda p: (not p.get("is_mobile"), p.get("phone") is None))
    print()
    print(f"{'#':>3}  {'phone':<12} {'M':1}  {'rating':>6}  name / category")
    for i, p in enumerate(places, 1):
        print(f"{i:>3}  {p.get('phone') or '-':<12} {'M' if p.get('is_mobile') else ' '}  "
              f"{p.get('rating') if p.get('rating') is not None else '-':>6}  "
              f"{p.get('name')} / {p.get('category') or '-'}")
    n = stats["kept"]
    print()
    print(summarize(tile, stats))
    if n:
        print(f"with phone: {stats['phones']}/{n} ({stats['phones'] / n:.0%})   "
              f"mobile: {stats['mobiles']}/{n} ({stats['mobiles'] / n:.0%})   "
              f"mobile among phones: {stats['mobiles'] / max(stats['phones'], 1):.0%}")
    print(f"took {secs / 60:.1f} min; raw responses in {dump_dir}")
