from __future__ import annotations

import sqlite3

import pytest

from app import db


def columns(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name")]
    return {t: [tuple(c)[1:] for c in conn.execute(f"PRAGMA table_info('{t}')")] for t in tables}


def make_v1(path):
    conn = sqlite3.connect(path, isolation_level=None)
    script = (db.MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
    conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute("INSERT INTO source (siglum, title) VALUES ('brev', 'B')")
    conn.execute("INSERT INTO import_batch (source_id, file_name, file_sha256, page_first, "
                 "page_last, line_count) VALUES (1, 'f', 'x', 1, 1, 1)")
    conn.execute("INSERT INTO line (source_id, batch_id, page_start, line_on_page, row_type, genre) "
                 "VALUES (1, 1, 1, 10, 'antiphon', 'A')")
    conn.execute("INSERT INTO decision (line_id, status, cid, note) VALUES (1, 'Reject', '000001', 'x')")
    conn.execute("INSERT INTO decision (line_id, status, cid) VALUES (1, 'Confirm', '000002')")
    conn.close()


def test_v1_database_must_be_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    make_v1(path)
    with pytest.raises(db.DatabaseError, match="migrate"):
        db.connect(path)


def test_migration_keeps_data_and_matches_a_fresh_database(tmp_path):
    old, new = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    make_v1(old)
    assert db.migrate(old) == list(range(2, db.SCHEMA_VERSION + 1))
    assert db.migrate(old) == []
    db.init(new)

    a, b = db.connect(old), db.connect(new)
    assert columns(a) == columns(b)
    rows = a.execute("SELECT id, status, cid, note FROM decision ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "Reject", "000001", "x"), (2, "Confirm", "000002", None)]
    assert tuple(a.execute("SELECT status, cid FROM current_decision").fetchone()) == ("Confirm", "000002")
    assert a.execute("SELECT ci_genre FROM genre WHERE code = 'HV'").fetchone()[0] is None
    assert a.execute("SELECT COUNT(*) FROM flag_type WHERE code LIKE 'CI%'").fetchone()[0] == 4
    assert a.execute("SELECT COUNT(*) FROM flag_type WHERE code IN "
                     "('FEAST_SUPPLIED', 'OFFICE_V2_INFERRED')").fetchone()[0] == 2
    assert "extra_note" in [c[1] for c in a.execute("PRAGMA table_info(line)")]


def test_failed_migration_leaves_database_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    make_v1(path)
    broken = tmp_path / "migrations"
    broken.mkdir()
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        (broken / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (broken / "002_cantus_index.sql").write_text(
        (db.MIGRATIONS_DIR / "002_cantus_index.sql").read_text(encoding="utf-8")
        + "\nTHIS IS NOT SQL;\n", encoding="utf-8")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", broken)
    with pytest.raises(sqlite3.Error):
        db.migrate(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'feast_map'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 2


def test_decision_rules_in_the_schema(tmp_path):
    path = tmp_path / "n.sqlite"
    db.init(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO source (siglum, title) VALUES ('b', 'B')")
    conn.execute("INSERT INTO import_batch (source_id, file_name, file_sha256, page_first, "
                 "page_last, line_count) VALUES (1, 'f', 'x', 1, 1, 1)")
    conn.execute("INSERT INTO line (source_id, batch_id, page_start, line_on_page, row_type) "
                 "VALUES (1, 1, 1, 10, 'antiphon')")
    for status, cid, parent in [("Variant", None, None), ("Variant", "1.1", "1"),
                                ("New CID", "2", None), ("Confirm", None, None)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO decision (line_id, status, cid, parent_cid) VALUES (1, ?, ?, ?)",
                         (status, cid, parent))
