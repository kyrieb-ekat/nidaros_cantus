"""Export a book's chants to the Cantus Database import CSV.

The columns and their order follow the chant-import template used by the
parser (pipeline/parse_breviarium.py, IMPORT_COLS). Values follow the Cantus
Database field descriptions (https://cantusdatabase.org/description/):

  folio       edition page with leading zeros (553 -> "553", 53 -> "053")
  sequence    order of the chant on its page, from 1
  feast       the Cantus feast mapped to the line's heading (feast_map)
  position    Cantus Database rules (app/positions.py)
  cantus_id   only from a current Confirm or Fixed decision
  extra       printed rubric, then the editorial note (extra_note)

Nothing is exported while a heading is unmapped, unless allow_unmapped is set.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .db import DatabaseError
from .positions import compute_positions

IMPORT_COLUMNS = (
    "source_id", "marginalia", "folio", "content structure", "sequence", "incipit", "feast",
    "office", "genre", "position", "cantus_id", "mode", "finalis", "differentia",
    "fulltext_standardized", "fulltext_ms", "fulltext_standardized_proofread",
    "fulltext_ms_proofread", "volpiano", "volpiano_proofread", "image_link", "melody_id",
    "cao_concordances", "addendum", "extra", "indexing_notes", "node_id",
)


@dataclass
class ExportReport:
    rows: int = 0
    cantus_ids: int = 0
    decisions: Counter = field(default_factory=Counter)
    unmapped: list[str] = field(default_factory=list)
    position_checks: int = 0
    open_flags: int = 0


def export_import_file(conn: sqlite3.Connection, source_id: int, path: str | Path,
                       allow_unmapped: bool = False) -> ExportReport:
    source = conn.execute("SELECT * FROM source WHERE id = ?", (source_id,)).fetchone()
    lines = conn.execute(
        """
        SELECT l.*, m.ci_feast, m.status AS feast_status,
               d.status AS decision, d.cid AS decided_cid
        FROM line_export AS l
        LEFT JOIN feast_map AS m ON m.source_id = l.source_id AND m.feast = l.feast
        LEFT JOIN current_decision AS d ON d.line_id = l.id
        WHERE l.source_id = ? AND l.in_import = 1
        ORDER BY l.page_start, l.line_on_page
        """, (source_id,)).fetchall()

    report = ExportReport()
    unmapped = sorted({ln["feast"] or "(no heading)" for ln in lines
                       if ln["feast_status"] is None or ln["feast_status"] == "unmapped"})
    report.unmapped = unmapped
    if unmapped and not allow_unmapped:
        raise DatabaseError(
            "These headings have no Cantus feast yet (map them with `map-feast`, "
            "or export with --allow-unmapped):\n  " + "\n  ".join(unmapped))

    positions = compute_positions(conn, source_id)
    on_page: dict[int, int] = defaultdict(int)
    out_rows = []
    for ln in lines:
        on_page[ln["page_start"]] += 1
        pos = positions.get(ln["id"])
        if pos and pos.check:
            report.position_checks += 1
        decision = ln["decision"] or "(none)"
        report.decisions[decision] += 1
        cid = ln["decided_cid"] if ln["decision"] in ("Confirm", "Fixed") else ""
        if cid:
            report.cantus_ids += 1
        out_rows.append({
            "source_id": source["cdb_source_id"] or "",
            "folio": f"{ln['page_start']:03d}",
            "content structure": ln["content_structure"] or "",
            "sequence": on_page[ln["page_start"]],
            "incipit": _incipit(ln["fulltext_standardized"]),
            "feast": ln["ci_feast"] or ln["feast"] or "",
            "office": ln["office"] or "",
            "genre": ln["genre"],
            "position": (pos.value if pos else None) or "",
            "cantus_id": cid or "",
            "fulltext_standardized": _standardized(ln),
            "fulltext_ms": ln["fulltext_ms"] or "",
            "extra": " | ".join(x for x in (ln["rubric"], ln["extra_note"]) if x),
            "indexing_notes": _indexing_notes(ln),
        })

    report.open_flags = conn.execute(
        "SELECT COUNT(DISTINCT f.line_id) FROM flag f JOIN line_export l ON l.id = f.line_id "
        "WHERE l.source_id = ? AND l.in_import = 1 AND f.status = 'open'", (source_id,)).fetchone()[0]

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=IMPORT_COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(out_rows)
    report.rows = len(out_rows)
    return report


def _incipit(text: str | None) -> str:
    return " ".join((text or "").split()[:6])


def _standardized(ln) -> str:
    text = ln["fulltext_standardized"] or ""
    if ln["incipit_only"] and text and not text.endswith("*"):
        text += "*"
    return text


def _indexing_notes(ln) -> str:
    """As the parser composes them: notes, print variants, repetendum, where the
    full text is, and the edition page."""
    notes = []
    if ln["indexing_notes"]:
        notes.append(ln["indexing_notes"])
    if ln["variant_notes"]:
        notes.append("print variants: " + ln["variant_notes"])
    if ln["repetendum_note"]:
        notes.append(ln["repetendum_note"])
    if ln["xref_resolved_text"]:
        notes.append(f"full text elsewhere (p. {ln['xref_resolved_page']})")
    pages = f"{ln['page_start']}" + (f"-{ln['page_end']}" if ln["page_end"] else "")
    notes.append(f"edition p. {pages}")
    return "; ".join(notes)
