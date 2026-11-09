"""Connection, initialisation and the few write operations that need care."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1
DEFAULT_DB = Path("data") / "nidaros.sqlite"

# Fields of `line` that may be edited after import. Identity and ordering
# columns (id, source_id, batch_id, origin_ref, page_start, line_on_page) are
# deliberately excluded.
EDITABLE_FIELDS = frozenset({
    "page_end", "facsimile_ref", "signature_leaf", "row_type",
    "content_structure", "feast", "sub_section", "office", "office_printed",
    "nocturn", "slot", "genre", "sign", "fulltext_edition", "fulltext_ms",
    "fulltext_standardized", "rubric", "xref_raw", "xref_leaf",
    "incipit_only", "xref_resolved_page", "xref_resolved_text",
    "repetendum_note", "variant_notes", "indexing_notes",
})

DECISION_STATUSES = ("Confirm", "Reject", "Fixed")


class DatabaseError(Exception):
    """Raised for problems the user should see as a plain message."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an existing database with foreign keys enforced."""
    path = Path(path)
    if not path.exists():
        raise DatabaseError(f"No database at {path}. Run `python -m app init` first.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise DatabaseError(
            f"{path} has schema version {version}; this code expects {SCHEMA_VERSION}."
        )
    return conn


def init(path: str | Path) -> None:
    """Create a new database. Refuses to touch an existing file."""
    path = Path(path)
    if path.exists():
        raise DatabaseError(f"{path} already exists; not overwriting it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    except Exception:
        conn.close()
        path.unlink(missing_ok=True)
        raise
    conn.close()


def incipit(text: str | None, words: int = 6) -> str | None:
    """First `words` words of a text, as the parser computed the incipit column."""
    if not text:
        return None
    return " ".join(text.split()[:words])


def update_line(conn: sqlite3.Connection, line_id: int, field: str, value,
                note: str | None = None) -> bool:
    """Change one field of a line and record the change in edit_log.

    Returns False (and logs nothing) when the value is unchanged.
    """
    if field not in EDITABLE_FIELDS:
        raise DatabaseError(f"Field '{field}' cannot be edited.")
    row = conn.execute(f"SELECT {field} FROM line WHERE id = ?", (line_id,)).fetchone()
    if row is None:
        raise DatabaseError(f"No line with id {line_id}.")
    old = row[0]
    if old == value:
        return False
    with conn:
        conn.execute(f"UPDATE line SET {field} = ? WHERE id = ?", (value, line_id))
        conn.execute(
            "INSERT INTO edit_log (line_id, field, old_value, new_value, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (line_id, field, _as_text(old), _as_text(value), note),
        )
    return True


def record_decision(conn: sqlite3.Connection, line_id: int, status: str,
                    cid: str | None = None, note: str | None = None) -> int:
    """Append a Cantus ID decision for a line. Earlier decisions are kept."""
    if status not in DECISION_STATUSES:
        raise DatabaseError(f"Decision must be one of {', '.join(DECISION_STATUSES)}.")
    cid = (cid or "").strip() or None
    if status != "Reject" and cid is None:
        raise DatabaseError(f"A {status} decision needs a Cantus ID.")
    with conn:
        cur = conn.execute(
            "INSERT INTO decision (line_id, status, cid, note) VALUES (?, ?, ?, ?)",
            (line_id, status, cid, note),
        )
    return cur.lastrowid


def _as_text(value) -> str | None:
    return None if value is None else str(value)
