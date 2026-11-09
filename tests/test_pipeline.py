"""The parser's feast and Vespers rules, run end to end into the database."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

pytest.importorskip("bs4")
pytest.importorskip("lxml")

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE))

import build_xlsx  # noqa: E402
import parse_breviarium  # noqa: E402

from app.importer import import_workbook  # noqa: E402

from fake_ci import FIXTURES  # noqa: E402

VIGIL = "IN UIGILIA SANCTI ANDREE APOSTOLI"
FEAST = "[SANCTI ANDREE APOSTOLI]"
AFTER = "[SANCTI ANDREE APOSTOLI, post secundas vesperas]"


@pytest.fixture
def parsed(tmp_path):
    out = tmp_path / "out"
    parse_breviarium.main([str(FIXTURES / "synthetic_edition.html"), "--outdir", str(out)])
    with open(out / "master.csv", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    return out, rows


def test_vigil_feast_and_after_second_vespers(parsed):
    _, rows = parsed
    parts = [(r["row_type"], r["feast"], r["office"], r["review_flags"]) for r in rows
             if r["row_type"] in ("heading", "office_rubric")]
    assert parts == [
        ("heading", "", "", ""),
        ("heading", VIGIL, "", ""),
        ("heading", FEAST, "", "FEAST_SUPPLIED"),
        ("office_rubric", FEAST, "V", "OFFICE_V_UNSPEC"),
        ("office_rubric", FEAST, "L", ""),
        ("office_rubric", FEAST, "V2", "OFFICE_V2_INFERRED"),
        ("heading", AFTER, "", "FEAST_SUPPLIED"),
        ("office_rubric", AFTER, "M", ""),
        ("heading", "DE SANCTA BARBARA", "", ""),
        ("office_rubric", "DE SANCTA BARBARA", "V", "OFFICE_V_UNSPEC"),
    ]


def test_notes_positions_and_import_extra(parsed):
    out, rows = parsed
    chants = [r for r in rows if r["genre"]]
    vigil = [r for r in chants if r["feast"] == VIGIL]
    supplied = [r for r in chants if r["feast"] in (FEAST, AFTER)]
    assert len(vigil) == 1 and not vigil[0]["extra_note"]
    assert supplied and all(r["extra_note"] == parse_breviarium.SUPPLIED_NOTE for r in supplied)
    # positions restart in each part
    assert [r["position"] for r in chants if r["feast"] == AFTER] == ["01"]
    assert [(r["office"], r["position"]) for r in chants if r["feast"] == FEAST][:2] == \
        [("V", "01"), ("M", "01")]
    with open(out / "import.csv", encoding="utf-8") as fh:
        extra = {r["incipit"]: r["extra"] for r in csv.DictReader(fh)}
    assert extra["Hic est vir qui non est"] == ""
    assert extra["Domine Iesu Christe magister bone"] == parse_breviarium.SUPPLIED_NOTE


def test_workbook_imports_into_the_database(parsed, conn, tmp_path):
    out, _ = parsed
    workbook = tmp_path / "synthetic.xlsx"
    build_xlsx.build(str(out), str(workbook), "synthetic")
    report = import_workbook(conn, workbook, "brev")
    assert report.lines == 19
    notes = conn.execute("SELECT COUNT(*) FROM line WHERE extra_note IS NOT NULL").fetchone()[0]
    assert notes == 9   # 7 chants in the supplied parts + 2 supplied headings
    v2 = conn.execute("SELECT COUNT(*) FROM line_export WHERE office = 'V2' AND position IS NOT NULL"
                      ).fetchone()[0]
    assert v2 == 1
