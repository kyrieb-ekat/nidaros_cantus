from __future__ import annotations

from app.cantus import CantusIndex
from app.fetch import fetch_lines, line_flags, search, target_lines
from app.importer import import_workbook

from conftest import sample_rows, write_workbook
from fake_ci import FakeCantusIndex

CORPUS = [
    # antiphon: Cantus Index adds two words at the end -> minor, no flag
    {"cid": "002588", "fulltext": "Ecce vir prudens qui aedificavit domum suam supra petram",
     "genre": "A"},
    # responsory: Cantus Index has a long added phrase -> variant of 003290
    {"cid": "003290", "fulltext": "Iste sanctus pro lege dei sui certavit usque ad mortem "
                                  "et a verbis impiorum non timuit", "genre": "R"},
    # the verse's text exists only as a versicle -> new CID needed
    {"cid": "w00001", "fulltext": "Fundatus enim erat supra firmam petram", "genre": "W"},
    # the hymn's record is only an incipit -> incomplete
    {"cid": "008314", "fulltext": "Iste confessor*", "genre": "H"},
    # hymn verse: its own genre (HV) in Cantus Index
    {"cid": "008314a", "fulltext": "Qui pius prudens humilis pudicus sobriam duxit",
     "genre": "HV"},
]


def setup(conn, tmp_path, corpus=CORPUS):
    import_workbook(conn, write_workbook(tmp_path / "a.xlsx", sample_rows()), "brev")
    return FakeCantusIndex(corpus=corpus)


def run(conn, fake, **kwargs):
    client = CantusIndex(conn, transport=fake, min_interval=0, sleep=lambda s: None, **kwargs)
    lines = target_lines(conn, 1)
    return fetch_lines(conn, client, lines), client


def flags_by_line(conn):
    rows = conn.execute(
        "SELECT l.origin_ref, f.code, f.status, f.note FROM flag f JOIN line l ON l.id = f.line_id "
        "WHERE f.code LIKE 'CI!_%' ESCAPE '!' ORDER BY l.id, f.code").fetchall()
    return {(r["origin_ref"], r["code"]): (r["status"], r["note"]) for r in rows}


def test_fetch_flags_and_candidates(conn, tmp_path):
    fake = setup(conn, tmp_path)
    summary, client = run(conn, fake)

    assert summary.lines == 6 and not summary.errors
    flags = flags_by_line(conn)
    assert set(flags) == {
        ("master_seq 4", "CI_VARIANT"),
        ("master_seq 5", "CI_GENRE_NEW_CID"),
        ("master_seq 7", "CI_TEXT_INCOMPLETE"),
    }
    assert flags[("master_seq 4", "CI_VARIANT")][1].startswith("probable parent 003290:")
    assert "w00001 (genre W; this line is V)" in flags[("master_seq 5", "CI_GENRE_NEW_CID")][1]
    assert summary.unconfirmed_genres == {}

    best = conn.execute(
        "SELECT l.origin_ref, c.cid, c.text_class, c.genre_match FROM candidate c "
        "JOIN line l ON l.id = c.line_id WHERE c.rank = 1 ORDER BY l.id").fetchall()
    assert [tuple(r) for r in best] == [
        ("master_seq 3", "002588", "minor", "match"),
        ("master_seq 4", "003290", "suffix", "match"),
        ("master_seq 5", "w00001", "identical", "mismatch"),
        ("master_seq 7", "008314", "incomplete", "match"),
        ("master_seq 8", "008314a", "minor", "match"),
        ("master_seq 9", "002588", "minor", "match"),   # cue compared via its full text
    ]
    # full records were fetched for the leading candidates
    assert any("json-cid/003290" in u for u in fake.urls)
    assert any("json-cid/w00001" in u for u in fake.urls)


def test_rerun_uses_cache_and_keeps_cleared_flags(conn, tmp_path):
    fake = setup(conn, tmp_path)
    run(conn, fake)
    requests = len(fake.urls)
    with conn:
        conn.execute("UPDATE flag SET status = 'cleared' WHERE code = 'CI_VARIANT'")

    summary, client = run(conn, fake, offline=True)
    assert len(fake.urls) == requests and client.requests == 0
    flags = flags_by_line(conn)
    assert flags[("master_seq 4", "CI_VARIANT")][0] == "cleared"
    assert conn.execute("SELECT COUNT(*) FROM flag WHERE code = 'CI_VARIANT'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM candidate WHERE method = 'ci_text' AND rank = 1"
                        ).fetchone()[0] == 6


def test_unmapped_genre_is_reported_not_compared(conn, tmp_path):
    fake = setup(conn, tmp_path)
    with conn:
        conn.execute("UPDATE genre SET ci_genre = NULL WHERE code = 'HV'")
    summary, _ = run(conn, fake)
    assert summary.unconfirmed_genres == {"HV": {"HV": 1}}
    row = conn.execute("SELECT c.genre_match FROM candidate c JOIN line l ON l.id = c.line_id "
                       "WHERE l.origin_ref = 'master_seq 8' AND c.rank = 1").fetchone()
    assert row[0] == "unknown"


def test_flag_disappears_when_no_longer_true(conn, tmp_path):
    fake = setup(conn, tmp_path)
    run(conn, fake)
    with conn:
        conn.execute("UPDATE genre SET ci_genre = 'V,W' WHERE code = 'V'")
    run(conn, fake, offline=True)
    assert ("master_seq 5", "CI_GENRE_NEW_CID") not in flags_by_line(conn)
    row = conn.execute("SELECT c.text_class, c.genre_match FROM candidate c JOIN line l "
                       "ON l.id = c.line_id WHERE l.origin_ref = 'master_seq 5' AND c.rank = 1"
                       ).fetchone()
    assert tuple(row) == ("identical", "match")


def test_no_results_flag(conn, tmp_path):
    fake = setup(conn, tmp_path, corpus=[])
    summary, _ = run(conn, fake)
    assert summary.flags["CI_NO_MATCH"] == 6
    assert line_flags([], "A") == [("CI_NO_MATCH", "no search results")]


class SearchOnly:
    def __init__(self, corpus):
        self.fake = FakeCantusIndex(corpus=corpus)
        self.queries = []

    def text_search(self, query):
        self.queries.append(query)
        return self.fake._text(query)


def test_search_bridges_spelling():
    client = SearchOnly([{"cid": "003003", "genre": "A", "fulltext":
                          "Haec est vera fraternitas quae numquam potuit violari certamine"}])
    scored, queries = search(client, "Hec est vera fraternitas que numquam potuit violari certamine",
                             False, ["A"])
    assert scored[0].cid == "003003"
    assert scored[0].comparison.text_class == "identical"
    assert len(queries) == 1


def test_search_widens_when_capped():
    corpus = [{"cid": f"x{i:03d}", "genre": "A", "fulltext": f"Ave maria gratia plena virgo {i:03d}"}
              for i in range(105)]
    corpus.append({"cid": "001234", "genre": "A",
                   "fulltext": "Ave maria gratia plena dominus tecum benedicta tu"})
    client = SearchOnly(corpus)
    scored, queries = search(client, "Ave maria gratia plena dominus tecum benedicta tu",
                             False, ["A"])
    assert scored[0].cid == "001234" and scored[0].comparison.text_class == "identical"
    assert [q.count(" ") + 1 for q in queries] == [3, 4, 5]


def test_search_finds_a_different_opening():
    client = SearchOnly([{"cid": "006868.1", "genre": "R", "fulltext":
                          "Ecce homo dei ducebatur ut crucifigerent eum populus autem clamabat"}])
    scored, queries = search(client, "Homo dei ducebatur ut crucifigerent eum populus autem clamabat",
                             False, ["R"])
    assert scored[0].cid == "006868.1"
    assert scored[0].comparison.text_class == "suffix"
    assert "opening differs" in scored[0].comparison.reason


def test_genre_ranks_before_text():
    client = SearchOnly([
        {"cid": "r1", "genre": "R", "fulltext": "Homo dei ducebatur ut crucifigerent eum"},
        {"cid": "a1", "genre": "A", "fulltext": "Homo dei ducebatur ut crucifigerent eum populus"},
    ])
    scored, _ = search(client, "Homo dei ducebatur ut crucifigerent eum populus", False, ["A"])
    assert [s.cid for s in scored] == ["a1", "r1"]
    scored, _ = search(client, "Homo dei ducebatur ut crucifigerent eum populus", False, ["R"])
    assert scored[0].cid == "r1"
