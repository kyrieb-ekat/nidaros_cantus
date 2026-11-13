from __future__ import annotations

import csv

import pytest

from app import db
from app.export import IMPORT_COLUMNS, export_import_file
from app.importer import import_workbook

from conftest import FEAST, sample_rows, write_workbook


@pytest.fixture
def book(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    return conn


def lid(conn, seq):
    return conn.execute("SELECT id FROM line WHERE origin_ref = ?", (f"master_seq {seq}",)).fetchone()[0]


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_unmapped_feasts_stop_the_export(book, tmp_path):
    with pytest.raises(db.DatabaseError, match="no Cantus feast"):
        export_import_file(book, 1, tmp_path / "out.csv")
    report = export_import_file(book, 1, tmp_path / "out.csv", allow_unmapped=True)
    assert report.unmapped == [FEAST] and report.rows == 6


def test_export_values(book, tmp_path):
    conn = book
    with conn:
        conn.execute("INSERT INTO feast_map (source_id, feast, ci_feast, status) "
                     "VALUES (1, ?, 'Andreae', 'exact')", (FEAST,))
        conn.execute("UPDATE source SET cdb_source_id = 123456")
        conn.execute("UPDATE line SET rubric = 'Hymnus', extra_note = 'Feast heading supplied' "
                     "WHERE origin_ref = 'master_seq 7'")
    db.record_decision(conn, lid(conn, 3), "Confirm", "002588")
    db.record_decision(conn, lid(conn, 4), "Variant", parent_cid="003290")
    db.record_decision(conn, lid(conn, 5), "Fixed", "003290a")

    report = export_import_file(conn, 1, tmp_path / "out.csv")
    rows = read(tmp_path / "out.csv")
    assert list(rows[0]) == list(IMPORT_COLUMNS)
    got = [(r["folio"], r["sequence"], r["genre"], r["position"], r["cantus_id"]) for r in rows]
    assert got == [
        ("001", "1", "A", "p", "002588"),
        ("001", "2", "R", "", ""),            # Variant: no code yet
        ("001", "3", "V", "01", "003290a"),
        ("002", "1", "H", "", ""),
        ("002", "2", "HV", "02", ""),
        ("002", "3", "A", "M", ""),
    ]
    assert {r["feast"] for r in rows} == {"Andreae"}
    assert {r["source_id"] for r in rows} == {"123456"}
    assert rows[3]["extra"] == "Hymnus | Feast heading supplied"
    assert rows[5]["fulltext_standardized"] == "Ecce vir prudens*"
    assert rows[5]["indexing_notes"].endswith("edition p. 2")
    assert report.cantus_ids == 2
    assert report.decisions == {"Confirm": 1, "Variant": 1, "Fixed": 1, "(none)": 3}
    assert report.position_checks == 2      # single psalm antiphon; hymn stanza numbering
