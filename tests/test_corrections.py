"""split-feast, edit and flag: correcting lines after import."""

from __future__ import annotations

import pytest

from app import db
from app.__main__ import _line_ids, main
from app.importer import import_workbook

from conftest import FEAST, sample_rows, write_workbook


@pytest.fixture
def imported(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    return conn


def lid(conn, seq):
    return conn.execute("SELECT id FROM line WHERE origin_ref = ?", (f"master_seq {seq}",)).fetchone()[0]


def test_split_feast(imported):
    conn = imported
    moved, heading = db.split_feast(conn, lid(conn, 7), "[SANCTI EXEMPLI, post secundas vesperas]")
    assert moved == 3   # hymn, hymn verse, cue antiphon (lines 7-9)
    rows = conn.execute("SELECT origin_ref, row_type, feast, extra_note, position FROM line_export "
                        "ORDER BY page_start, line_on_page").fetchall()
    order = [r["origin_ref"] for r in rows]
    assert order.index("supplied by split-feast") == order.index("master_seq 7") - 1
    after = {r["origin_ref"]: r for r in rows}
    assert after["master_seq 6"]["feast"] == FEAST
    assert after["master_seq 7"]["feast"].startswith("[SANCTI EXEMPLI")
    assert after["master_seq 7"]["position"] == "01"          # positions restart
    assert after["master_seq 9"]["extra_note"] == db.SUPPLIED_NOTE
    assert after["supplied by split-feast"]["row_type"] == "heading"
    flags = conn.execute("SELECT code FROM flag WHERE line_id = ?", (heading,)).fetchall()
    assert [f[0] for f in flags] == ["FEAST_SUPPLIED"]
    logged = conn.execute("SELECT COUNT(*) FROM edit_log WHERE note = 'split-feast'").fetchone()[0]
    assert logged == 1 + 3 + 3        # heading, three feasts, three notes


def test_split_feast_keeps_other_notes(imported):
    conn = imported
    with conn:
        conn.execute("UPDATE line SET extra_note = 'own note' WHERE origin_ref = 'master_seq 8'")
    db.split_feast(conn, lid(conn, 7), "[X]", heading=False)
    note = conn.execute("SELECT extra_note FROM line WHERE origin_ref = 'master_seq 8'").fetchone()[0]
    assert note == "own note"
    assert conn.execute("SELECT COUNT(*) FROM line").fetchone()[0] == 9


def test_split_feast_checks(imported):
    conn = imported
    with pytest.raises(db.DatabaseError, match="already has"):
        db.split_feast(conn, lid(conn, 7), FEAST)
    with pytest.raises(db.DatabaseError, match="No line"):
        db.split_feast(conn, 999, "[X]")


def test_set_flag(imported):
    conn = imported
    a = lid(conn, 3)
    db.set_flag(conn, a, "NO_OFFICE", "clear", note="checked")
    assert tuple(conn.execute("SELECT status, note FROM flag WHERE line_id = ? AND code = 'NO_OFFICE'",
                              (a,)).fetchone()) == ("cleared", "checked")
    db.set_flag(conn, a, "NO_OFFICE", "reopen")
    db.set_flag(conn, a, "OFFICE_V2_INFERRED", "add")
    with pytest.raises(db.DatabaseError, match="already has"):
        db.set_flag(conn, a, "OFFICE_V2_INFERRED", "add")
    with pytest.raises(db.DatabaseError, match="Unknown flag"):
        db.set_flag(conn, a, "NOT_A_FLAG", "add")


def test_line_ids():
    assert _line_ids("110") == [110]
    assert _line_ids("110-112,115") == [110, 111, 112, 115]
    with pytest.raises(db.DatabaseError):
        _line_ids("12-10")


def test_edit_command(tmp_path, capsys):
    path = str(tmp_path / "e.sqlite")
    main(["--db", path, "init"])
    main(["--db", path, "add-source", "brev", "B"])
    main(["--db", path, "import", str(write_workbook(tmp_path / "a.xlsx", sample_rows())),
          "--source", "brev"])
    conn = db.connect(path)
    ids = [lid(conn, 4), lid(conn, 5)]
    conn.close()
    capsys.readouterr()
    assert main(["--db", path, "edit", f"{ids[0]}-{ids[1]}", "office", "V2", "--note", "after Lauds"]) == 0
    assert "changed 2 of 2 lines" in capsys.readouterr().out
    assert main(["--db", path, "edit", str(ids[0]), "nocturn", "x"]) == 1
    assert main(["--db", path, "edit", str(ids[0]), "office", "ZZ"]) == 1   # not a service code
    assert main(["--db", path, "edit", str(ids[0]), "extra_note", "--clear"]) == 0
