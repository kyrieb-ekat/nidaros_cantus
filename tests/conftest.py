from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.importer import REQUIRED_COLUMNS  # noqa: E402

PILOT = ROOT / "data" / "import" / "breviarium_andrew-lucy_pilot_master.xlsx"

FEAST = "IN FESTO SANCTI EXEMPLI"


def _row(seq, row_type, page, *, genre=None, position=None, import_seq=None,
         text=None, office="V", **extra):
    row = {c: None for c in REQUIRED_COLUMNS}
    row.update(
        master_seq=seq, row_type=row_type, page_start=page, feast=FEAST,
        content_structure="Proprium de sanctis", office=office, genre=genre,
        position=position, import_seq=import_seq,
        in_import="yes" if import_seq else None,
        fulltext_edition=text, fulltext_ms=text, fulltext_standardized=text,
        incipit=db.incipit(text) if genre else None,
    )
    row.update(extra)
    return row


def sample_rows() -> list[dict]:
    """A small feast covering every link type and the derived columns."""
    return [
        _row(1, "heading", 1, office=None),
        _row(2, "office_rubric", 1),
        _row(3, "antiphon", 1, genre="A", position="01", import_seq=1,
             text="Ecce vir prudens qui aedificavit domum suam",
             review_flags="NO_OFFICE; NEW_CODE"),
        _row(4, "responsory", 1, genre="R", position="02", import_seq=2,
             text="Iste sanctus pro lege dei sui certavit"),
        _row(5, "responsory_verse", 1, genre="V", position="03", import_seq=3,
             text="Fundatus enim erat supra firmam petram"),
        _row(6, "prayer", 1, genre="Or", position="04", text="Deus qui nos Per"),
        _row(7, "hymn", 2, genre="H", position="05", import_seq=4,
             text="Iste confessor domini sacratus"),
        _row(8, "hymn_verse", 2, genre="HV", position="06", import_seq=5,
             text="Qui pius prudens humilis pudicus"),
        _row(9, "antiphon", 2, genre="A", position="07", import_seq=6,
             text="Ecce vir prudens", incipit_only="yes",
             indexing_notes="full text at master_seq 3"),
    ]


def write_workbook(path: Path, rows: list[dict], spot: list[dict] | None = None) -> Path:
    wb = openpyxl.Workbook()
    readme = wb.active
    readme.title = "README"
    readme.append(["Review flags", None, None])
    readme.append(["Flag", "Meaning", "Rows"])
    readme.append(["NO_OFFICE", "No hour rubric precedes this item", None])
    readme.append([None, None, None])

    master = wb.create_sheet("Master")
    master.append(list(REQUIRED_COLUMNS))
    for row in rows:
        master.append([row[c] for c in REQUIRED_COLUMNS])

    sheet = wb.create_sheet("Spot-check")
    sheet.append(["master_seq", "why check this row", "status", "your note / fix"])
    for s in spot if spot is not None else [{"master_seq": 3, "why": "first antiphon"}]:
        sheet.append([s["master_seq"], s.get("why"), s.get("status", "To check"), s.get("note")])

    wb.save(path)
    return path


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.sqlite"
    db.init(path)
    connection = db.connect(path)
    connection.execute("INSERT INTO source (siglum, title) VALUES ('brev', 'Breviarium')")
    connection.commit()
    yield connection
    connection.close()
