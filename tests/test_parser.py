from pathlib import Path

import pytest

from leadmap.parser import ParseError, parse_dom_card, parse_search_body

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name", ["search_plain.txt", "search_wrapped.txt"])
def test_parse_real_responses(name):
    places = parse_search_body((FIX / name).read_text(encoding="utf-8"))
    assert len(places) == 20
    for p in places:
        assert p["place_id"].startswith("ChIJ")
        assert p["name"]
        assert 35.7 < p["lat"] < 35.95 and 50.8 < p["lng"] < 51.1
        assert p["maps_url"].endswith(p["place_id"])
        if p["phone"]:
            assert p["phone"].startswith("0")
    assert any(p["is_mobile"] for p in places)
    assert any(p["phone"] and not p["is_mobile"] for p in places)


def test_known_record():
    places = parse_search_body((FIX / "search_plain.txt").read_text(encoding="utf-8"))
    p = next(p for p in places if p["place_id"] == "ChIJm7sFog16pAgTtpU4r16H1Ur")
    assert p["name"] == "املاک نمونه 21"   # fixtures are anonymized
    assert p["phone"] == "09122159010" and p["is_mobile"]
    assert p["rating"] == 5.0
    assert "بنگاه" in p["category"]


def test_garbage_raises():
    with pytest.raises(ParseError):
        parse_search_body("<html>nope</html>")


def test_no_results_block():
    assert parse_search_body(")]}'\n[[\"x\"]]") == []


def test_dom_card():
    href = ("https://www.google.com/maps/place/x/data=!4m7!3m6!1s0x3f8dbfbe596bdb35:0xa8b15a9b0f42f30"
            "!8m2!3d35.818658!4d50.9890533!16s%2Fg%2F11gdknnp81!19sChIJNdtrWb6_jT8RMC_0sKkViwo?hl=fa")
    p = parse_dom_card("املاک دی", href, "املاک دی\n4.5\nبنگاه مشاوره املاک · خیابان\n۰۹۱۲ ۱۲۳ ۴۵۶۷")
    assert p["place_id"] == "ChIJNdtrWb6_jT8RMC_0sKkViwo"
    assert (p["lat"], p["lng"]) == (35.818658, 50.9890533)
    assert p["phone"] == "09121234567" and p["is_mobile"]
    assert p["rating"] == 4.5
