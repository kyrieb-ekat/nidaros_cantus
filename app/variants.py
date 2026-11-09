"""Requests for new Cantus IDs, and recording the codes Cantus Index assigns.

Lines whose current decision is Variant or New CID are exported to a CSV for
the Cantus Index editors. The file has an empty assigned_cid column; once it
is filled in, importing the same file records each code as a Fixed decision.
No code is ever generated here.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .cantus import CantusError, CantusIndex
from .db import DatabaseError
from .fetch import comparison_text

EXPORT_COLUMNS = (
    "line_id", "page", "signature_leaf", "feast", "ci_feast", "feast_status", "office",
    "genre", "request", "parent_cid", "parent_text", "reason", "note",
    "standardized_text", "edition_text", "assigned_cid",
)
REQUEST_STATUSES = ("Variant", "New CID")


def export_requests(conn: sqlite3.Connection, source_id: int, path: str | Path) -> int:
    """Write one row per line whose current decision is Variant or New CID."""
    rows = conn.execute(
        """
        SELECT l.*, d.status AS request, d.parent_cid, d.reason, d.note AS decision_note,
               m.ci_feast, m.status AS feast_status
        FROM current_decision AS d
        JOIN line_export AS l ON l.id = d.line_id
        LEFT JOIN feast_map AS m ON m.source_id = l.source_id AND m.feast = l.feast
        WHERE l.source_id = ? AND d.status IN ('Variant', 'New CID')
        ORDER BY l.page_start, l.line_on_page
        """, (source_id,)).fetchall()
    cache = CantusIndex(conn, offline=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            text, _ = comparison_text(conn, row)
            writer.writerow({
                "line_id": row["id"],
                "page": row["page_start"],
                "signature_leaf": row["signature_leaf"],
                "feast": row["feast"],
                "ci_feast": row["ci_feast"],
                "feast_status": row["feast_status"] or "not mapped yet",
                "office": row["office"],
                "genre": row["genre"],
                "request": row["request"],
                "parent_cid": row["parent_cid"],
                "parent_text": _parent_text(conn, cache, row["id"], row["parent_cid"]),
                "reason": row["reason"],
                "note": row["decision_note"],
                "standardized_text": text,
                "edition_text": row["fulltext_edition"],
                "assigned_cid": "",
            })
    return len(rows)


def _parent_text(conn, cache: CantusIndex, line_id: int, cid: str | None) -> str | None:
    if not cid:
        return None
    try:
        data = cache.chant(cid)
    except CantusError:
        data = None
    if data and data.get("info", {}).get("field_full_text"):
        return data["info"]["field_full_text"]
    row = conn.execute("SELECT cand_text FROM candidate WHERE line_id = ? AND cid = ? "
                       "ORDER BY rank LIMIT 1", (line_id, cid)).fetchone()
    return row[0] if row else None


@dataclass
class CodeImport:
    recorded: int = 0
    already: int = 0
    blank: int = 0
    problems: list[str] = field(default_factory=list)


def import_codes(conn: sqlite3.Connection, path: str | Path) -> CodeImport:
    """Record assigned codes as Fixed decisions. All rows or none."""
    path = Path(path)
    if not path.exists():
        raise DatabaseError(f"No such file: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        code_column = "assigned_cid" if "assigned_cid" in fields else "cid" if "cid" in fields else None
        if "line_id" not in fields or code_column is None:
            raise DatabaseError("The file needs a line_id column and an assigned_cid (or cid) column.")
        rows = list(reader)

    report = CodeImport()
    to_record = []
    for number, row in enumerate(rows, start=2):
        code = (row.get(code_column) or "").strip()
        if not code:
            report.blank += 1
            continue
        try:
            line_id = int(row["line_id"])
        except (TypeError, ValueError):
            report.problems.append(f"row {number}: line_id {row['line_id']!r} is not a number")
            continue
        current = conn.execute("SELECT status, cid, parent_cid FROM current_decision "
                               "WHERE line_id = ?", (line_id,)).fetchone()
        if current is None:
            report.problems.append(f"row {number}: line {line_id} has no decision")
        elif current["status"] == "Fixed" and current["cid"] == code:
            report.already += 1
        elif current["status"] not in REQUEST_STATUSES:
            report.problems.append(f"row {number}: line {line_id} is {current['status']}"
                                   f"{' ' + current['cid'] if current['cid'] else ''}, "
                                   "not waiting for a code")
        else:
            to_record.append((line_id, code, current["parent_cid"], current["status"]))

    if report.problems:
        return report
    with conn:
        for line_id, code, parent, request in to_record:
            conn.execute(
                "INSERT INTO decision (line_id, status, cid, parent_cid, note) "
                "VALUES (?, 'Fixed', ?, ?, ?)",
                (line_id, code, parent, f"assigned by Cantus Index ({request} request)"),
            )
    report.recorded = len(to_record)
    return report
