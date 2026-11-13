"""Cantus Database positions for imported chants.

Rules from https://cantusdatabase.org/description/ ("Position"):

* A chant that is the only one of its genre in a service: blank.
* Lauds and Vespers: psalm antiphons numbered 1, 2, ... by their place in the
  service. One antiphon for all the psalms is "p"; one antiphon beginning a
  series (et reliquae, cum ceteris ...) is "1".
* Canticle antiphons: "M" (Magnificat), "B" (Benedictus), "N" (Nunc dimittis);
  several are "1M", "2M" ... An antiphon is a canticle antiphon when the next
  line is the canticle's psalm cue ("Psalmus Benedictus"), when its slot says so
  (ad Magnificat, in evangelio ...), or when it follows the hymn or versicle.
* Matins: antiphons and responsories "nocturn.place" (1.2); a single antiphon
  for a nocturn, and a nocturn's versicle, "n." Without nocturns, numbered in
  series. Invitatories are blank. A versicle after the last responsory of the
  last nocturn closes Matins and is blank, as in Cantus Database's index of the
  Tsgrooten Antiphoner (B-Gu Hs BKT.006, fol. 166r: "M W" with no position).
* Verses of antiphons and responsories: "01", "02" ..., even when only one.
* Memorials (chants under a commemoration rubric, office known): "R".

Hymns are numbered by stanza: the hymn (first stanza) blank, then its verses
"02", "03" ... as stanza numbers. This follows the stanza numbering of hymn
Cantus IDs (ah51067:02) and is flagged for checking.

Cases that need a judgement are returned with a reason and become
POSITION_CHECK flags. A position set by hand (position_override) always wins.
"""

from __future__ import annotations

import re
import sqlite3
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from .db import refresh_flags

CANTICLE_OFFICES = {"L": "B", "V": "M", "V2": "M", "C": "N"}
CANTICLE_SLOTS = {"ad magnificat": "M", "ad benedictus": "B", "ad nunc dimittis": "N",
                  "in evangelio": None}            # None: the office's own canticle
CANTICLE_CUES = (("magnificat", "M"), ("benedictus", "B"), ("nunc dimittis", "N"))
SERIES_RUBRIC = re.compile(r"\b(cum\s+ceteris|et\s+reliqu|ceter|reliqu)", re.I)
FLAG = "POSITION_CHECK"


@dataclass
class Position:
    value: str | None
    check: str | None = None        # why a judgement is needed, if it is


def compute_positions(conn: sqlite3.Connection, source_id: int) -> dict[int, Position]:
    """Position for every imported chant of a book, keyed by line id."""
    every = conn.execute(
        "SELECT l.*, COALESCE(g.imported, 0) AS imported FROM line AS l "
        "LEFT JOIN genre AS g ON g.code = l.genre "
        "WHERE l.source_id = ? ORDER BY l.page_start, l.line_on_page", (source_id,)).fetchall()
    rows = [r for r in every if r["imported"]]
    cue_after = _canticle_cues(every)
    verse_of = dict(conn.execute(
        "SELECT from_line, to_line FROM line_link WHERE kind = 'verse_of'").fetchall())
    stanza_of = dict(conn.execute(
        "SELECT from_line, to_line FROM line_link WHERE kind = 'stanza_of'").fetchall())

    result: dict[int, Position] = {}
    groups: OrderedDict = OrderedDict()
    for row in rows:
        groups.setdefault((row["feast"], row["office"]), []).append(row)

    for (_, office), members in groups.items():
        if not office:
            for row in members:
                result[row["id"]] = Position(None, "no office: position cannot be determined")
            continue
        main, memorial, verses, stanzas = [], [], [], []
        for row in members:
            if row["genre"] == "V":
                verses.append(row)
            elif row["genre"] == "HV":
                stanzas.append(row)
            elif row["sub_section"]:
                memorial.append(row)
            else:
                main.append(row)
        for row in memorial:
            result[row["id"]] = Position("R")
        _verses(verses, verse_of, result)
        if office == "M":
            _matins(main, result)
        elif office in CANTICLE_OFFICES:
            _canticle_office(main, CANTICLE_OFFICES[office], cue_after, result)
        else:
            _singletons_or_series(main, result)
        _stanzas(main, stanzas, stanza_of, result)     # after the office rules: adds the hymn check

    for row in rows:
        if row["position_override"]:
            result[row["id"]] = Position(row["position_override"])
    return result


def _verses(verses, verse_of, result) -> None:
    count: dict[int, int] = defaultdict(int)
    for row in verses:
        parent = verse_of.get(row["id"])
        if parent is None:
            result[row["id"]] = Position("01", "verse not linked to a responsory; numbered 01")
            continue
        count[parent] += 1
        result[row["id"]] = Position(f"{count[parent]:02d}")


def _stanzas(main, stanzas, stanza_of, result) -> None:
    number: dict[int, int] = defaultdict(lambda: 1)
    for row in stanzas:
        hymn = stanza_of.get(row["id"])
        if hymn is None:
            result[row["id"]] = Position(None, "hymn verse not linked to a hymn")
            continue
        number[hymn] += 1
        result[row["id"]] = Position(f"{number[hymn]:02d}")
    hymns_with_stanzas = set(number)
    for row in main:
        if row["genre"] == "H" and row["id"] in hymns_with_stanzas:
            current = result.get(row["id"], Position(None))
            note = "hymn stanzas numbered 02, 03 ... after the hymn; convention to confirm"
            result[row["id"]] = Position(current.value,
                                         f"{current.check}; {note}" if current.check else note)


def _by_genre(rows):
    out: dict[str, list] = defaultdict(list)
    for row in rows:
        out[row["genre"]].append(row)
    return out


def _matins(main, result) -> None:
    by_genre = _by_genre(main)
    for row in by_genre.pop("I", []):
        result[row["id"]] = Position(None)
    _single_or_flag(by_genre.pop("H", []), "several hymns at Matins", result)

    nocturns: dict = defaultdict(lambda: defaultdict(list))
    unassigned: dict[str, list] = defaultdict(list)
    for genre in ("A", "R", "W"):
        for row in by_genre.pop(genre, []):
            if row["nocturn"]:
                nocturns[row["nocturn"]][genre].append(row)
            else:
                unassigned[genre].append(row)
    last_nocturn = max(nocturns, default=None, key=_nocturn_number)
    for n, parts in nocturns.items():
        ants = parts["A"]
        if len(ants) == 1:
            result[ants[0]["id"]] = Position(f"{n}.")
        for k, row in enumerate(ants if len(ants) > 1 else [], start=1):
            result[row["id"]] = Position(f"{n}.{k}")
        for k, row in enumerate(parts["R"], start=1):
            result[row["id"]] = Position(f"{n}.{k}")
        closing = []
        if n == last_nocturn and parts["R"]:
            last_r = _order(parts["R"][-1])
            closing = [w for w in parts["W"] if _order(w) > last_r]
        for row in closing:
            result[row["id"]] = Position(None)          # closes Matins
        opening = [w for w in parts["W"] if w not in closing]
        for row in opening:
            check = f"several versicles in nocturn {n}" if len(opening) > 1 else None
            result[row["id"]] = Position(f"{n}.", check)
    for genre, rows in unassigned.items():
        for k, row in enumerate(rows, start=1):
            if genre == "W":
                result[row["id"]] = Position(None, "Matins versicle without a nocturn")
            else:
                result[row["id"]] = Position(str(k), "Matins without nocturns: numbered in series")
    _singletons_or_series([r for rows in by_genre.values() for r in rows], result)


def _order(row) -> tuple[int, int]:
    return (row["page_start"], row["line_on_page"])


def _nocturn_number(n) -> int:
    try:
        return int(n)
    except (TypeError, ValueError):
        return 0


def _canticle_cues(every) -> dict[int, str]:
    """Antiphon id -> canticle letter, where the next line is that canticle's cue."""
    found = {}
    for row, nxt in zip(every, every[1:]):
        if row["genre"] == "A" and nxt["row_type"] == "psalm_cue":
            text = " ".join(x for x in (nxt["fulltext_edition"], nxt["rubric"]) if x).lower()
            for word, letter in CANTICLE_CUES:
                if word in text:
                    found[row["id"]] = letter
                    break
    return found


def _canticle_office(main, letter, cue_after, result) -> None:
    psalm, canticle, others = [], [], []
    letters: dict[int, str] = {}
    past_psalms = False
    for row in main:
        slot = (row["slot"] or "").lower()
        if row["genre"] == "A":
            if row["id"] in cue_after:
                canticle.append(row)
                letters[row["id"]] = cue_after[row["id"]]
            elif slot in CANTICLE_SLOTS:
                canticle.append(row)
                letters[row["id"]] = CANTICLE_SLOTS[slot] or letter
            elif slot == "super psalmos":
                psalm.append(row)
            elif past_psalms:
                canticle.append(row)
            else:
                psalm.append(row)
        else:
            others.append(row)
            if row["genre"] in ("H", "W"):
                past_psalms = True

    if len(psalm) == 1:
        row = psalm[0]
        if SERIES_RUBRIC.search(row["rubric"] or ""):
            result[row["id"]] = Position("1")
        else:
            result[row["id"]] = Position("p", "single psalm antiphon: 'p' if sung with all psalms, "
                                              "'1' if it begins a series")
    else:
        too_many = len(psalm) > 5
        for k, row in enumerate(psalm, start=1):
            result[row["id"]] = Position(str(k), "more than five psalm antiphons" if too_many else None)

    by_letter: dict[str, list] = defaultdict(list)
    for row in canticle:
        by_letter[letters.get(row["id"], letter)].append(row)
    for canticle_letter, members in by_letter.items():
        if len(members) == 1:
            result[members[0]["id"]] = Position(canticle_letter)
        else:
            for k, row in enumerate(members, start=1):
                result[row["id"]] = Position(f"{k}{canticle_letter}")
    _singletons_or_series(others, result)


def _single_or_flag(rows, reason, result) -> None:
    for row in rows:
        result[row["id"]] = Position(None, reason if len(rows) > 1 else None)


def _singletons_or_series(rows, result) -> None:
    """Blank for the only chant of its genre; numbered and flagged otherwise."""
    for genre, members in _by_genre(rows).items():
        if len(members) == 1:
            result[members[0]["id"]] = Position(None)
            continue
        for k, row in enumerate(members, start=1):
            result[row["id"]] = Position(str(k), f"several {genre} chants in this service: "
                                                 "numbered in series; alternates share a position")


def refresh_position_flags(conn: sqlite3.Connection, source_id: int) -> dict[int, Position]:
    """Recompute positions and make the POSITION_CHECK flags match."""
    positions = compute_positions(conn, source_id)
    with conn:
        for line_id, pos in positions.items():
            refresh_flags(conn, line_id, (FLAG,), [(FLAG, pos.check)] if pos.check else [])
    return positions
