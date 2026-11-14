from __future__ import annotations

import re

import pytest

pytest.importorskip("flask")

from app import db  # noqa: E402
from app.cantus import CantusIndex  # noqa: E402
from app.fetch import fetch_lines, target_lines  # noqa: E402
from app.importer import import_workbook  # noqa: E402
from app.web import create_app  # noqa: E402

from conftest import FEAST, sample_rows, write_workbook  # noqa: E402
from fake_ci import FakeCantusIndex, fixture  # noqa: E402
from test_fetch import CORPUS  # noqa: E402


def make_db(tmp_path):
    path = tmp_path / "w.sqlite"
    db.init(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO source (siglum, title) VALUES ('brev', 'Breviarium Nidrosiense')")
    conn.commit()
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    return path, conn


@pytest.fixture
def site(tmp_path):
    path, conn = make_db(tmp_path)
    fake = FakeCantusIndex(corpus=CORPUS, feasts=fixture("json-feasts_sample.json"))
    fetch_lines(conn, CantusIndex(conn, transport=fake, min_interval=0),
                target_lines(conn, 1, line_ids=[3, 4]))
    conn.close()
    app = create_app(path, transport=fake, min_interval=0)
    client = app.test_client()
    return client, page_token(client, "/line/3"), path


def page_token(client, url):
    """The token as the browser sends it: read from the forms on the page."""
    html = client.get(url).get_data(as_text=True)
    tokens = set(re.findall(r'name="token" value="([^"]*)"', html))
    assert len(tokens) == 1 and "" not in tokens, tokens
    return tokens.pop()


def current(path, line_id):
    conn = db.connect(path)
    row = conn.execute("SELECT status, cid, parent_cid FROM current_decision WHERE line_id = ?",
                       (line_id,)).fetchone()
    conn.close()
    return tuple(row) if row else None


def test_pages_render(site):
    client, _, _ = site
    r = client.get("/lines")
    assert r.status_code == 302 and r.headers["Location"].endswith("/line/3")
    page = client.get("/line/3").get_data(as_text=True)
    assert "Ecce vir prudens" in page and "Confirm 002588" in page and "Variant of 002588" in page
    assert client.get("/line/999").status_code == 404
    assert client.get("/feasts").status_code == 200
    assert "Andreae" in client.get("/feasts?q=andre&heading=" + FEAST).get_data(as_text=True)
    assert "No chants match" in client.get("/lines?status=Confirm").get_data(as_text=True)


def test_every_form_carries_the_token(site):
    client, token, _ = site
    for url in ("/line/3", "/line/4", "/feasts?q=andre&heading=" + FEAST):
        html = client.get(url).get_data(as_text=True)
        forms = re.findall(r'<form method="post".*?</form>', html, re.S)
        assert forms
        assert all(f'name="token" value="{token}"' in form for form in forms)


def test_changes_need_the_token(site):
    client, _, path = site
    r = client.post("/line/3/decide", data={"decision": "Reject"})
    assert r.status_code == 403 and "reload the page" in r.get_data(as_text=True)
    assert current(path, 3) is None


def test_confirm_and_advance_within_filters(site):
    client, token, path = site
    r = client.post("/line/3/decide", data={"token": token, "decision": "Confirm", "cid": "002588",
                                            "advance": "1", "office": "V"})
    assert current(path, 3) == ("Confirm", "002588", None)
    assert re.search(r"/line/4\?office=V$", r.headers["Location"])


def test_variant_new_cid_and_errors(site):
    client, token, path = site
    client.post("/line/4/decide", data={"token": token, "decision": "Variant", "parent": "003290",
                                        "reason": "phrase added"})
    assert current(path, 4) == ("Variant", None, "003290")
    r = client.post("/line/5/decide", data={"token": token, "decision": "Fixed"}, follow_redirects=True)
    assert "needs a Cantus ID" in r.get_data(as_text=True) and current(path, 5) is None
    client.post("/line/5/decide", data={"token": token, "decision": "New CID", "parent": "w00001"})
    assert current(path, 5) == ("New CID", None, "w00001")


def test_edit_flag_and_position(site):
    client, token, path = site
    client.post("/line/3/edit", data={"token": token, "field": "fulltext_standardized",
                                      "value": "Ecce vir prudens qui edificavit domum suam"})
    client.post("/line/3/edit", data={"token": token, "field": "position_override", "value": "1"})
    client.post("/line/3/flag", data={"token": token, "code": "NO_OFFICE", "action": "clear"})
    assert client.post("/line/3/edit", data={"token": token, "field": "page_start",
                                             "value": "9"}).status_code == 400
    conn = db.connect(path)
    line = conn.execute("SELECT fulltext_standardized, position_override FROM line WHERE id = 3").fetchone()
    assert tuple(line) == ("Ecce vir prudens qui edificavit domum suam", "1")
    assert conn.execute("SELECT status FROM flag WHERE line_id = 3 AND code = 'NO_OFFICE'"
                        ).fetchone()[0] == "cleared"
    assert conn.execute("SELECT COUNT(*) FROM edit_log WHERE line_id = 3").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM flag WHERE line_id = 3 AND code = 'POSITION_CHECK' "
                        "AND status = 'open'").fetchone()[0] == 0
    conn.close()


def test_fetch_from_the_screen(site):
    client, token, path = site
    r = client.post("/line/5/fetch", data={"token": token}, follow_redirects=True)
    assert "Looked up in Cantus Index" in r.get_data(as_text=True)
    conn = db.connect(path)
    assert conn.execute("SELECT cid FROM candidate WHERE line_id = 5 AND rank = 1").fetchone()[0] == "w00001"
    conn.close()


def test_map_feast(site):
    client, token, path = site
    r = client.post("/feasts/map", data={"token": token, "heading": FEAST, "ci": "Andreae",
                                         "status": "variant"})
    assert r.headers["Location"].endswith("/feasts")      # no headings left to map
    conn = db.connect(path)
    assert tuple(conn.execute("SELECT ci_feast, status FROM feast_map").fetchone()) == ("Andreae", "variant")
    conn.close()
    r = client.post("/feasts/map", data={"token": token, "heading": FEAST, "ci": "Nowhere",
                                         "status": "exact"}, follow_redirects=True)
    assert "No Cantus Index feast is called" in r.get_data(as_text=True)


def test_verse_related_to_confirmed_responsory_comes_first(tmp_path):
    path, conn = make_db(tmp_path)
    corpus = [
        # the unrelated verse is the closer text, so it is ranked first when stored
        {"cid": "000001a", "genre": "V", "fulltext": "Fundatus enim erat supra firmam petram"},
        {"cid": "003290b", "genre": "V", "fulltext": "Fundatus enim erat supra firmam petram et",
         "related": "003290"},
    ]
    fake = FakeCantusIndex(corpus=corpus)
    fetch_lines(conn, CantusIndex(conn, transport=fake, min_interval=0),
                target_lines(conn, 1, line_ids=[5]))
    db.record_decision(conn, 4, "Confirm", "003290")
    conn.close()
    page = create_app(path, transport=fake, min_interval=0).test_client().get("/line/5").get_data(as_text=True)
    assert "listed first" in page
    assert page.index("Confirm 003290b") < page.index("Confirm 000001a")
