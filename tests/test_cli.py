from __future__ import annotations

from app import db
from app.__main__ import main
from app.cantus import CantusIndex
from app.fetch import fetch_lines, target_lines

from conftest import FEAST, sample_rows, write_workbook
from fake_ci import FakeCantusIndex, fixture
from test_fetch import CORPUS


def setup(tmp_path, capsys):
    path = str(tmp_path / "cli.sqlite")
    assert main(["--db", path, "init"]) == 0
    assert main(["--db", path, "add-source", "brev", "Breviarium Nidrosiense"]) == 0
    workbook = write_workbook(tmp_path / "a.xlsx", sample_rows())
    assert main(["--db", path, "import", str(workbook), "--source", "brev"]) == 0
    conn = db.connect(path)
    fake = FakeCantusIndex(corpus=CORPUS, feasts=fixture("json-feasts_sample.json"))
    client = CantusIndex(conn, transport=fake, min_interval=0)
    fetch_lines(conn, client, target_lines(conn, 1))
    client.feasts()
    conn.close()
    capsys.readouterr()
    return path


def line_id(path, seq):
    conn = db.connect(path)
    return conn.execute("SELECT id FROM line WHERE origin_ref = ?", (f"master_seq {seq}",)).fetchone()[0]


def test_review_commands(tmp_path, capsys):
    path = setup(tmp_path, capsys)
    r = line_id(path, 4)

    assert main(["--db", path, "map-feast", FEAST, "--ci", "Andreae", "--variant"]) == 0
    assert main(["--db", path, "feasts"]) == 0
    out = capsys.readouterr().out
    assert "variant   Andreae" in out and "exact 0, variant 1" in out

    assert main(["--db", path, "feasts", "--search", "andre"]) == 0
    assert "Vig. Andreae" in capsys.readouterr().out

    assert main(["--db", path, "show", str(r)]) == 0
    out = capsys.readouterr().out
    assert "CI_VARIANT: probable parent 003290" in out
    assert "003290" in out and "suffix" in out and "concordances 1: CD 1" in out

    assert main(["--db", path, "decide", str(r), "variant", "--parent", "003290",
                 "--reason", "added phrase"]) == 0
    assert main(["--db", path, "decide", str(r), "variant"]) == 1
    assert "needs the probable parent" in capsys.readouterr().err

    out_csv = str(tmp_path / "requests.csv")
    assert main(["--db", path, "export-requests", out_csv]) == 0
    assert "Wrote 1 request" in capsys.readouterr().out

    assert main(["--db", path, "status"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "looked up in Cantus Index 6" in out
    assert "Variant 1" in out and "variant 1" in out


def test_fetch_offline_and_genre_map(tmp_path, capsys):
    path = setup(tmp_path, capsys)
    assert main(["--db", path, "genre-map", "HV", "--clear"]) == 0
    assert main(["--db", path, "fetch", "--offline", "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "6 lines, 0 requests" in out
    assert "genre HV is not mapped to Cantus Index; close matches had genre: HV (1)" in out

    assert main(["--db", path, "genre-map", "HV", "HV"]) == 0
    assert main(["--db", path, "fetch", "--offline", "--quiet"]) == 0
    assert "genre HV is not mapped" not in capsys.readouterr().out


def test_fetch_reports_failures_and_continues(tmp_path, capsys):
    path = setup(tmp_path, capsys)
    conn = db.connect(path)
    with conn:
        conn.execute("DELETE FROM api_cache")
    assert main(["--db", path, "fetch", "--offline", "--quiet"]) == 2
    assert "6 lines failed" in capsys.readouterr().out


def test_positions_source_and_export(tmp_path, capsys):
    path = setup(tmp_path, capsys)
    assert main(["--db", path, "positions", "--list"]) == 0
    out = capsys.readouterr().out
    assert "6 chants; 2 positions need a judgement" in out and "single psalm antiphon" in out
    assert main(["--db", path, "source", "brev", "--cdb-source-id", "123456"]) == 0
    out_csv = str(tmp_path / "import.csv")
    assert main(["--db", path, "export", out_csv]) == 1
    assert "no Cantus feast" in capsys.readouterr().err
    assert main(["--db", path, "map-feast", FEAST, "--ci", "Andreae"]) == 0
    assert main(["--db", path, "export", out_csv]) == 0
    out = capsys.readouterr().out
    assert "Wrote 6 chants" in out and "source_id is blank" not in out
