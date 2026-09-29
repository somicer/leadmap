"""Read-only status dashboard over HTTPS with basic auth (stdlib only, no tokens, no Claude)."""
import base64
import hmac
import json
import logging
import os
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from urllib.parse import quote

from .db import DB
from .hub import HubDB, HubError, export_hub
from .service import backoff_remaining

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("dashboard.html")
HUB_PAGE = Path(__file__).with_name("hub.html")
MAX_BODY = 8_000_000


class _CertReloader:
    """Reload the certificate when acme.sh renews it (checked at most once a minute)."""

    def __init__(self, ctx, cert, key):
        self.ctx, self.cert, self.key = ctx, cert, key
        self.mtime = 0.0
        self.checked = 0.0
        self.lock = threading.Lock()
        self.check(force=True)

    def check(self, force=False):
        now = time.monotonic()
        if not force and now - self.checked < 60:
            return
        with self.lock:
            self.checked = now
            m = os.path.getmtime(self.cert)
            if m != self.mtime:
                self.ctx.load_cert_chain(self.cert, self.key)
                self.mtime = m
                log.info("TLS certificate loaded (%s)", self.cert)


class _Failures:
    """Lock an IP out for 10 minutes after 10 failed logins."""

    def __init__(self):
        self.d: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def blocked(self, ip):
        with self.lock:
            recent = [t for t in self.d.get(ip, []) if t > time.time() - 600]
            self.d[ip] = recent
            return len(recent) >= 10

    def add(self, ip):
        with self.lock:
            self.d.setdefault(ip, []).append(time.time())


def collect(cfg, db) -> dict:
    tz = ZoneInfo(cfg["pacing"]["timezone"])
    job = db.get_state("hub_job") or {"city": cfg["city"], "segment": cfg["search"]["segment"]}
    cities = []
    for c, seg in db.jobs():
        tc = db.tile_counts(c, seg)
        pc = db.place_counts(c, seg)
        cities.append({"city": c, "segment": seg, "tiles": tc, "tiles_total": sum(tc.values()),
                       "places": pc["total"], "phones": pc["phones"], "mobiles": pc["mobiles"]})
    recent = [dict(r) for r in db.conn.execute(
        "SELECT id, status, results_count, attempts, last_error, updated_at FROM tiles "
        "WHERE status<>'pending' OR last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 12")]
    events = [dict(r) for r in db.conn.execute(
        "SELECT timestamp, type, detail FROM events WHERE type NOT IN ('start','stop') "
        "ORDER BY id DESC LIMIT 10")]
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    new_today = db.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(is_mobile),0) FROM places WHERE first_seen >= ?",
        (today_start.astimezone(timezone.utc).isoformat(timespec="seconds"),)).fetchone()
    bo = db.get_state("backoff") or {}
    try:
        active = subprocess.run(["systemctl", "is-active", "leadmap"], capture_output=True,
                                text=True, timeout=5).stdout.strip()
    except Exception:
        active = "unknown"
    log_tail = []
    lp = cfg.path("logs") / "run.log"
    if lp.exists():
        with open(lp, "rb") as f:
            f.seek(max(0, lp.stat().st_size - 20000))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
        log_tail = [ln for ln in lines if " DEBUG " not in ln][-25:]
    exports = sorted((p.name for p in cfg.path("exports").glob("*.xlsx")
                      if not p.name.endswith(".tmp.xlsx")), reverse=True)[:15]
    return {
        "now": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "service": active, "city": job["city"], "segment": job["segment"], "cities": cities,
        "new_today": new_today[0], "new_today_mobile": new_today[1],
        "backoff": {"level": bo.get("level", 0), "until": bo.get("until"),
                    "reason": bo.get("reason"), "remaining_s": backoff_remaining(db)},
        "tiles_last_hour": len([t for t in (db.get_state("tile_runs", []) or []) if t > time.time() - 3600]),
        "active_hours": cfg["pacing"]["active_hours"],
        "recent_tiles": recent, "events": events, "log": log_tail, "exports": exports,
        "hub_server": bool(cfg["hub"].get("server")), "hub_url": cfg["hub"].get("url") or None,
    }


def make_handler(cfg, user, password, reloader, failures):
    db_path = cfg.path("db")
    exports_dir = cfg.path("exports")
    hub_on = bool(cfg["hub"].get("server"))
    hub_path = cfg.path("hub_db")
    hub_exports = exports_dir / "hub"
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class H(BaseHTTPRequestHandler):
        server_version = "leadmap"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.client_address[0], fmt % args)

        def _auth(self) -> bool:
            ip = self.client_address[0]
            if failures.blocked(ip):
                self.send_error(429, "Too many failed logins")
                return False
            if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                return True
            if self.headers.get("Authorization"):
                failures.add(ip)
                log.warning("dashboard: failed login from %s", ip)
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="leadmap"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, code, body: bytes, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise HubError(413, "request too large")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise HubError(400, "invalid JSON") from None
            if not isinstance(data, dict):
                raise HubError(400, "expected a JSON object")
            return data

        def _download(self, folder: Path, name: str):
            f = (folder / name).resolve()
            if f.parent != folder.resolve() or not f.is_file() or f.suffix != ".xlsx":
                self.send_error(404)
                return
            self._send(200, f.read_bytes(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

        def _worker_api(self, op: str):
            """Worker endpoints: bearer token instead of the admin password."""
            ip = self.client_address[0]
            if failures.blocked(ip):
                self.send_error(429, "Too many failed logins")
                return
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            hub = HubDB(hub_path)
            try:
                w = hub.worker_by_token(token)
                if w is None:
                    failures.add(ip)
                    log.warning("hub: bad worker token from %s", ip)
                    self._json(401, {"error": "bad token"})
                    return
                fn = {"claim": hub.claim, "heartbeat": hub.heartbeat,
                      "complete": hub.complete, "fail": hub.fail}.get(op)
                if fn is None:
                    self.send_error(404)
                    return
                self._json(200, fn(w, ip, self._body()))
            except HubError as e:
                self._json(e.code, {"error": str(e)})
            finally:
                hub.close()

        def _hub_admin(self, data: dict):
            hub = HubDB(hub_path)
            try:
                op = data.get("op")
                if op == "add_jobs":
                    names = data.get("names") or []
                    if isinstance(names, str):
                        names = names.splitlines()
                    segments = data.get("segments") or []
                    if not isinstance(segments, list) or not segments:
                        raise HubError(400, "choose at least one segment")
                    added = {}
                    for seg in segments[:50]:
                        added[str(seg)] = hub.add_jobs([n for n in names if isinstance(n, str)][:500], str(seg))
                    return {"added": added}
                if op == "job":
                    hub.job_action(int(data.get("id") or 0), str(data.get("action")))
                    return {"ok": True}
                if op == "set_segment":
                    kws = data.get("keywords") or []
                    if isinstance(kws, str):
                        kws = kws.replace("،", "\n").replace(",", "\n").splitlines()
                    return {"keywords": hub.set_segment(str(data.get("name") or ""), kws)}
                if op == "delete_segment":
                    hub.delete_segment(str(data.get("name") or ""))
                    return {"ok": True}
                if op == "add_worker":
                    return {"token": hub.add_worker(str(data.get("name") or ""))}
                if op == "worker":
                    hub.worker_action(int(data.get("id") or 0), str(data.get("action")))
                    return {"ok": True}
                if op == "export":
                    return {"message": export_hub(cfg, hub)}
                raise HubError(400, f"unknown op {op!r}")
            finally:
                hub.close()

        def _hub_state(self) -> dict:
            hub = HubDB(hub_path)
            try:
                data = hub.overview()
            finally:
                hub.close()
            data["exports"] = sorted((p.name for p in hub_exports.glob("*.xlsx")
                                      if not p.name.endswith(".tmp.xlsx")), reverse=True) \
                if hub_exports.exists() else []
            data["public_url"] = f"https://{cfg['dashboard'].get('public_host')}:{cfg['dashboard']['port']}"
            return data

        def do_GET(self):
            reloader.check()
            if not self._auth():
                return
            path = self.path.split("?", 1)[0]
            try:
                if hub_on and path == "/hub":
                    self._send(200, HUB_PAGE.read_bytes(), "text/html; charset=utf-8")
                elif hub_on and path == "/api/hub/state":
                    self._json(200, self._hub_state())
                elif hub_on and path.startswith("/hub-exports/"):
                    from urllib.parse import unquote
                    self._download(hub_exports, unquote(path.rsplit("/", 1)[-1]))
                elif path == "/":
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/status":
                    db = DB(db_path)
                    try:
                        data = collect(cfg, db)
                    finally:
                        db.close()
                    self._send(200, json.dumps(data, ensure_ascii=False).encode(),
                               "application/json; charset=utf-8")
                elif path.startswith("/exports/"):
                    self._download(exports_dir, path.rsplit("/", 1)[-1])
                else:
                    self.send_error(404)
            except Exception:
                log.exception("dashboard request failed")
                self.send_error(500)

        def do_POST(self):
            reloader.check()
            if hub_on and self.path.startswith("/api/hub/w/"):
                self._worker_api(self.path.rsplit("/", 1)[-1])
                return
            if not self._auth():
                return
            if hub_on and self.path == "/api/hub/admin":
                # custom header: a cross-site form can't send it, so basic-auth creds can't be abused (CSRF)
                if self.headers.get("X-Leadmap") != "1":
                    self.send_error(403)
                    return
                try:
                    self._json(200, self._hub_admin(self._body()))
                except HubError as e:
                    self._json(e.code, {"error": str(e)})
                except Exception:
                    log.exception("hub admin request failed")
                    self._json(500, {"error": "internal error (see logs/dashboard.log)"})
                return
            if self.path != "/api/export":
                self.send_error(404)
                return
            try:
                from .export import export_all
                db = DB(db_path)
                try:
                    export_all(cfg, db)
                finally:
                    db.close()
                self._send(200, b'{"ok":true}', "application/json")
            except Exception:
                log.exception("export from dashboard failed")
                self.send_error(500)

    return H


def _hub_daily_export(cfg):
    """Merged Excel files of all workers at export.daily_time (Tehran)."""
    tz = ZoneInfo(cfg["pacing"]["timezone"])
    hh, mm = map(int, cfg["export"]["daily_time"].split(":"))
    while True:
        try:
            now = datetime.now(tz)
            today = now.date().isoformat()
            hub = HubDB(cfg.path("hub_db"))
            try:
                if (now.hour, now.minute) >= (hh, mm) and hub.get_state("last_export_date") != today:
                    export_hub(cfg, hub, now.date())
                    hub.set_state("last_export_date", today)
            finally:
                hub.close()
        except Exception:
            log.exception("hub daily export failed")
        time.sleep(300)


def serve(cfg):
    d = cfg["dashboard"]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    reloader = _CertReloader(ctx, d["cert"], d["key"])

    class TLSServer(ThreadingHTTPServer):
        daemon_threads = True

        def finish_request(self, request, client_address):
            # Handshake per connection in its own thread, so a silent client can't stall accept().
            request.settimeout(20)
            tls = ctx.wrap_socket(request, server_side=True)
            super().finish_request(tls, client_address)

        def handle_error(self, request, client_address):
            log.debug("connection error from %s", client_address, exc_info=True)

    if cfg["hub"].get("server"):
        threading.Thread(target=_hub_daily_export, args=(cfg,), daemon=True).start()
    handler = make_handler(cfg, d["user"], d["password"], reloader, _Failures())
    handler.timeout = 30
    httpd = TLSServer((d.get("host", "0.0.0.0"), d["port"]), handler)
    log.info("Dashboard on https://%s:%s", d.get("public_host", "0.0.0.0"), d["port"])
    httpd.serve_forever()
