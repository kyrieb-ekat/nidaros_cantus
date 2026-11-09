"""Map each feast heading of a book to a Cantus Index feast.

The Cantus Index feast list (json-feasts) is fetched once and cached. Each
heading is mapped by hand, with a status:

  exact     the Cantus Index feast is the same occasion
  variant   the closest Cantus Index feast, not an exact match
  unmapped  no Cantus Index feast fits
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .cantus import CantusIndex
from .db import DatabaseError
from .textmatch import plain

STATUSES = ("exact", "variant", "unmapped")


@dataclass(frozen=True)
class CIFeast:
    code: str | None
    name: str
    alt_names: tuple[str, ...]
    description: str | None
    date: str | None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name,) + self.alt_names


def ci_feasts(client: CantusIndex) -> list[CIFeast]:
    data = client.feasts()
    items = data.values() if isinstance(data, dict) else data
    feasts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (item.get("feastname") or "").strip()
        if not name:
            continue
        alts = tuple(a.strip() for a in (item.get("alt_feast_names") or []) if a and a.strip())
        feasts.append(CIFeast(item.get("feastcode"), name, alts,
                              item.get("description"), item.get("feastdate")))
    return feasts


def search_feasts(feasts: list[CIFeast], term: str) -> list[CIFeast]:
    """Feasts whose name, alternative names or description contain the term.

    Matching ignores case and accents, and treats i/j and u/v alike.
    """
    needle = _fold(term)
    hits = []
    for feast in feasts:
        haystack = " | ".join(_fold(t) for t in feast.names + (feast.description or "",))
        if needle in haystack:
            hits.append(feast)
    return hits


def find_feast(feasts: list[CIFeast], name: str) -> CIFeast:
    """The feast with this name or alternative name (exact, ignoring case)."""
    wanted = name.strip().casefold()
    matches = [f for f in feasts if wanted in (n.casefold() for n in f.names)]
    if not matches:
        raise DatabaseError(f"No Cantus Index feast is called '{name}'. "
                            "Use `python -m app feasts --search` to find the name.")
    if len({f.name for f in matches}) > 1:
        names = ", ".join(sorted({f.name for f in matches}))
        raise DatabaseError(f"'{name}' matches several Cantus Index feasts: {names}.")
    return matches[0]


def feast_headings(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    """Each feast heading of the book with its line count and mapping, in book order."""
    return conn.execute(
        """
        SELECT l.feast, COUNT(*) AS lines, MIN(l.page_start) AS first_page,
               m.ci_feast, m.ci_feastcode, m.status, m.note
        FROM line AS l
        LEFT JOIN feast_map AS m ON m.source_id = l.source_id AND m.feast = l.feast
        WHERE l.source_id = ? AND l.feast IS NOT NULL
        GROUP BY l.feast
        ORDER BY MIN(l.page_start * 100000 + l.line_on_page)
        """, (source_id,)).fetchall()


def map_feast(conn: sqlite3.Connection, source_id: int, heading: str, status: str,
              feast: CIFeast | None = None, note: str | None = None) -> None:
    if status not in STATUSES:
        raise DatabaseError(f"Status must be one of {', '.join(STATUSES)}.")
    if (status == "unmapped") != (feast is None):
        raise DatabaseError("Give a Cantus Index feast for exact or variant, and none for unmapped.")
    exists = conn.execute("SELECT 1 FROM line WHERE source_id = ? AND feast = ? LIMIT 1",
                          (source_id, heading)).fetchone()
    if not exists:
        raise DatabaseError(f"No lines have the feast heading '{heading}'.")
    with conn:
        conn.execute(
            """
            INSERT INTO feast_map (source_id, feast, ci_feast, ci_feastcode, status, note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, feast) DO UPDATE SET
                ci_feast = excluded.ci_feast, ci_feastcode = excluded.ci_feastcode,
                status = excluded.status, note = excluded.note,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (source_id, heading, feast.name if feast else None,
             feast.code if feast else None, status, note),
        )


def mapped_feast_names(conn: sqlite3.Connection, source_id: int, heading: str | None,
                       feasts: list[CIFeast] | None = None) -> set[str]:
    """Cantus Index names (with alternatives, if the list is given) for a heading."""
    if heading is None:
        return set()
    row = conn.execute("SELECT ci_feast FROM feast_map WHERE source_id = ? AND feast = ?",
                       (source_id, heading)).fetchone()
    if row is None or row[0] is None:
        return set()
    names = {row[0]}
    for feast in feasts or []:
        if feast.name == row[0]:
            names.update(feast.alt_names)
    return names


def _fold(text: str) -> str:
    return plain(text).replace("j", "i").replace("v", "u")
