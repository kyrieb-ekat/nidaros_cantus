"""What the review screen shows: the queue, a line in context, its candidates."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..cantus import CantusError, CantusIndex, Evidence, evidence
from ..feasts import ci_feasts, mapped_feast_names
from ..fetch import comparison_text
from ..positions import Position, compute_positions
from ..textmatch import words

DECISION_FILTERS = ("undecided", "Confirm", "Reject", "Fixed", "Variant", "New CID")
CONTEXT = 4  # lines shown before and after


@dataclass
class Filters:
    feast: str | None = None
    office: str | None = None
    flag: str | None = None
    status: str | None = None

    @classmethod
    def from_args(cls, args) -> "Filters":
        return cls(
            **{k: (args.get(k) or None) for k in ("feast", "office", "flag", "status")}
        )

    def query(self, **changes) -> dict:
        values = {k: getattr(self, k) for k in ("feast", "office", "flag", "status")}
        values.update(changes)
        return {k: v for k, v in values.items() if v}


def queue(
    conn: sqlite3.Connection, source_id: int, filters: Filters
) -> list[sqlite3.Row]:
    sql = """
        SELECT l.id, l.page_start, l.office, l.genre, l.feast, l.fulltext_standardized,
               d.status AS decision, d.cid AS decided_cid,
               (SELECT COUNT(*) FROM flag f WHERE f.line_id = l.id AND f.status = 'open') AS open_flags
        FROM line_export AS l
        LEFT JOIN current_decision AS d ON d.line_id = l.id
        WHERE l.source_id = ? AND l.in_import = 1
    """
    params: list = [source_id]
    if filters.feast:
        sql += " AND l.feast = ?"
        params.append(filters.feast)
    if filters.office:
        sql += " AND l.office = ?"
        params.append(filters.office)
    if filters.flag:
        sql += (
            " AND EXISTS (SELECT 1 FROM flag f WHERE f.line_id = l.id AND f.status = 'open'"
            " AND f.code = ?)"
        )
        params.append(filters.flag)
    if filters.status == "undecided":
        sql += " AND d.status IS NULL"
    elif filters.status:
        sql += " AND d.status = ?"
        params.append(filters.status)
    return conn.execute(
        sql + " ORDER BY l.page_start, l.line_on_page", params
    ).fetchall()


def filter_choices(conn: sqlite3.Connection, source_id: int) -> dict:
    feasts = [
        r[0]
        for r in conn.execute(
            "SELECT feast FROM line WHERE source_id = ? AND feast IS NOT NULL GROUP BY feast "
            "ORDER BY MIN(page_start * 100000 + line_on_page)",
            (source_id,),
        )
    ]
    offices = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT office FROM line_export WHERE source_id = ? AND in_import = 1 "
            "AND office IS NOT NULL ORDER BY office",
            (source_id,),
        )
    ]
    flags = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT f.code FROM flag f JOIN line l ON l.id = f.line_id "
            "WHERE l.source_id = ? AND f.status = 'open' ORDER BY f.code",
            (source_id,),
        )
    ]
    return {
        "feasts": feasts,
        "offices": offices,
        "flags": flags,
        "statuses": DECISION_FILTERS,
    }


def progress(conn: sqlite3.Connection, source_id: int) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(d.status IS NOT NULL) AS decided,
               SUM(d.status IN ('Variant', 'New CID')) AS waiting
        FROM line_export AS l LEFT JOIN current_decision AS d ON d.line_id = l.id
        WHERE l.source_id = ? AND l.in_import = 1
        """,
        (source_id,),
    ).fetchone()
    return {k: row[k] or 0 for k in ("total", "decided", "waiting")}


# --------------------------------------------------------------------------
# One line


def context(conn: sqlite3.Connection, line: sqlite3.Row) -> list[sqlite3.Row]:
    before = conn.execute(
        "SELECT * FROM line_export WHERE source_id = ? AND (page_start < ? OR "
        "(page_start = ? AND line_on_page < ?)) ORDER BY page_start DESC, line_on_page DESC LIMIT ?",
        (
            line["source_id"],
            line["page_start"],
            line["page_start"],
            line["line_on_page"],
            CONTEXT,
        ),
    ).fetchall()
    after = conn.execute(
        "SELECT * FROM line_export WHERE source_id = ? AND (page_start > ? OR "
        "(page_start = ? AND line_on_page > ?)) ORDER BY page_start, line_on_page LIMIT ?",
        (
            line["source_id"],
            line["page_start"],
            line["page_start"],
            line["line_on_page"],
            CONTEXT,
        ),
    ).fetchall()
    return list(reversed(before)) + [line] + after


@dataclass
class Token:
    text: str
    op: str  # "same", "here" (only in this line's text), "there" (only in the candidate)


@dataclass
class CandidateView:
    row: sqlite3.Row
    ev: Evidence | None
    concordances: list[dict]
    diff_local: list[Token]
    diff_ci: list[Token]
    promoted: bool = False  # verse related to the responsory's confirmed CID


@dataclass
class LineView:
    line: sqlite3.Row
    text: str | None
    is_cue: bool
    position: Position | None
    mapping: sqlite3.Row | None
    decision: sqlite3.Row | None
    flags: list[sqlite3.Row]
    candidates: list[CandidateView]
    context: list[sqlite3.Row]
    history: list[dict] = field(default_factory=list)
    responsory_cid: str | None = None


def line_view(conn: sqlite3.Connection, line_id: int) -> LineView | None:
    line = conn.execute("SELECT * FROM line_export WHERE id = ?", (line_id,)).fetchone()
    if line is None:
        return None
    text, is_cue = comparison_text(conn, line)
    position = compute_positions(conn, line["source_id"]).get(line["id"])
    mapping = conn.execute(
        "SELECT * FROM feast_map WHERE source_id = ? AND feast = ?",
        (line["source_id"], line["feast"]),
    ).fetchone()
    decision = conn.execute(
        "SELECT * FROM current_decision WHERE line_id = ?", (line_id,)
    ).fetchone()
    flags = conn.execute(
        "SELECT * FROM flag WHERE line_id = ? ORDER BY ord", (line_id,)
    ).fetchall()

    cache = CantusIndex(conn, offline=True)
    try:
        known_feasts = ci_feasts(cache)
    except CantusError:
        known_feasts = []
    names = mapped_feast_names(conn, line["source_id"], line["feast"], known_feasts)

    responsory_cid = None
    if line["genre"] == "V":
        r = conn.execute(
            "SELECT d.cid FROM line_link k JOIN current_decision d ON d.line_id = k.to_line "
            "WHERE k.from_line = ? AND k.kind = 'verse_of' AND d.status IN ('Confirm', 'Fixed')",
            (line_id,),
        ).fetchone()
        responsory_cid = r[0] if r else None

    views = []
    for c in conn.execute(
        "SELECT * FROM candidate WHERE line_id = ? ORDER BY method, rank", (line_id,)
    ):
        try:
            data = cache.chant(c["cid"])
        except CantusError:
            data = None
        ev = evidence(c["cid"], data, names, line["office"])
        local_tokens, ci_tokens = word_diff(text or "", c["cand_text"] or "")
        views.append(
            CandidateView(
                row=c,
                ev=ev,
                concordances=(data or {}).get("chants") or [],
                diff_local=local_tokens,
                diff_ci=ci_tokens,
                promoted=bool(
                    responsory_cid and ev and ev.related_cid == responsory_cid
                ),
            )
        )
    views.sort(
        key=lambda v: not v.promoted
    )  # stable: keeps the stored ranking otherwise

    view = LineView(
        line=line,
        text=text,
        is_cue=is_cue,
        position=position,
        mapping=mapping,
        decision=decision,
        flags=flags,
        candidates=views,
        context=context(conn, line),
        responsory_cid=responsory_cid,
    )
    view.history = history(conn, line_id)
    return view


def history(conn: sqlite3.Connection, line_id: int) -> list[dict]:
    items = [
        {
            "at": r["decided_at"],
            "what": f"Decision: {r['status']}"
            + (f" {r['cid']}" if r["cid"] else "")
            + (f" (parent {r['parent_cid']})" if r["parent_cid"] else ""),
            "note": r["note"] or r["reason"],
        }
        for r in conn.execute("SELECT * FROM decision WHERE line_id = ?", (line_id,))
    ]
    items += [
        {
            "at": r["edited_at"],
            "what": f"{r['field']}: {r['old_value'] or '(empty)'} "
            f"→ {r['new_value'] or '(empty)'}",
            "note": r["note"],
        }
        for r in conn.execute("SELECT * FROM edit_log WHERE line_id = ?", (line_id,))
    ]
    return sorted(items, key=lambda x: x["at"], reverse=True)


def word_diff(local: str, ci: str) -> tuple[list[Token], list[Token]]:
    """Word-level diff on normalised spelling, shown in each text's own words."""
    a, b = local.split(), ci.split()
    na = [" ".join(words(t)) for t in a]
    nb = [" ".join(words(t)) for t in b]
    left, right = [], []
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        None, na, nb, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            left += [Token(t, "same") for t in a[i1:i2]]
            right += [Token(t, "same") for t in b[j1:j2]]
        else:
            left += [Token(t, "here") for t in a[i1:i2]]
            right += [Token(t, "there") for t in b[j1:j2]]
    return left, right
