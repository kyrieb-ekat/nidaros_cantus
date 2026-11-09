from __future__ import annotations

import csv

import pytest

from app import db
from app.cantus import CantusIndex
from app.fetch import fetch_lines, target_lines
from app.importer import import_workbook
from app.variants import export_requests, import_codes

from conftest import sample_rows, write_workbook
from fake_ci import FakeCantusIndex
from test_fetch import CORPUS


def line_id(conn, seq):
    return conn.execute("SELECT id FROM line WHERE origin_ref = ?", (f"master_seq {seq}",)).fetchone()[0]


@pytest.fixture
def fetched(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    client = CantusIndex(conn, transport=FakeCantusIndex(corpus=CORPUS), min_interval=0)
    fetch_lines(conn, client, target_lines(conn, 1))
    return conn


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_decisions_need_the_right_fields(fetched):
    conn, r = fetched, line_id(fetched, 4)
    with pytest.raises(db.DatabaseError, match="parent"):
        db.record_decision(conn, r, "Variant")
    with pytest.raises(db.DatabaseError, match="no Cantus ID of its own"):
        db.record_decision(conn, r, "Variant", "003290.1", parent_cid="003290")
    with pytest.raises(db.DatabaseError, match="No line"):
        db.record_decision(conn, 9999, "Reject")
    db.record_decision(conn, r, "New CID", reason="genre differs")


def test_export_and_import_round_trip(fetched, tmp_path):
    conn = fetched
    r, v = line_id(conn, 4), line_id(conn, 5)
    db.record_decision(conn, r, "Variant", parent_cid="003290",
                       reason="9-word phrase in the Cantus Index text only, at the end")
    db.record_decision(conn, v, "New CID", parent_cid="w00001", reason="text is a versicle there")
    db.record_decision(conn, line_id(conn, 3), "Confirm", "002588")

    path = tmp_path / "requests.csv"
    assert export_requests(conn, 1, path) == 2
    rows = read(path)
    assert [(x["line_id"], x["request"], x["parent_cid"]) for x in rows] == \
        [(str(r), "Variant", "003290"), (str(v), "New CID", "w00001")]
    assert rows[0]["parent_text"].startswith("Iste sanctus pro lege dei sui certavit usque")
    assert rows[0]["standardized_text"] == "Iste sanctus pro lege dei sui certavit"
    assert rows[0]["feast_status"] == "not mapped yet"
    assert rows[0]["assigned_cid"] == ""

    # the editor fills in one code; the other is still pending
    rows[0]["assigned_cid"] = "003290.1"
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = import_codes(conn, path)
    assert (report.recorded, report.already, report.blank, report.problems) == (1, 0, 1, [])
    current = conn.execute("SELECT status, cid, parent_cid FROM current_decision WHERE line_id = ?",
                           (r,)).fetchone()
    assert tuple(current) == ("Fixed", "003290.1", "003290")
    history = [x[0] for x in conn.execute("SELECT status FROM decision WHERE line_id = ? ORDER BY id", (r,))]
    assert history == ["Variant", "Fixed"]

    again = import_codes(conn, path)
    assert (again.recorded, again.already) == (0, 1)
    assert export_requests(conn, 1, tmp_path / "next.csv") == 1


def test_import_refuses_lines_not_waiting_for_a_code(fetched, tmp_path):
    conn = fetched
    a = line_id(conn, 3)
    db.record_decision(conn, a, "Confirm", "002588")
    w = line_id(conn, 4)
    db.record_decision(conn, w, "Variant", parent_cid="003290")
    path = tmp_path / "codes.csv"
    path.write_text(f"line_id,cid\n{w},003290.1\n{a},002588.2\n", encoding="utf-8")
    report = import_codes(conn, path)
    assert report.recorded == 0
    assert report.problems and "is Confirm 002588" in report.problems[0]
    assert conn.execute("SELECT status FROM current_decision WHERE line_id = ?", (w,)).fetchone()[0] \
        == "Variant"
