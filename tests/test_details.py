import threading

from leadmap.db import DB
from leadmap.pacing import Pacer
from leadmap.scrape import TileScraper, TransientError

CFG = {"pacing": {"timezone": "Asia/Tehran", "detail_delay_s": [0, 0], "max_details_per_hour": 100},
       "grid": {"pad_ratio": 0.25}}


def make(tmp_path, results):
    db = DB(tmp_path / "t.db")
    s = TileScraper(CFG, db, None, Pacer(CFG, db, threading.Event()))
    calls = iter(results)

    def fake_open(url):
        r = next(calls)
        if isinstance(r, Exception):
            raise r
        return r
    s.open_detail = fake_open
    places = []
    for i in range(len(results)):
        p = {"place_id": f"p{i}", "name": f"n{i}", "maps_url": f"u{i}", "phone": None}
        db.upsert_place(p)
        places.append(p)
    return db, s, places


def test_detail_failure_does_not_fail_tile(tmp_path):
    db, s, places = make(tmp_path, [TransientError("timeout"), "+98 912 111 2233"])
    assert s.fill_missing_phones(places) == 2
    assert db.needs_details("p0")            # failed one stays unchecked → retried later
    assert not db.needs_details("p1")
    assert places[1]["phone"] == "09121112233" and places[1]["is_mobile"]


def test_three_failures_stop_details(tmp_path):
    db, s, places = make(tmp_path, [TransientError("t")] * 3 + ["0912 000 0000"])
    assert s.fill_missing_phones(places) == 3
    assert db.needs_details("p3")
