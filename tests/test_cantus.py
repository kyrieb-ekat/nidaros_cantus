from __future__ import annotations

import pytest

from app.cantus import BASE_URL, CantusError, CantusIndex, evidence

from fake_ci import FakeCantusIndex, fixture


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def client(conn, fake, **kwargs):
    clock = Clock()
    return CantusIndex(conn, transport=fake, sleep=clock.sleep, clock=clock, **kwargs), clock


def test_bom_is_removed_and_responses_are_cached(conn):
    fake = FakeCantusIndex(chants={"006868": fixture("json-cid_006868.json")})
    ci, _ = client(conn, fake)
    first = ci.chant("006868")
    assert first["info"]["field_feast"] == "Andreae"
    assert ci.chant("006868") == first
    assert len(fake.urls) == 1 and ci.requests == 1
    body = conn.execute("SELECT body FROM api_cache").fetchone()[0]
    assert not body.startswith("\ufeff")


def test_refresh_fetches_each_url_once_per_run(conn):
    fake = FakeCantusIndex(corpus=[{"cid": "1", "fulltext": "Homo dei", "genre": "R"}])
    ci, _ = client(conn, fake)
    ci.text_search("homo")
    again, _ = client(conn, fake, refresh=True)
    again.text_search("homo")
    again.text_search("homo")
    assert len(fake.urls) == 2


def test_offline_uses_only_the_cache(conn):
    fake = FakeCantusIndex()
    ci, _ = client(conn, fake, offline=True)
    with pytest.raises(CantusError, match="offline"):
        ci.text_search("homo")
    assert fake.urls == []


def test_not_found_is_cached_as_none(conn):
    fake = FakeCantusIndex()
    ci, _ = client(conn, fake)
    assert ci.chant("999999") is None
    assert ci.chant("999999") is None
    assert len(fake.urls) == 1


def test_rate_limit(conn):
    fake = FakeCantusIndex()
    ci, clock = client(conn, fake, min_interval=1.0)
    ci.text_search("a b")
    ci.text_search("c d")
    ci.text_search("e f")
    assert clock.slept == [1.0, 1.0]


def test_server_errors_are_retried(conn):
    url = f"{BASE_URL}/json-text/homo"
    fake = FakeCantusIndex(corpus=[{"cid": "1", "fulltext": "Homo dei", "genre": "R"}],
                           failures={url: [503, 502]})
    ci, clock = client(conn, fake, min_interval=0)
    assert ci.text_search("homo") == [{"cid": "1", "fulltext": "Homo dei", "genre": "R"}]
    assert clock.slept == [5, 15]


def test_persistent_errors_give_up_and_cache_nothing(conn):
    url = f"{BASE_URL}/json-text/homo"
    fake = FakeCantusIndex(failures={url: [500, 500, 500]})
    ci, _ = client(conn, fake, min_interval=0)
    with pytest.raises(CantusError, match="gave up"):
        ci.text_search("homo")
    assert conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0] == 0


def test_query_is_url_encoded(conn):
    fake = FakeCantusIndex()
    ci, _ = client(conn, fake)
    ci.text_search("h%ec _est")
    assert fake.urls == [f"{BASE_URL}/json-text/h%25ec%20_est"]


def test_evidence_from_real_response():
    ev = evidence("006868", fixture("json-cid_006868.json"), {"andreae"}, "M")
    assert ev.ci_feast == "Andreae" and ev.ci_genre == "R" and ev.cao == "6868"
    assert ev.records == {"SEMM": 1, "CD": 1}
    assert sum(ev.reported.values()) == 139
    assert not ev.complete
    assert ev.same_feast == {"SEMM": 1, "CD": 1}
    assert ev.same_feast_office == {"SEMM": 1}


def test_evidence_without_feast_mapping():
    ev = evidence("006868", fixture("json-cid_006868.json"))
    assert ev.same_feast == {} and ev.total == 2
    assert evidence("x", None) is None
