from __future__ import annotations

import pytest

from app import db
from app.positions import compute_positions, refresh_position_flags


class Book:
    """Builds lines directly, in order, for position tests."""

    def __init__(self, conn):
        self.conn = conn
        self.n = 0
        self.last = {}
        with conn:
            conn.execute("INSERT INTO source (siglum, title) VALUES ('t', 'T')")
            conn.execute("INSERT INTO import_batch (source_id, file_name, file_sha256, page_first, "
                         "page_last, line_count) VALUES (1, 'f', 'x', 1, 1, 0)")

    def add(self, genre=None, office="V", row_type=None, nocturn=None, slot=None, memorial=None,
            rubric=None, text="Textus", feast="F", link=None):
        self.n += 1
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO line (source_id, batch_id, page_start, line_on_page, row_type, feast, "
                "office, nocturn, slot, genre, sub_section, rubric, fulltext_standardized, "
                "fulltext_edition) VALUES (1, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.n * 10, row_type or (genre or "rubric"), feast, office, nocturn, slot, genre,
                 memorial, rubric, text, text))
            line_id = cur.lastrowid
            if link:
                kind, target = link
                self.conn.execute("INSERT INTO line_link (from_line, to_line, kind, origin) "
                                  "VALUES (?, ?, ?, 'import')", (line_id, target, kind))
        return line_id


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "p.sqlite"
    db.init(path)
    conn = db.connect(path)
    yield Book(conn)
    conn.close()


def values(book, ids):
    pos = compute_positions(book.conn, 1)
    return [pos[i].value for i in ids], [pos[i].check for i in ids]


def test_vespers(book):
    ants = [book.add("A") for _ in range(5)]
    r = book.add("R")
    v = book.add("V", link=("verse_of", r))
    h = book.add("H")
    w = book.add("W")
    m = book.add("A")
    vals, checks = values(book, ants + [r, v, h, w, m])
    assert vals == ["1", "2", "3", "4", "5", None, "01", None, None, "M"]
    assert not any(checks)


def test_single_psalm_antiphon(book):
    a = book.add("A", rubric="cum ceteris aa.iij.")
    book.add("H")
    b = book.add("A", office="V2", rubric="Antiphona")
    vals, checks = values(book, [a, b])
    assert vals[0] == "1" and checks[0] is None
    assert vals[1] == "p" and "single psalm antiphon" in checks[1]


def test_lauds_benedictus_from_psalm_cue(book):
    ants = [book.add("A", office="L") for _ in range(6)]
    book.add(row_type="psalm_cue", office="L", text="Benedictus", rubric="Psalmus")
    vals, checks = values(book, ants)
    assert vals == ["1", "2", "3", "4", "5", "B"] and not any(checks)


def test_canticle_letter_from_slot(book):
    a = book.add("A", office="L", slot="ad Magnificat")
    b = book.add("A", office="C")
    book.add(row_type="psalm_cue", office="C", text="Nunc dimittis")
    vals, _ = values(book, [a, b])
    assert vals == ["M", "N"]


def test_matins(book):
    i = book.add("I", office="M")
    h = book.add("H", office="M")
    n1 = [book.add("A", office="M", nocturn=1) for _ in range(3)]
    w1 = book.add("W", office="M", nocturn=1)
    r1 = book.add("R", office="M", nocturn=1)
    v1 = book.add("V", office="M", nocturn=1, link=("verse_of", r1))
    r2 = book.add("R", office="M", nocturn=1)
    a2 = book.add("A", office="M", nocturn=2)
    w2 = book.add("W", office="M", nocturn=2)
    r3 = book.add("R", office="M", nocturn=2)
    vals, checks = values(book, [i, h] + n1 + [w1, r1, v1, r2, a2, w2, r3])
    assert vals == [None, None, "1.1", "1.2", "1.3", "1.", "1.1", "01", "1.2", "2.", "2.", "2.1"]
    assert not any(checks)


def test_versicle_closing_matins_is_blank(book):
    n2 = [book.add("W", office="M", nocturn=2), book.add("R", office="M", nocturn=2)]
    w3 = book.add("W", office="M", nocturn=3)
    r3 = [book.add("R", office="M", nocturn=3) for _ in range(3)]
    v3 = book.add("V", office="M", nocturn=3, link=("verse_of", r3[-1]))
    closing = book.add("W", office="M", nocturn=3)
    vals, checks = values(book, n2 + [w3] + r3 + [v3, closing])
    assert vals == ["2.", "2.1", "3.", "3.1", "3.2", "3.3", "01", None]
    assert not any(checks)


def test_two_versicles_before_the_responsories_are_flagged(book):
    a = book.add("W", office="M", nocturn=1)
    b = book.add("W", office="M", nocturn=1)
    book.add("R", office="M", nocturn=1)
    vals, checks = values(book, [a, b])
    assert vals == ["1.", "1."] and all("several versicles" in c for c in checks)


def test_matins_without_nocturns_is_flagged(book):
    a, b = book.add("A", office="M"), book.add("A", office="M")
    vals, checks = values(book, [a, b])
    assert vals == ["1", "2"] and all("without nocturns" in c for c in checks)


def test_memorial_hours_hymns_and_override(book):
    mem = book.add("A", office="L", memorial="Commemoratio de aduentu")
    tr = book.add("R", office="T")
    tv = book.add("V", office="T", link=("verse_of", tr))
    h = book.add("H", office="V")
    hv1 = book.add("HV", office="V", link=("stanza_of", h))
    hv2 = book.add("HV", office="V", link=("stanza_of", h))
    none = book.add("A", office=None)
    vals, checks = values(book, [mem, tr, tv, h, hv1, hv2, none])
    assert vals == ["R", None, "01", None, "02", "03", None]
    assert "hymn stanzas" in checks[3] and "no office" in checks[6]

    db.update_line(book.conn, none, "position_override", "1")
    vals, checks = values(book, [none])
    assert vals == ["1"] and checks == [None]


def test_flags_follow_positions(book):
    a = book.add("A", office="M")
    b = book.add("A", office="M")
    refresh_position_flags(book.conn, 1)
    count = "SELECT COUNT(*) FROM flag WHERE code = 'POSITION_CHECK' AND status = 'open'"
    assert book.conn.execute(count).fetchone()[0] == 2
    db.update_line(book.conn, a, "position_override", "1.1")
    db.update_line(book.conn, b, "position_override", "1.2")
    refresh_position_flags(book.conn, 1)
    assert book.conn.execute(count).fetchone()[0] == 0
