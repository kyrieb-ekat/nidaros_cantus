"""Import a parser-generated master workbook into the database.

An import adds a new stretch of the book. It never modifies lines that are
already in the database: if any page in the workbook already has lines for
the same source, the import is refused. Everything happens in one transaction,
and the import is rolled back if the values the database derives (incipit,
position, in_import, import_seq) do not reproduce the workbook's own values.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from .db import DatabaseError, incipit

MASTER_SHEET = "Master"
README_SHEET = "README"
SPOT_SHEET = "Spot-check"

# Columns copied into `line` unchanged (apart from blank -> NULL).
LINE_TEXT_COLUMNS = (
    "facsimile_ref", "signature_leaf", "row_type", "content_structure", "feast",
    "sub_section", "office", "office_printed", "slot", "genre", "sign",
    "fulltext_edition", "fulltext_ms", "fulltext_standardized", "rubric",
    "xref_raw", "xref_leaf", "xref_resolved_text", "repetendum_note",
    "variant_notes", "indexing_notes",
)

# Columns the database derives; read only to verify the import.
DERIVED_COLUMNS = ("in_import", "import_seq", "position", "incipit")

CID_COLUMNS = ("cid_proposed", "cid_candidate_text", "cid_decision")

# Columns read when present (older workbooks lack them).
OPTIONAL_COLUMNS = ("extra_note",)

REQUIRED_COLUMNS = (
    ("master_seq", "page_start", "page_end", "nocturn", "incipit_only",
     "xref_resolved_page", "review_flags")
    + LINE_TEXT_COLUMNS + DERIVED_COLUMNS + CID_COLUMNS
)

LINE_STEP = 10  # gap between consecutive line_on_page values

FULL_TEXT_NOTE = re.compile(r"full text at master_seq (\d+)")
CID_TOKEN = re.compile(r"^\s*([^\s();]+)\s*(?:\(\s*([-+]?\d*\.?\d+)\s*\))?\s*$")


@dataclass
class ImportReport:
    batch_id: int = 0
    lines: int = 0
    import_rows: int = 0
    flags: int = 0
    new_flag_types: list[str] = field(default_factory=list)
    links: dict[str, int] = field(default_factory=dict)
    spot_checks: int = 0
    candidates: int = 0
    decisions: int = 0
    pages: tuple[int, int] = (0, 0)
    warnings: list[str] = field(default_factory=list)


class WorkbookImportError(DatabaseError):
    """The workbook cannot be imported as it stands."""


# --------------------------------------------------------------------------
# Reading the workbook

def _blank_to_none(value):
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _sheet_rows(ws) -> tuple[list[str], list[dict]]:
    rows = ws.iter_rows(values_only=True)
    header = [h.strip() if isinstance(h, str) else h for h in next(rows)]
    records = []
    for values in rows:
        if all(v is None for v in values):
            continue
        records.append({h: _blank_to_none(v) for h, v in zip(header, values) if h})
    return header, records


def _read_flag_legend(wb) -> dict[str, str]:
    """Flag codes and meanings from the README table headed 'Flag | Meaning'."""
    if README_SHEET not in wb.sheetnames:
        return {}
    legend, inside = {}, False
    for row in wb[README_SHEET].iter_rows(values_only=True):
        first = row[0] if row else None
        if not inside:
            inside = first == "Flag" and len(row) > 1 and row[1] == "Meaning"
            continue
        if not first:
            break
        legend[str(first).strip()] = row[1]
    return legend


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_int(value, column: str, seq) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise WorkbookImportError(f"master_seq {seq}: {column} is not a whole number ({value!r}).")


def _yes_flag(value, column: str, seq) -> int:
    if value is None:
        return 0
    if str(value).strip().lower() == "yes":
        return 1
    raise WorkbookImportError(f"master_seq {seq}: {column} must be 'yes' or blank ({value!r}).")


def _parse_proposals(text: str, seq) -> list[tuple[str, float | None]]:
    proposals = []
    for token in text.split(";"):
        if not token.strip():
            continue
        match = CID_TOKEN.match(token)
        if not match:
            raise WorkbookImportError(f"master_seq {seq}: cannot read cid_proposed entry {token.strip()!r}.")
        cid, score = match.groups()
        proposals.append((cid, float(score) if score else None))
    return proposals


# --------------------------------------------------------------------------
# Import

def import_workbook(conn: sqlite3.Connection, path: str | Path, siglum: str) -> ImportReport:
    path = Path(path)
    if not path.exists():
        raise WorkbookImportError(f"No such file: {path}")

    source = conn.execute("SELECT id FROM source WHERE siglum = ?", (siglum,)).fetchone()
    if source is None:
        raise WorkbookImportError(f"No source '{siglum}'. Add it first with `python -m app add-source`.")
    source_id = source["id"]

    file_hash = _sha256(path)
    if conn.execute("SELECT 1 FROM import_batch WHERE file_sha256 = ?", (file_hash,)).fetchone():
        raise WorkbookImportError(f"{path.name} has already been imported.")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if MASTER_SHEET not in wb.sheetnames:
            raise WorkbookImportError(f"{path.name} has no '{MASTER_SHEET}' sheet.")
        header, rows = _sheet_rows(wb[MASTER_SHEET])
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise WorkbookImportError(f"Master sheet is missing columns: {', '.join(missing)}.")
        legend = _read_flag_legend(wb)
        spot_rows = _sheet_rows(wb[SPOT_SHEET])[1] if SPOT_SHEET in wb.sheetnames else []
    finally:
        wb.close()

    if not rows:
        raise WorkbookImportError("The Master sheet has no rows.")
    _check_order(rows)

    pages = (rows[0]["page_start"], rows[-1]["page_start"])
    overlap = conn.execute(
        "SELECT COUNT(*) FROM line WHERE source_id = ? AND page_start BETWEEN ? AND ?",
        (source_id, *pages),
    ).fetchone()[0]
    if overlap:
        raise WorkbookImportError(
            f"Pages {pages[0]}-{pages[1]} already have {overlap} lines for '{siglum}'. "
            "Imports only add new pages; existing lines are never replaced."
        )

    if conn.in_transaction:
        raise WorkbookImportError("The connection has uncommitted changes; commit them before importing.")
    report = ImportReport(pages=pages)
    try:
        conn.execute("BEGIN")
        report.new_flag_types = _ensure_flag_types(conn, legend, rows)
        report.batch_id = conn.execute(
            "INSERT INTO import_batch (source_id, file_name, file_sha256, page_first, "
            "page_last, line_count) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, path.name, file_hash, pages[0], pages[1], len(rows)),
        ).lastrowid
        ids = _insert_lines(conn, source_id, report.batch_id, rows)
        report.lines = len(ids)
        report.flags = _insert_flags(conn, rows, ids)
        report.links = _insert_links(conn, rows, ids, report.warnings)
        report.spot_checks = _insert_spot_checks(conn, spot_rows, ids)
        report.candidates, report.decisions = _insert_cid_columns(conn, rows, ids)
        report.import_rows = _verify_derived(conn, rows, ids)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report


def _check_order(rows: list[dict]) -> None:
    previous_seq, previous_page = None, None
    seen = set()
    for row in rows:
        seq = _as_int(row["master_seq"], "master_seq", row["master_seq"])
        page = _as_int(row["page_start"], "page_start", seq)
        if seq is None or page is None:
            raise WorkbookImportError("Every Master row needs master_seq and page_start.")
        if seq in seen:
            raise WorkbookImportError(f"master_seq {seq} appears twice.")
        if previous_seq is not None and seq < previous_seq:
            raise WorkbookImportError(f"master_seq {seq} is out of order (after {previous_seq}).")
        if previous_page is not None and page < previous_page:
            raise WorkbookImportError(f"master_seq {seq}: page {page} comes after page {previous_page}.")
        if not row["row_type"]:
            raise WorkbookImportError(f"master_seq {seq} has no row_type.")
        seen.add(seq)
        row["master_seq"], row["page_start"] = seq, page
        previous_seq, previous_page = seq, page


def _ensure_flag_types(conn, legend: dict[str, str], rows: list[dict]) -> list[str]:
    for code, meaning in legend.items():
        conn.execute(
            "INSERT INTO flag_type (code, meaning) VALUES (?, ?) "
            "ON CONFLICT(code) DO UPDATE SET meaning = COALESCE(flag_type.meaning, excluded.meaning)",
            (code, meaning),
        )
    unknown = []
    for row in rows:
        for code in _flag_codes(row):
            if code not in legend and code not in unknown:
                exists = conn.execute("SELECT 1 FROM flag_type WHERE code = ?", (code,)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO flag_type (code) VALUES (?)", (code,))
                    unknown.append(code)
    return unknown


def _flag_codes(row: dict) -> list[str]:
    text = row.get("review_flags")
    return [c.strip() for c in text.split(";") if c.strip()] if text else []


def _insert_lines(conn, source_id: int, batch_id: int, rows: list[dict]) -> dict[int, int]:
    ids: dict[int, int] = {}
    page, n = None, 0
    columns = ("source_id", "batch_id", "origin_ref", "page_start", "line_on_page",
               "page_end", "nocturn", "incipit_only", "xref_resolved_page") \
        + LINE_TEXT_COLUMNS + OPTIONAL_COLUMNS
    sql = (f"INSERT INTO line ({', '.join(columns)}) "
           f"VALUES ({', '.join('?' for _ in columns)})")
    for row in rows:
        seq = row["master_seq"]
        n = n + 1 if row["page_start"] == page else 1
        page = row["page_start"]
        resolved = row["xref_resolved_page"]
        values = (
            source_id, batch_id, f"master_seq {seq}", page, n * LINE_STEP,
            _as_int(row["page_end"], "page_end", seq),
            _as_int(row["nocturn"], "nocturn", seq),
            _yes_flag(row["incipit_only"], "incipit_only", seq),
            None if resolved is None else str(resolved),
        ) + tuple(row[c] for c in LINE_TEXT_COLUMNS) + tuple(row.get(c) for c in OPTIONAL_COLUMNS)
        try:
            ids[seq] = conn.execute(sql, values).lastrowid
        except sqlite3.IntegrityError as exc:
            raise WorkbookImportError(
                f"master_seq {seq}: {exc} (genre {row['genre']!r}, office {row['office']!r})."
            ) from exc
    return ids


def _insert_flags(conn, rows: list[dict], ids: dict[int, int]) -> int:
    count = 0
    for row in rows:
        for ord_, code in enumerate(_flag_codes(row), start=1):
            conn.execute(
                "INSERT INTO flag (line_id, code, ord) VALUES (?, ?, ?)",
                (ids[row["master_seq"]], code, ord_),
            )
            count += 1
    return count


def _insert_links(conn, rows: list[dict], ids: dict[int, int], warnings: list[str]) -> dict[str, int]:
    counts = {"verse_of": 0, "stanza_of": 0, "refrain_of": 0, "full_text_in": 0}
    last_responsory: dict[str | None, int] = {}
    last_hymn: dict[str | None, int] = {}

    def link(from_seq: int, kind: str, to_seq: int) -> None:
        conn.execute(
            "INSERT INTO line_link (from_line, to_line, kind, origin) VALUES (?, ?, ?, 'import')",
            (ids[from_seq], ids[to_seq], kind),
        )
        counts[kind] += 1

    for row in rows:
        seq, kind, feast = row["master_seq"], row["row_type"], row["feast"]
        if kind == "responsory":
            last_responsory[feast] = seq
        elif kind == "hymn":
            last_hymn[feast] = seq
        elif kind == "responsory_verse":
            if feast in last_responsory:
                link(seq, "verse_of", last_responsory[feast])
            else:
                warnings.append(f"master_seq {seq}: responsory verse with no responsory before it.")
        elif kind in ("hymn_verse", "hymn_refrain"):
            target = "stanza_of" if kind == "hymn_verse" else "refrain_of"
            if feast in last_hymn:
                link(seq, target, last_hymn[feast])
            else:
                warnings.append(f"master_seq {seq}: {kind} with no hymn before it.")

        match = FULL_TEXT_NOTE.search(row["indexing_notes"] or "")
        if match:
            target_seq = int(match.group(1))
            if target_seq in ids:
                link(seq, "full_text_in", target_seq)
            else:
                warnings.append(f"master_seq {seq}: full text cited at master_seq {target_seq}, "
                                "which is not in this workbook.")
    return counts


def _insert_spot_checks(conn, spot_rows: list[dict], ids: dict[int, int]) -> int:
    count = 0
    for row in spot_rows:
        seq = row.get("master_seq")
        if seq is None:
            continue
        if seq not in ids:
            raise WorkbookImportError(f"Spot-check refers to master_seq {seq}, which is not in Master.")
        conn.execute(
            "INSERT INTO spot_check (line_id, reason, status, note) VALUES (?, ?, ?, ?)",
            (ids[seq], row.get("why check this row"), row.get("status") or "To check",
             row.get("your note / fix")),
        )
        count += 1
    return count


def _insert_cid_columns(conn, rows: list[dict], ids: dict[int, int]) -> tuple[int, int]:
    """Carry over anything enrich_cids.py or manual review left in the CID columns.

    Follows the workbook's README: to confirm, a single CID is left in
    cid_proposed; for Fixed, cid_proposed holds the CID typed by hand.
    """
    candidates = decisions = 0
    for row in rows:
        seq = row["master_seq"]
        proposed = _parse_proposals(row["cid_proposed"], seq) if row["cid_proposed"] else []
        status = row["cid_decision"]
        if status is not None:
            status = str(status).strip().capitalize()
            if status not in ("Confirm", "Reject", "Fixed"):
                raise WorkbookImportError(f"master_seq {seq}: unknown cid_decision {row['cid_decision']!r}.")
            if status in ("Confirm", "Fixed") and len(proposed) != 1:
                raise WorkbookImportError(f"master_seq {seq}: {status} needs exactly one CID in cid_proposed.")

        if status != "Fixed":
            for rank, (cid, score) in enumerate(proposed, start=1):
                conn.execute(
                    "INSERT INTO candidate (line_id, cid, score, rank, method, cand_text) "
                    "VALUES (?, ?, ?, ?, 'enrich_cids', ?)",
                    (ids[seq], cid, score, rank, row["cid_candidate_text"] if rank == 1 else None),
                )
                candidates += 1
        if status is not None:
            cid = proposed[0][0] if len(proposed) == 1 else None
            conn.execute(
                "INSERT INTO decision (line_id, status, cid, note) VALUES (?, ?, ?, ?)",
                (ids[seq], status, cid, "imported from workbook"),
            )
            decisions += 1
    return candidates, decisions


def _verify_derived(conn, rows: list[dict], ids: dict[int, int]) -> int:
    """Check that the database reproduces the workbook's derived columns."""
    batch_id = conn.execute("SELECT batch_id FROM line WHERE id = ?",
                            (next(iter(ids.values())),)).fetchone()[0]
    derived = {
        r["id"]: r for r in conn.execute(
            "SELECT id, position, in_import, import_seq, fulltext_standardized "
            "FROM line_export WHERE batch_id = ?", (batch_id,))
    }
    sheet_seq = [r["import_seq"] for r in rows if r["import_seq"] is not None]
    db_seq = [derived[ids[r["master_seq"]]]["import_seq"] for r in rows
              if derived[ids[r["master_seq"]]]["import_seq"] is not None]
    # import_seq continues across imports, so compare offsets from the first row.
    sheet_base = (min(sheet_seq) - 1) if sheet_seq else 0
    db_base = (min(db_seq) - 1) if db_seq else 0

    problems = []
    for row in rows:
        seq = row["master_seq"]
        d = derived[ids[seq]]
        expected = {
            "position": row["position"],
            "in_import": 1 if row["in_import"] == "yes" else 0,
            "import_seq": None if row["import_seq"] is None else row["import_seq"] - sheet_base,
            "incipit": row["incipit"],
        }
        actual = {
            "position": d["position"],
            "in_import": d["in_import"],
            "import_seq": None if d["import_seq"] is None else d["import_seq"] - db_base,
            "incipit": incipit(d["fulltext_standardized"]) if row["genre"] else None,
        }
        for key in expected:
            if expected[key] != actual[key]:
                problems.append(f"master_seq {seq}: {key} is {expected[key]!r} in the workbook, "
                                f"{actual[key]!r} when derived")
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise WorkbookImportError("Derived values do not match the workbook; nothing was imported.\n  "
                           + shown + more)
    return len(sheet_seq)
