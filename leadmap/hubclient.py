"""Worker side of the hub: claim a city, send heartbeats (progress + new places), report completion.

A hub that is down never stops scraping: the worker keeps its last assignment (state
`hub_city`) and retries; places are pushed from a persisted cursor, so nothing is lost.
"""
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

SYNC_FIELDS = ("place_id", "name", "phone_raw", "phone", "is_mobile", "address", "lat", "lng",
               "category", "rating", "maps_url", "city", "tile_id", "first_seen", "last_seen")


class HubUnavailable(Exception):
    """The hub could not be reached or refused the request."""


class HubClient:
    def __init__(self, cfg, db):
        h = cfg["hub"]
        self.url = h["url"].rstrip("/")
        self.token = h["token"]
        self.db = db
        self.interval = h.get("heartbeat_s", 60)
        self.batch = h.get("sync_batch", 500)
        self.ctx = ssl.create_default_context()
        if h.get("insecure_tls"):  # self-signed hub certificate
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.last_beat = 0.0
        self.hostname = socket.gethostname()

    @staticmethod
    def configured(cfg) -> bool:
        h = cfg.get("hub") or {}
        return bool(h.get("url") and h.get("token"))

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps({**payload, "hostname": self.hostname}, ensure_ascii=False).encode()
        req = urllib.request.Request(f"{self.url}/api/hub/w/{path}", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code == 401:
                detail = "token rejected (revoked or wrong hub.token in config.yaml)"
            raise HubUnavailable(f"HTTP {e.code} on {path}: {detail}") from None
        except (OSError, ValueError) as e:
            raise HubUnavailable(f"{path}: {e}") from None

    # --- calls -------------------------------------------------------------------
    def claim(self) -> dict:
        return self._post("claim", {"prefer": self.started_cities()})

    def complete(self, city: str):
        self._post("complete", {"city": city, "tiles": self.db.tile_counts(city)})

    def fail(self, city: str, error: str):
        self._post("fail", {"city": city, "error": error})

    def heartbeat(self, report: dict, force: bool = False) -> dict | None:
        """Throttled; returns the hub's reply or None when skipped. Pushes one batch of places."""
        if not force and time.monotonic() - self.last_beat < self.interval:
            return None
        self.last_beat = time.monotonic()
        cursor = self.db.get_state("hub_sync_cursor") or ["", ""]
        places = self._places_after(cursor)
        city = report.get("city")
        payload = {**report, "places": places, "sync_backlog": self._backlog(cursor),
                   "tiles": self.db.tile_counts(city) if city else None}
        reply = self._post("heartbeat", payload)
        if places:
            last = places[-1]
            self.db.set_state("hub_sync_cursor", [last["last_seen"], last["place_id"]])
            log.info("hub: pushed %d places", len(places))
            if len(places) == self.batch:
                self.last_beat = 0.0  # more waiting: send the next batch on the next tick
        return reply

    # --- local data -------------------------------------------------------------------
    def _places_after(self, cursor) -> list[dict]:
        ts, pid = cursor
        rows = self.db.conn.execute(
            f"SELECT {', '.join(SYNC_FIELDS)} FROM places "
            "WHERE last_seen > ? OR (last_seen = ? AND place_id > ?) "
            "ORDER BY last_seen, place_id LIMIT ?", (ts, ts, pid, self.batch)).fetchall()
        return [dict(r) for r in rows]

    def _backlog(self, cursor) -> int:
        ts, pid = cursor
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM places WHERE last_seen > ? OR (last_seen = ? AND place_id > ?)",
            (ts, ts, pid)).fetchone()[0]

    def started_cities(self) -> list[str]:
        """Cities with pending tiles here, most recently worked first (resume them before new ones)."""
        rows = self.db.conn.execute(
            "SELECT city, MAX(CASE WHEN status<>'pending' THEN updated_at END) last, "
            "SUM(status='pending') pending FROM tiles GROUP BY city "
            "HAVING pending > 0 ORDER BY last IS NULL, last DESC").fetchall()
        return [r["city"] for r in rows]
