from __future__ import annotations

import pytest

from app.cantus import CantusIndex
from app.db import DatabaseError
from app.feasts import (ci_feasts, feast_headings, find_feast, map_feast,
                        mapped_feast_names, search_feasts)
from app.importer import import_workbook

from conftest import FEAST, sample_rows, write_workbook
from fake_ci import FakeCantusIndex, fixture


@pytest.fixture
def feasts(conn):
    fake = FakeCantusIndex(feasts=fixture("json-feasts_sample.json"))
    return ci_feasts(CantusIndex(conn, transport=fake, min_interval=0))


def test_search_uses_names_alternatives_and_descriptions(feasts):
    assert [f.name for f in search_feasts(feasts, "andre")] == ["Andreae", "Vig. Andreae"]
    assert [f.name for f in search_feasts(feasts, "apostoli")] == ["Andreae"]
    assert [f.name for f in search_feasts(feasts, "virgin")] == ["Luciae"]


def test_find_feast(feasts):
    assert find_feast(feasts, "andreae apostoli").name == "Andreae"
    with pytest.raises(DatabaseError, match="No Cantus Index feast"):
        find_feast(feasts, "Andrew")


def test_map_feast(conn, tmp_path, feasts):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    andreae = find_feast(feasts, "Andreae")

    map_feast(conn, 1, FEAST, "variant", andreae, note="closest available")
    row = feast_headings(conn, 1)[0]
    assert (row["feast"], row["ci_feast"], row["ci_feastcode"], row["status"]) == \
        (FEAST, "Andreae", "14113000", "variant")
    assert mapped_feast_names(conn, 1, FEAST, feasts) == {"Andreae", "Andreae Apostoli"}

    map_feast(conn, 1, FEAST, "unmapped")
    assert feast_headings(conn, 1)[0]["status"] == "unmapped"
    assert mapped_feast_names(conn, 1, FEAST) == set()


def test_map_feast_checks(conn, tmp_path, feasts):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    andreae = find_feast(feasts, "Andreae")
    with pytest.raises(DatabaseError, match="No lines"):
        map_feast(conn, 1, "IN FESTO NULLO", "exact", andreae)
    with pytest.raises(DatabaseError):
        map_feast(conn, 1, FEAST, "exact", None)
    with pytest.raises(DatabaseError):
        map_feast(conn, 1, FEAST, "unmapped", andreae)
