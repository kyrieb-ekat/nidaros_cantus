"""Connection, schema migrations, and the write operations that need care."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
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

DECISION_STATUSES = ("Confirm", "Reject", "Fixed", "Variant", "New CID")


class DatabaseError(Exception):
    """Raised for problems the user should see as a plain message."""


# --------------------------------------------------------------------------
# Schema versions

def migrations() -> list[tuple[int, Path]]:
    """Migration files in order, as (version, path). Files are named NNN_name.sql."""
    found = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = re.match(r"(\d+)_", path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise DatabaseError(f"Migration files are not numbered 1..n: {versions}")
    return found


SCHEMA_VERSION = len(migrations())


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _apply_pending(conn: sqlite3.Connection) -> list[int]:
    """Apply each pending migration in its own transaction."""
    applied = []
    for version, path in migrations():
        if version <= _version(conn):
            continue
        script = path.read_text(encoding="utf-8")
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an existing, up-to-date database with foreign keys enforced."""
    path = Path(path)
    if not path.exists():
        raise DatabaseError(f"No database at {path}. Run `python -m app init` first.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = _version(conn)
    if version != SCHEMA_VERSION:
        conn.close()
        if version < SCHEMA_VERSION:
            raise DatabaseError(
                f"{path} is at schema version {version}; this code needs {SCHEMA_VERSION}. "
                "Commit the database to git, then run `python -m app migrate`."
            )
        raise DatabaseError(
            f"{path} is at schema version {version}, newer than this code ({SCHEMA_VERSION})."
        )
    return conn


def init(path: str | Path) -> None:
    """Create a new database at the current schema version. Never overwrites."""
    path = Path(path)
    if path.exists():
        raise DatabaseError(f"{path} already exists; not overwriting it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        _apply_pending(conn)
    except Exception:
        conn.close()
        path.unlink(missing_ok=True)
        raise
    conn.close()


def migrate(path: str | Path) -> list[int]:
    """Bring an existing database up to the current schema version."""
    path = Path(path)
    if not path.exists():
        raise DatabaseError(f"No database at {path}.")
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        if _version(conn) > SCHEMA_VERSION:
            raise DatabaseError(f"{path} is newer than this code; not migrating.")
        applied = _apply_pending(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise DatabaseError("Foreign key check failed after migrating.")
        return applied
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Derived values and edits

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
                    cid: str | None = None, note: str | None = None,
                    parent_cid: str | None = None, reason: str | None = None) -> int:
    """Append a Cantus ID decision for a line. Earlier decisions are kept.

    Confirm and Fixed need a CID. Variant and New CID never take one: the code
    is assigned by Cantus Index later and recorded then as Fixed. Variant needs
    the probable parent CID.
    """
    if status not in DECISION_STATUSES:
        raise DatabaseError(f"Decision must be one of {', '.join(DECISION_STATUSES)}.")
    cid = (cid or "").strip() or None
    parent_cid = (parent_cid or "").strip() or None
    if status in ("Confirm", "Fixed") and cid is None:
        raise DatabaseError(f"A {status} decision needs a Cantus ID.")
    if status in ("Variant", "New CID") and cid is not None:
        raise DatabaseError(f"A {status} decision has no Cantus ID of its own; "
                            "record the code as Fixed once Cantus Index assigns it.")
    if status == "Variant" and parent_cid is None:
        raise DatabaseError("A Variant decision needs the probable parent CID.")
    if conn.execute("SELECT 1 FROM line WHERE id = ?", (line_id,)).fetchone() is None:
        raise DatabaseError(f"No line with id {line_id}.")
    with conn:
        cur = conn.execute(
            "INSERT INTO decision (line_id, status, cid, parent_cid, reason, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (line_id, status, cid, parent_cid, reason, note),
        )
    return cur.lastrowid


def _as_text(value) -> str | None:
    return None if value is None else str(value)
