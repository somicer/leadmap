import threading
from http.server import ThreadingHTTPServer

import openpyxl
import pytest
import yaml

import leadmap.service as service_mod
from leadmap.config import ROOT, Config
from leadmap.dashboard import _Failures, make_handler
from leadmap.db import DB
from leadmap.hub import HubDB, HubError, export_hub
from leadmap.hubclient import HubClient, HubUnavailable


def place(pid, **kw):
    return {"place_id": pid, "name": f"املاک {pid}", "phone": None, "is_mobile": False,
            "first_seen": "2026-09-20T10:00:00+00:00", "last_seen": "2026-09-20T10:00:00+00:00", **kw}


@pytest.fixture
def hub(tmp_path):
    h = HubDB(tmp_path / "hub.db")
    yield h
    h.close()


def worker(hub, name):
    token = hub.add_worker(name)
    return hub.worker_by_token(token)


def test_claim_order_prefer_and_exclusive(hub):
    hub.add_cities(["شیراز", "مشهد", "اصفهان"])
    a, b = worker(hub, "a"), worker(hub, "b")
    assert hub.claim(a, "1.1.1.1", {"prefer": ["اصفهان"]})["city"] == "اصفهان"  # resumes its own city
    assert hub.claim(a, "1.1.1.1", {})["city"] == "اصفهان"                     # idempotent
    assert hub.claim(b, "2.2.2.2", {"prefer": ["اصفهان"]})["city"] == "شیراز"  # taken → queue head
    hub.city_action("مشهد", "top")
    c = worker(hub, "c")
    assert hub.claim(c, "3.3.3.3", {})["city"] == "مشهد"
    assert hub.claim(worker(hub, "d"), "4.4.4.4", {})["city"] is None       # queue empty


def test_complete_release_pause_revoke(hub):
    hub.add_cities(["کرج", "قم"])
    a = worker(hub, "a")
    assert hub.claim(a, "ip", {})["city"] == "کرج"
    hub.complete(a, "ip", {"city": "کرج", "tiles": {"done": 9, "empty": 1}})
    o = {c["name"]: c for c in hub.overview()["cities"]}
    assert o["کرج"]["status"] == "done" and o["کرج"]["tiles"] == {"done": 9, "empty": 1}
    assert hub.claim(a, "ip", {})["city"] == "قم"
    hub.city_action("قم", "release")                    # admin takes it back
    assert hub.heartbeat(a, "ip", {"city": "قم"})["assignment"] is None
    hub.worker_action(a["id"], "pause")
    a = hub.conn.execute("SELECT * FROM workers WHERE id=?", (a["id"],)).fetchone()
    assert hub.claim(a, "ip", {}) == {"city": None, "paused": True}
    hub.worker_action(a["id"], "resume")
    b = worker(hub, "b")
    assert hub.claim(b, "ip", {})["city"] == "قم"
    hub.worker_action(b["id"], "revoke")                 # revoked → city back to queue, token dead
    assert hub.worker_by_token("x") is None
    assert {c["name"]: c["status"] for c in hub.overview()["cities"]}["قم"] == "queued"
    with pytest.raises(HubError):
        hub.city_action("کرج", "release")               # not active


def test_fail_and_requeue(hub):
    hub.add_cities(["ناکجاآباد"])
    a = worker(hub, "a")
    hub.claim(a, "ip", {})
    hub.fail(a, "ip", {"city": "ناکجاآباد", "error": "Nominatim returned nothing"})
    c = hub.overview()["cities"][0]
    assert c["status"] == "error" and "Nominatim" in c["last_error"]
    hub.city_action("ناکجاآباد", "requeue")
    assert hub.overview()["cities"][0]["status"] == "queued"


def test_place_merge(hub):
    a, b = worker(hub, "a"), worker(hub, "b")
    hub.heartbeat(a, "ip", {"places": [place("p1", city="کرج", last_seen="2026-09-21T00:00:00+00:00")]})
    hub.heartbeat(b, "ip", {"places": [place("p1", city="تهران", phone="09121112233", is_mobile=True,
                                             first_seen="2026-09-19T00:00:00+00:00"), {"bad": 1}]})
    r = hub.conn.execute("SELECT * FROM places").fetchone()
    assert r["phone"] == "09121112233" and r["is_mobile"] == 1
    assert r["city"] == "کرج"                                  # first city wins
    assert r["first_seen"].startswith("2026-09-19") and r["last_seen"].startswith("2026-09-21")
    hub.heartbeat(a, "ip", {"places": [place("p1")]})          # later record without phone keeps it
    assert hub.conn.execute("SELECT is_mobile, phone FROM places").fetchone()[0] == 1


def test_export_hub(hub, tmp_path):
    a = worker(hub, "a")
    hub.heartbeat(a, "ip", {"places": [place("p1", city="کرج", phone="09121112233", is_mobile=True),
                                       place("p2", city="قم")]})
    cfg = Config({"pacing": {"timezone": "Asia/Tehran"}, "paths": {"exports": str(tmp_path / "ex")}})
    export_hub(cfg, hub)
    out = tmp_path / "ex" / "hub"
    wb = openpyxl.load_workbook(out / "all_leads.xlsx")
    assert wb["همه"]["A1"].value == "city" and wb["همه"].max_row == 3 and wb["موبایل"].max_row == 2
    assert {p.name for p in out.glob("city_*.xlsx")} == {"city_کرج.xlsx", "city_قم.xlsx"}


# --- over HTTP: dashboard handler + worker client ------------------------------------
@pytest.fixture
def server(tmp_path):
    base = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    base["paths"] = {k: str(tmp_path / k) for k in ("db", "exports", "logs", "cache", "debug", "profile")}
    base["paths"]["hub_db"] = str(tmp_path / "hub.db")
    base["hub"] = {"server": True}
    cfg = Config(base)
    reloader = type("R", (), {"check": lambda self: None})()
    handler = make_handler(cfg, "admin", "pw", reloader, _Failures())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield cfg, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_client_roundtrip(server, tmp_path):
    cfg, url = server
    hub = HubDB(cfg.path("hub_db"))
    token = hub.add_worker("w1")
    hub.add_cities(["کرج"])
    hub.close()

    db = DB(tmp_path / "worker.db")
    for i in range(5):
        db.upsert_place(place(f"x{i}", city="کرج"))
    wcfg = {"hub": {"url": url, "token": token, "sync_batch": 2, "heartbeat_s": 3600}}
    client = HubClient(wcfg, db)
    assert client.claim() == {"city": "کرج", "paused": False}
    replies = [client.heartbeat({"phase": "scraping", "city": "کرج"}, force=True) for _ in range(3)]
    assert [r["accepted"] for r in replies] == [2, 2, 1]
    assert replies[0]["assignment"] == "کرج"
    assert client.heartbeat({"city": "کرج"}) is None               # throttled, nothing left
    hub = HubDB(cfg.path("hub_db"))
    assert hub.conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 5
    hub.close()
    client.complete("کرج")

    bad = HubClient({"hub": {"url": url, "token": "nope"}}, db)
    with pytest.raises(HubUnavailable, match="token rejected"):
        bad.claim()
    down = HubClient({"hub": {"url": "http://127.0.0.1:9", "token": token}}, db)
    with pytest.raises(HubUnavailable):
        down.claim()


def test_admin_api_needs_auth_and_csrf_header(server):
    import base64
    import json
    import urllib.error
    import urllib.request
    _, url = server
    auth = "Basic " + base64.b64encode(b"admin:pw").decode()

    def post(headers):
        req = urllib.request.Request(f"{url}/api/hub/admin", method="POST",
                                     data=json.dumps({"op": "add_cities", "names": ["یزد"]}).encode(),
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    assert post({})[0] == 401
    assert post({"Authorization": auth})[0] == 403
    assert post({"Authorization": auth, "X-Leadmap": "1"}) == (200, {"added": ["یزد"]})


# --- Service in hub mode (no browser, no network) -----------------------------------
class FakeHub:
    url = "fake"

    def __init__(self, cities):
        self.queue = list(cities)
        self.completed, self.failed = [], []

    def claim(self):
        return {"city": self.queue[0] if self.queue else None, "paused": False}

    def complete(self, city):
        self.completed.append(city)
        self.queue.remove(city)

    def fail(self, city, err):
        self.failed.append(city)
        self.queue.remove(city)

    def heartbeat(self, report, force=False):
        return {"assignment": self.queue[0] if self.queue else None, "paused": False}


def make_service(tmp_path, monkeypatch, hub):
    base = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    base["paths"] = {k: str(tmp_path / k) for k in ("db", "exports", "logs", "cache", "debug", "profile")}
    cfg = Config(base)
    cfg["hub"] = {}
    db = DB(tmp_path / "w.db")

    def fake_init(cfg, db, city):
        if city == "bad":
            raise RuntimeError("Nominatim returned nothing for 'bad'")
        db.insert_tiles([{"id": f"{city}:0:0", "city": city, "seq": 0, "row": 0, "col": 0, "lat": 1,
                          "lng": 1, "zoom": 16, "min_lat": 0, "min_lng": 0, "max_lat": 2, "max_lng": 2}])
        return {"poly": None, "tiles": 1, "added": 1}

    monkeypatch.setattr(service_mod, "init_city", fake_init)
    svc = service_mod.Service(cfg, db, None, hub=hub)
    sleeps = []
    monkeypatch.setattr(svc.pacer, "sleep", lambda s, reason="": sleeps.append(reason))
    return svc, db, sleeps


def test_service_hub_flow(tmp_path, monkeypatch):
    hub = FakeHub(["bad", "کرج", "قم"])
    svc, db, sleeps = make_service(tmp_path, monkeypatch, hub)
    assert not svc.hub_ready() and hub.failed == ["bad"]         # bad city reported, skipped
    assert svc.hub_ready() and svc.city == "کرج"
    assert db.get_state("hub_city") == "کرج"
    db.update_tile("کرج:0:0", status="done")
    svc.finish_city()
    assert hub.completed == ["کرج"] and svc.city is None
    assert svc.hub_ready() and svc.city == "قم"
    svc.hub_assignment = None                                     # admin released it in the panel
    hub.queue.clear()
    assert not svc.hub_ready() and svc.city is None
    assert "hub queue empty" in sleeps


def test_service_keeps_cached_city_when_hub_down(tmp_path, monkeypatch):
    class DownHub(FakeHub):
        def claim(self):
            raise HubUnavailable("down")
    svc, db, sleeps = make_service(tmp_path, monkeypatch, DownHub([]))
    assert not svc.hub_ready() and sleeps == ["hub unreachable"]  # nothing cached: wait
    db.set_state("hub_city", "کرج")
    assert svc.hub_ready() and svc.city == "کرج"                  # cached: keep scraping
