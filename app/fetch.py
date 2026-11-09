"""Look up Cantus Index candidates for lines and flag what needs attention.

For each line the steps are:

1. Choose the text to compare: the standardized text, or for an incipit-only
   line the full text it refers to, when that is known.
2. Search Cantus Index, starting with the first three words and widening when
   the 100-result cap is hit. If nothing close turns up, try the first two
   words, then words two to four (catches a different opening), then the
   longest word.
3. Compare every result with the line's text and genre (textmatch.compare).
4. Store the best candidates and fetch full records for the top three.
5. Refresh the line's CI_* flags.

Nothing is decided here. Rankings and flags are proposals for review.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .cantus import RESULT_CAP, CantusError, CantusIndex
from .textmatch import (CLASS_ORDER, Comparison, compare, distinctive_word,
                        query_words, tolerant_query)

METHOD = "ci_text"
KEEP = 10               # candidates stored per line
DETAIL = 3              # candidates whose full record (json-cid) is fetched
MAX_WORDS = 6           # widest search
CI_FLAGS = ("CI_VARIANT", "CI_GENRE_NEW_CID", "CI_TEXT_INCOMPLETE", "CI_NO_MATCH")
GOOD = ("identical", "minor")


@dataclass
class Scored:
    cid: str
    ci_text: str
    ci_genre: str | None
    comparison: Comparison
    genre_match: str

    @property
    def rank_key(self):
        return (self.genre_match == "mismatch", CLASS_ORDER[self.comparison.text_class],
                -self.comparison.score, self.cid)


@dataclass
class LineResult:
    line_id: int
    queries: list[str] = field(default_factory=list)
    candidates: list[Scored] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None

    @property
    def best(self) -> Scored | None:
        return self.candidates[0] if self.candidates else None


# --------------------------------------------------------------------------
# Selecting lines and texts

def target_lines(conn: sqlite3.Connection, source_id: int, feast: str | None = None,
                 line_ids: Iterable[int] | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT * FROM line_export WHERE source_id = ? AND in_import = 1")
    params: list = [source_id]
    if feast is not None:
        sql += " AND feast = ?"
        params.append(feast)
    ids = list(line_ids or [])
    if ids:
        sql += f" AND id IN ({', '.join('?' for _ in ids)})"
        params.extend(ids)
    return conn.execute(sql + " ORDER BY page_start, line_on_page", params).fetchall()


def comparison_text(conn: sqlite3.Connection, line: sqlite3.Row) -> tuple[str | None, bool]:
    """The text to compare, and whether it is only a cue (opening words)."""
    if not line["incipit_only"]:
        return line["fulltext_standardized"], False
    target = conn.execute(
        "SELECT t.fulltext_standardized FROM line_link k JOIN line t ON t.id = k.to_line "
        "WHERE k.from_line = ? AND k.kind = 'full_text_in'", (line["id"],)).fetchone()
    if target and target[0]:
        return target[0], False
    if line["xref_resolved_text"]:
        return line["xref_resolved_text"], False
    return line["fulltext_standardized"], True


def ci_genres(conn: sqlite3.Connection, genre: str | None) -> list[str] | None:
    """Cantus Index genre codes for a local genre, or None if not confirmed."""
    if genre is None:
        return None
    row = conn.execute("SELECT ci_genre FROM genre WHERE code = ?", (genre,)).fetchone()
    if row is None or row[0] is None:
        return None
    return [g.strip() for g in row[0].split(",") if g.strip()]


# --------------------------------------------------------------------------
# Searching and scoring

def score_results(results: Iterable[dict], text: str, is_cue: bool,
                  genres: list[str] | None) -> list[Scored]:
    scored: dict[str, Scored] = {}
    for item in results:
        cid = (item.get("cid") or "").strip()
        if not cid or cid in scored:
            continue
        ci_text = item.get("fulltext") or ""
        ci_genre = item.get("genre")
        if genres is None:
            genre_match = "unknown"
        else:
            genre_match = "match" if ci_genre in genres else "mismatch"
        scored[cid] = Scored(cid, ci_text, ci_genre, compare(text, ci_text, is_cue), genre_match)
    return sorted(scored.values(), key=lambda s: s.rank_key)


def search(client: CantusIndex, text: str, is_cue: bool,
           genres: list[str] | None) -> tuple[list[Scored], list[str]]:
    ws = query_words(text)
    if not ws:
        return [], []
    results: list[dict] = []
    queries: list[str] = []

    def run(query: str) -> int:
        if len(query.replace("%", "").replace("_", "").strip()) < 2:
            return 0
        queries.append(query)
        found = client.text_search(query)
        results.extend(found)
        return len(found)

    def close_enough() -> bool:
        return any(s.comparison.text_class != "new"
                   for s in score_results(results, text, is_cue, genres))

    # 1. Opening words, widened while the result cap is hit.
    n = min(3, len(ws))
    count = run(tolerant_query(ws, n))
    while count >= RESULT_CAP and n < min(MAX_WORDS, len(ws)):
        n += 1
        count = run(tolerant_query(ws, n))

    # 2-4. Fallbacks, only while nothing close has been found.
    fallbacks = []
    if len(ws) > 2:
        fallbacks.append(tolerant_query(ws, 2))
    if len(ws) >= 4:
        fallbacks.append(tolerant_query(ws[1:], 3))
    word = distinctive_word(ws)
    if word:
        fallbacks.append(tolerant_query([word], 1))
    for query in fallbacks:
        if close_enough():
            break
        if query not in queries:
            run(query)

    return score_results(results, text, is_cue, genres), queries


def line_flags(scored: list[Scored], line_genre: str | None) -> list[tuple[str, str]]:
    """CI_* flags for a line, each with a note naming the CID it concerns."""
    same = [s for s in scored if s.genre_match != "mismatch"]
    other_good = [s for s in scored if s.genre_match == "mismatch"
                  and s.comparison.text_class in GOOD]
    best = same[0] if same else None
    flags: list[tuple[str, str]] = []

    if best and best.comparison.text_class == "incomplete":
        flags.append(("CI_TEXT_INCOMPLETE",
                      f"probable parent {best.cid}: {best.comparison.reason}"))
    elif best and best.comparison.text_class == "suffix":
        flags.append(("CI_VARIANT", f"probable parent {best.cid}: {best.comparison.reason}"))

    if other_good and not (best and best.comparison.text_class in GOOD):
        g = other_good[0]
        flags.append(("CI_GENRE_NEW_CID",
                      f"text matches {g.cid} (genre {g.ci_genre}; this line is {line_genre})"))

    if not flags and not (best and best.comparison.text_class in GOOD):
        if not scored:
            flags.append(("CI_NO_MATCH", "no search results"))
        else:
            closest = best or scored[0]
            flags.append(("CI_NO_MATCH",
                          f"closest {closest.cid}: {closest.comparison.reason}"))
    return flags


# --------------------------------------------------------------------------
# Storing

def _kept(scored: list[Scored]) -> list[Scored]:
    kept = scored[:KEEP]
    extra = [s for s in scored[KEEP:] if s.genre_match == "mismatch"
             and s.comparison.text_class in GOOD][:3]
    return kept + extra


def store(conn: sqlite3.Connection, line_id: int, scored: list[Scored],
          flags: list[tuple[str, str]]) -> None:
    with conn:
        conn.execute("DELETE FROM candidate WHERE line_id = ? AND method = ?", (line_id, METHOD))
        for rank, s in enumerate(_kept(scored), start=1):
            conn.execute(
                "INSERT INTO candidate (line_id, cid, score, rank, method, cand_text, cand_genre, "
                "text_class, class_reason, genre_match) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (line_id, s.cid, round(s.comparison.score, 4), rank, METHOD, s.ci_text,
                 s.ci_genre, s.comparison.text_class, s.comparison.reason, s.genre_match),
            )
        _refresh_flags(conn, line_id, flags)


def _refresh_flags(conn: sqlite3.Connection, line_id: int, flags: list[tuple[str, str]]) -> None:
    """Add new CI flags, update notes, and drop open ones that no longer apply.

    A flag that was cleared stays cleared.
    """
    wanted = dict(flags)
    existing = {r["code"]: r for r in conn.execute(
        f"SELECT code, status FROM flag WHERE line_id = ? AND code IN "
        f"({', '.join('?' for _ in CI_FLAGS)})", (line_id, *CI_FLAGS))}
    for code, row in existing.items():
        if code not in wanted and row["status"] == "open":
            conn.execute("DELETE FROM flag WHERE line_id = ? AND code = ?", (line_id, code))
        elif code in wanted and row["status"] == "open":
            conn.execute("UPDATE flag SET note = ? WHERE line_id = ? AND code = ?",
                         (wanted[code], line_id, code))
    next_ord = conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM flag WHERE line_id = ?",
                            (line_id,)).fetchone()[0]
    for code, note in flags:
        if code not in existing:
            conn.execute("INSERT INTO flag (line_id, code, ord, note) VALUES (?, ?, ?, ?)",
                         (line_id, code, next_ord, note))
            next_ord += 1


# --------------------------------------------------------------------------
# Running

def fetch_line(conn: sqlite3.Connection, client: CantusIndex, line: sqlite3.Row) -> LineResult:
    result = LineResult(line_id=line["id"])
    text, is_cue = comparison_text(conn, line)
    if not text:
        result.error = "the line has no text to search with"
        return result
    genres = ci_genres(conn, line["genre"])
    try:
        scored, result.queries = search(client, text, is_cue, genres)
        flags = line_flags(scored, line["genre"])
        # Full records for the leading candidates, and for a matching text of
        # another genre when it is the reason for a flag.
        detail = [s for s in scored if s.genre_match != "mismatch"][:DETAIL]
        detail += [s for s in scored if s.genre_match == "mismatch"
                   and s.comparison.text_class in GOOD][:1]
        for s in detail:
            client.chant(s.cid)
    except CantusError as exc:
        result.error = str(exc)
        return result
    store(conn, line["id"], scored, flags)
    result.candidates, result.flags = scored, flags
    return result


@dataclass
class FetchSummary:
    lines: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    flags: Counter = field(default_factory=Counter)
    unconfirmed_genres: dict[str, Counter] = field(default_factory=dict)


def fetch_lines(conn: sqlite3.Connection, client: CantusIndex, lines: list[sqlite3.Row],
                progress: Callable[[int, int, sqlite3.Row, LineResult], None] | None = None
                ) -> FetchSummary:
    summary = FetchSummary()
    for index, line in enumerate(lines, start=1):
        result = fetch_line(conn, client, line)
        summary.lines += 1
        if result.error:
            summary.errors.append((line["id"], result.error))
        for code, _ in result.flags:
            summary.flags[code] += 1
        if ci_genres(conn, line["genre"]) is None:
            # Which Cantus Index genres close matches have, to settle the mapping.
            seen = summary.unconfirmed_genres.setdefault(line["genre"], Counter())
            for s in result.candidates:
                if s.comparison.text_class in GOOD:
                    seen[s.ci_genre or "(none)"] += 1
        if progress:
            progress(index, len(lines), line, result)
    return summary
