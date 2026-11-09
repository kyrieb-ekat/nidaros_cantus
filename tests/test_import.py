from __future__ import annotations

import shutil

import pytest

from app import db
from app.importer import WorkbookImportError, import_workbook

from conftest import PILOT, sample_rows, write_workbook


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


# --- initialisation --------------------------------------------------------

def test_init_creates_schema_and_seeds(tmp_path):
    path = tmp_path / "new.sqlite"
    db.init(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert count(conn, "SELECT COUNT(*) FROM genre WHERE imported = 1") == 7
    assert count(conn, "SELECT label FROM office WHERE code = 'V2'") == "Second Vespers"


def test_init_refuses_existing_file(tmp_path):
    path = tmp_path / "new.sqlite"
    db.init(path)
    with pytest.raises(db.DatabaseError):
        db.init(path)


def test_connect_requires_existing_file(tmp_path):
    with pytest.raises(db.DatabaseError):
        db.connect(tmp_path / "missing.sqlite")


# --- importing a synthetic workbook -------------------------------------------

def test_import_lines_links_flags(conn, tmp_path):
    report = import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")

    assert report.lines == 9
    assert report.import_rows == 6
    assert report.links == {"verse_of": 1, "stanza_of": 1, "refrain_of": 0, "full_text_in": 1}
    assert report.new_flag_types == ["NEW_CODE"]
    assert report.spot_checks == 1

    # line_on_page restarts on each page, in steps of 10
    assert [tuple(r) for r in conn.execute(
        "SELECT page_start, line_on_page FROM line ORDER BY page_start, line_on_page")] == [
        (1, 10), (1, 20), (1, 30), (1, 40), (1, 50), (1, 60), (2, 10), (2, 20), (2, 30)]

    flags = conn.execute("SELECT code, ord FROM flag ORDER BY ord").fetchall()
    assert [tuple(f) for f in flags] == [("NO_OFFICE", 1), ("NEW_CODE", 2)]
    assert count(conn, "SELECT meaning FROM flag_type WHERE code = 'NO_OFFICE'") \
        == "No hour rubric precedes this item"


def test_link_targets(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    links = {
        r["kind"]: (r["f"], r["t"]) for r in conn.execute(
            "SELECT k.kind, a.origin_ref AS f, b.origin_ref AS t FROM line_link k "
            "JOIN line a ON a.id = k.from_line JOIN line b ON b.id = k.to_line")
    }
    assert links["verse_of"] == ("master_seq 5", "master_seq 4")
    assert links["stanza_of"] == ("master_seq 8", "master_seq 7")
    assert links["full_text_in"] == ("master_seq 9", "master_seq 3")


def test_derived_mismatch_rolls_back(conn, tmp_path):
    rows = sample_rows()
    rows[3]["position"] = "09"  # the responsory, wrongly numbered
    with pytest.raises(WorkbookImportError, match="position"):
        import_workbook(conn, write_workbook(tmp_path / "bad.xlsx", rows), "brev")
    assert count(conn, "SELECT COUNT(*) FROM line") == 0
    assert count(conn, "SELECT COUNT(*) FROM import_batch") == 0
    assert count(conn, "SELECT COUNT(*) FROM flag_type WHERE code = 'NO_OFFICE'") == 0


def test_same_file_twice_is_refused(conn, tmp_path):
    path = write_workbook(tmp_path / "a.xlsx", sample_rows())
    import_workbook(conn, path, "brev")
    with pytest.raises(WorkbookImportError, match="already been imported"):
        import_workbook(conn, path, "brev")


def test_overlapping_pages_are_refused(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    rows = sample_rows()
    rows[0]["indexing_notes"] = "edited"  # different file, same pages
    with pytest.raises(WorkbookImportError, match="never replaced"):
        import_workbook(conn, write_workbook(tmp_path / "b.xlsx", rows), "brev")
    assert count(conn, "SELECT COUNT(*) FROM line") == 9


def test_later_pages_continue_import_seq(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    rows = sample_rows()
    for row in rows:
        row["page_start"] += 10
        row["feast"] = "IN FESTO ALTERIUS"
    import_workbook(conn, write_workbook(tmp_path / "b.xlsx", rows), "brev")
    seqs = [r[0] for r in conn.execute(
        "SELECT import_seq FROM line_export WHERE in_import = 1 ORDER BY page_start, line_on_page")]
    assert seqs == list(range(1, 13))


def test_unknown_source(conn, tmp_path):
    with pytest.raises(WorkbookImportError, match="No source"):
        import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "missal")


def test_unknown_genre_is_reported(conn, tmp_path):
    rows = sample_rows()
    rows[2]["genre"] = "XX"
    with pytest.raises(WorkbookImportError, match="master_seq 3"):
        import_workbook(conn, write_workbook(tmp_path / "a.xlsx", rows), "brev")
    assert count(conn, "SELECT COUNT(*) FROM line") == 0


def test_cid_columns_become_candidates_and_decisions(conn, tmp_path):
    rows = sample_rows()
    rows[2].update(cid_proposed="002588 (0.94); 002589 (0.71)", cid_candidate_text="Ecce vir prudens")
    rows[3].update(cid_proposed="006960", cid_decision="Confirm")
    rows[4].update(cid_proposed="006960a", cid_decision="fixed")
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", rows), "brev")

    cands = conn.execute(
        "SELECT l.origin_ref, c.cid, c.score, c.rank, c.cand_text FROM candidate c "
        "JOIN line l ON l.id = c.line_id ORDER BY l.id, c.rank").fetchall()
    assert [tuple(c) for c in cands] == [
        ("master_seq 3", "002588", 0.94, 1, "Ecce vir prudens"),
        ("master_seq 3", "002589", 0.71, 2, None),
        ("master_seq 4", "006960", None, 1, None),
    ]
    decisions = conn.execute(
        "SELECT l.origin_ref, d.status, d.cid FROM current_decision d "
        "JOIN line l ON l.id = d.line_id ORDER BY l.id").fetchall()
    assert [tuple(d) for d in decisions] == [
        ("master_seq 4", "Confirm", "006960"),
        ("master_seq 5", "Fixed", "006960a"),
    ]


def test_confirm_needs_a_single_cid(conn, tmp_path):
    rows = sample_rows()
    rows[2].update(cid_proposed="002588 (0.94); 002589 (0.71)", cid_decision="Confirm")
    with pytest.raises(WorkbookImportError, match="exactly one CID"):
        import_workbook(conn, write_workbook(tmp_path / "a.xlsx", rows), "brev")


# --- edits and decisions -------------------------------------------------------

def test_update_line_logs_changes(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    line_id = count(conn, "SELECT id FROM line WHERE origin_ref = 'master_seq 3'")

    assert db.update_line(conn, line_id, "office", "V2", note="checked facsimile")
    assert not db.update_line(conn, line_id, "office", "V2")
    log = conn.execute("SELECT field, old_value, new_value, note FROM edit_log").fetchall()
    assert [tuple(r) for r in log] == [("office", "V", "V2", "checked facsimile")]


def test_update_line_rejects_bad_values(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    line_id = count(conn, "SELECT id FROM line WHERE origin_ref = 'master_seq 3'")
    with pytest.raises(db.DatabaseError):
        db.update_line(conn, line_id, "page_start", 99)
    with pytest.raises(db.DatabaseError, match="not a known genre code"):
        db.update_line(conn, line_id, "genre", "XX")
    assert count(conn, "SELECT COUNT(*) FROM edit_log") == 0
    assert count(conn, "SELECT genre FROM line WHERE id = ?", line_id) == "A"


def test_derived_incipit_follows_edits(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    line_id = count(conn, "SELECT id FROM line WHERE origin_ref = 'master_seq 3'")
    db.update_line(conn, line_id, "fulltext_standardized", "Ecce vir prudens qui edificavit domum")
    text = count(conn, "SELECT fulltext_standardized FROM line WHERE id = ?", line_id)
    assert db.incipit(text) == "Ecce vir prudens qui edificavit domum"


def test_decisions_are_appended(conn, tmp_path):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    line_id = count(conn, "SELECT id FROM line WHERE origin_ref = 'master_seq 3'")
    with pytest.raises(db.DatabaseError):
        db.record_decision(conn, line_id, "Confirm")
    db.record_decision(conn, line_id, "Confirm", "002588")
    db.record_decision(conn, line_id, "Fixed", "002589", note="different verse")
    assert count(conn, "SELECT COUNT(*) FROM decision") == 2
    current = conn.execute("SELECT status, cid FROM current_decision").fetchall()
    assert [tuple(r) for r in current] == [("Fixed", "002589")]


# --- the pilot workbook ----------------------------------------------------------

@pytest.mark.skipif(not PILOT.exists(), reason="pilot workbook not present")
def test_pilot_workbook(conn, tmp_path):
    path = shutil.copy(PILOT, tmp_path / PILOT.name)
    report = import_workbook(conn, path, "brev")
    assert (report.lines, report.import_rows) == (565, 319)
    assert report.pages == (553, 577)
    assert report.links == {"verse_of": 51, "stanza_of": 15, "refrain_of": 6, "full_text_in": 3}
    assert report.flags == 97
    assert report.new_flag_types == []
    assert report.spot_checks == 35
