"""Command line: python -m app <command> ...

Setting up
  init              create a new, empty database
  migrate           upgrade an existing database to the current schema
  add-source        register a book (e.g. the Breviarium)
  import            import a parser-generated master workbook for a book

Cantus Index
  feasts            list feast headings and their mapping, or search Cantus Index feasts
  map-feast         map a feast heading to a Cantus Index feast
  genre-map         set which Cantus Index genre codes match a local genre
  fetch             look up Cantus Index candidates for lines

Reviewing
  status            counts per book
  show              one line with its text, flags, candidates and evidence
  decide            record a decision for a line
  export-requests   write lines needing a variant or new Cantus ID to a CSV
  import-codes      record the Cantus IDs assigned in that CSV

Every command accepts --db PATH before the command name.
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from . import db
from .cantus import CantusError, CantusIndex, evidence
from .feasts import (STATUSES, ci_feasts, feast_headings, find_feast, map_feast,
                     mapped_feast_names, search_feasts)
from .fetch import comparison_text, fetch_lines, target_lines
from .importer import import_workbook
from .variants import export_requests, import_codes

DECISION_WORDS = {
    "confirm": "Confirm", "reject": "Reject", "fixed": "Fixed",
    "variant": "Variant", "new-cid": "New CID",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(db.DEFAULT_DB),
                        help=f"database file (default: {db.DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("init", help="create a new, empty database")
    sub.add_parser("migrate", help="upgrade an existing database to the current schema")

    p = sub.add_parser("add-source", help="register a book")
    p.add_argument("siglum", help="short key, e.g. brev")
    p.add_argument("title", help='e.g. "Breviarium Nidrosiense"')
    p.add_argument("--edition", help="edition the text is taken from")
    p.add_argument("--cdb-source-id", type=int, help="Cantus Database source ID, once known")

    p = sub.add_parser("import", help="import a master workbook")
    p.add_argument("workbook")
    p.add_argument("--source", required=True, help="siglum of the book")

    p = sub.add_parser("feasts", help="list feast headings, or search Cantus Index feasts")
    p.add_argument("--source", help="siglum (optional when there is only one book)")
    p.add_argument("--search", metavar="TERM", help="search Cantus Index feast names")
    p.add_argument("--refresh", action="store_true", help="fetch the feast list again")

    p = sub.add_parser("map-feast", help="map a feast heading to a Cantus Index feast")
    p.add_argument("heading", help="the feast heading exactly as in the book")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--ci", metavar="NAME", help="Cantus Index feast name")
    target.add_argument("--unmapped", action="store_true", help="no Cantus Index feast fits")
    p.add_argument("--variant", action="store_true",
                   help="the Cantus Index feast is the closest, not an exact match")
    p.add_argument("--note")
    p.add_argument("--source")

    p = sub.add_parser("genre-map", help="set the Cantus Index genre code(s) for a local genre")
    p.add_argument("genre", help="local genre code, e.g. HV")
    p.add_argument("ci_genre", nargs="?", help="Cantus Index code(s), comma-separated")
    p.add_argument("--clear", action="store_true", help="mark as not confirmed")

    p = sub.add_parser("fetch", help="look up Cantus Index candidates")
    p.add_argument("--source")
    p.add_argument("--feast", help="only this feast heading")
    p.add_argument("--line", type=int, action="append", metavar="ID", help="only this line (repeatable)")
    p.add_argument("--refresh", action="store_true", help="ignore the cache and fetch again")
    p.add_argument("--offline", action="store_true", help="use only cached responses")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between requests (default 1)")
    p.add_argument("--quiet", action="store_true", help="no per-line progress")

    p = sub.add_parser("status", help="show counts")
    p.add_argument("--source", help="limit to one book")

    p = sub.add_parser("show", help="show one line")
    p.add_argument("line_id", type=int)

    p = sub.add_parser("decide", help="record a decision for a line")
    p.add_argument("line_id", type=int)
    p.add_argument("decision", choices=list(DECISION_WORDS))
    p.add_argument("cid", nargs="?", help="Cantus ID (confirm, fixed)")
    p.add_argument("--parent", help="probable parent Cantus ID (variant; optional for new-cid)")
    p.add_argument("--reason", help="why (variant, new-cid)")
    p.add_argument("--note")

    p = sub.add_parser("export-requests", help="write Variant and New CID lines to a CSV")
    p.add_argument("path")
    p.add_argument("--source")

    p = sub.add_parser("import-codes", help="record assigned Cantus IDs from that CSV")
    p.add_argument("path")

    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (db.DatabaseError, CantusError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Everything finished so far is saved.", file=sys.stderr)
        return 130


# --------------------------------------------------------------------------
# Helpers

def _source(conn, siglum: str | None):
    if siglum:
        row = conn.execute("SELECT * FROM source WHERE siglum = ?", (siglum,)).fetchone()
        if row is None:
            raise db.DatabaseError(f"No source '{siglum}'.")
        return row
    rows = conn.execute("SELECT * FROM source").fetchall()
    if len(rows) != 1:
        raise db.DatabaseError("Several books are registered; choose one with --source." if rows
                               else "No books yet; add one with `python -m app add-source`.")
    return rows[0]


def _short(text: str | None, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _wrap(text: str | None, indent: str = "    ") -> str:
    return textwrap.fill(" ".join((text or "").split()), width=88,
                         initial_indent=indent, subsequent_indent=indent)


# --------------------------------------------------------------------------
# Setting up

def cmd_init(args) -> int:
    db.init(args.db)
    print(f"Created {args.db} (schema version {db.SCHEMA_VERSION})")
    return 0


def cmd_migrate(args) -> int:
    applied = db.migrate(args.db)
    if applied:
        print(f"Migrated {args.db} to schema version {applied[-1]} "
              f"(applied {', '.join(map(str, applied))}).")
    else:
        print(f"{args.db} is already at schema version {db.SCHEMA_VERSION}.")
    return 0


def cmd_add_source(args) -> int:
    conn = db.connect(args.db)
    with conn:
        if conn.execute("SELECT 1 FROM source WHERE siglum = ?", (args.siglum,)).fetchone():
            raise db.DatabaseError(f"Source '{args.siglum}' already exists.")
        conn.execute(
            "INSERT INTO source (siglum, title, edition, cdb_source_id) VALUES (?, ?, ?, ?)",
            (args.siglum, args.title, args.edition, args.cdb_source_id),
        )
    print(f"Added source '{args.siglum}': {args.title}")
    return 0


def cmd_import(args) -> int:
    conn = db.connect(args.db)
    report = import_workbook(conn, args.workbook, args.source)
    print(f"Imported batch {report.batch_id}: pages {report.pages[0]}-{report.pages[1]}")
    print(f"  lines           {report.lines}")
    print(f"  import rows     {report.import_rows}")
    print(f"  flags           {report.flags}")
    print("  links           " + ", ".join(f"{k} {v}" for k, v in report.links.items()))
    print(f"  spot-checks     {report.spot_checks}")
    print(f"  CID candidates  {report.candidates}")
    print(f"  CID decisions   {report.decisions}")
    print("  derived values (incipit, position, in_import, import_seq) match the workbook")
    if report.new_flag_types:
        print("  flag codes not in the README legend: " + ", ".join(report.new_flag_types))
    for warning in report.warnings:
        print(f"  warning: {warning}")
    return 0


# --------------------------------------------------------------------------
# Cantus Index

def cmd_feasts(args) -> int:
    conn = db.connect(args.db)
    if args.search:
        client = CantusIndex(conn, refresh=args.refresh)
        hits = search_feasts(ci_feasts(client), args.search)
        if not hits:
            print(f"No Cantus Index feast matches '{args.search}'.")
        for feast in hits[:40]:
            alts = f"  (also: {', '.join(feast.alt_names)})" if feast.alt_names else ""
            print(f"  {feast.name:<32} {feast.code or '':<10} {feast.date or '':<8}{alts}")
            if feast.description:
                print(_wrap(feast.description, "      "))
        if len(hits) > 40:
            print(f"  … and {len(hits) - 40} more; narrow the search.")
        return 0

    source = _source(conn, args.source)
    if args.refresh:
        ci_feasts(CantusIndex(conn, refresh=True))
    counts = {s: 0 for s in STATUSES}
    unmapped = 0
    for row in feast_headings(conn, source["id"]):
        if row["status"]:
            counts[row["status"]] += 1
            mapping = row["ci_feast"] or "(no Cantus Index feast)"
            print(f"  p.{row['first_page']:<5} {_short(row['feast'], 46):<47} "
                  f"{row['status']:<9} {mapping}")
        else:
            unmapped += 1
            print(f"  p.{row['first_page']:<5} {_short(row['feast'], 46):<47} not mapped")
    print(f"{source['siglum']}: exact {counts['exact']}, variant {counts['variant']}, "
          f"unmapped {counts['unmapped']}, not yet mapped {unmapped}")
    return 0


def cmd_map_feast(args) -> int:
    conn = db.connect(args.db)
    source = _source(conn, args.source)
    if args.unmapped:
        if args.variant:
            raise db.DatabaseError("--variant needs a Cantus Index feast (--ci).")
        map_feast(conn, source["id"], args.heading, "unmapped", None, args.note)
        print(f"'{args.heading}': no Cantus Index feast (flagged as unmapped)")
        return 0
    feast = find_feast(ci_feasts(CantusIndex(conn)), args.ci)
    status = "variant" if args.variant else "exact"
    map_feast(conn, source["id"], args.heading, status, feast, args.note)
    print(f"'{args.heading}' -> {feast.name} ({status})")
    return 0


def cmd_genre_map(args) -> int:
    conn = db.connect(args.db)
    if args.clear == bool(args.ci_genre):
        raise db.DatabaseError("Give Cantus Index genre code(s), or --clear.")
    value = None if args.clear else ",".join(g.strip() for g in args.ci_genre.split(",") if g.strip())
    with conn:
        changed = conn.execute("UPDATE genre SET ci_genre = ? WHERE code = ?",
                               (value, args.genre)).rowcount
    if not changed:
        raise db.DatabaseError(f"No local genre '{args.genre}'.")
    print(f"{args.genre} -> {value or 'not confirmed'}. Run `fetch --offline` to re-rank from the cache.")
    return 0


def cmd_fetch(args) -> int:
    conn = db.connect(args.db)
    source = _source(conn, args.source)
    lines = target_lines(conn, source["id"], args.feast, args.line)
    if not lines:
        print("No matching lines.")
        return 1
    client = CantusIndex(conn, min_interval=args.interval, refresh=args.refresh,
                         offline=args.offline)

    def progress(i, n, line, result):
        if args.quiet:
            return
        head = f"[{i}/{n}] line {line['id']} p.{line['page_start']} {line['genre']:<3} " \
               f"{_short(line['fulltext_standardized'], 34):<35}"
        if result.error:
            print(f"{head} ! {result.error}")
            return
        best = result.best
        found = f"{best.cid} {best.comparison.text_class}" if best else "no results"
        flags = f"  [{', '.join(code for code, _ in result.flags)}]" if result.flags else ""
        print(f"{head} -> {found}{flags}")

    summary = fetch_lines(conn, client, lines, progress)
    print(f"\n{summary.lines} lines, {client.requests} requests to Cantus Index.")
    for code, n in summary.flags.most_common():
        print(f"  {code:<20} {n}")
    for genre, seen in summary.unconfirmed_genres.items():
        found = ", ".join(f"{g} ({n})" for g, n in seen.most_common()) or "none yet"
        print(f"  genre {genre} is not mapped to Cantus Index; close matches had genre: {found}")
    if summary.errors:
        print(f"  {len(summary.errors)} lines failed; run the same command again to retry them:")
        for line_id, error in summary.errors[:10]:
            print(f"    line {line_id}: {error}")
    return 0 if not summary.errors else 2


# --------------------------------------------------------------------------
# Reviewing

def cmd_show(args) -> int:
    conn = db.connect(args.db)
    line = conn.execute("SELECT * FROM line_export WHERE id = ?", (args.line_id,)).fetchone()
    if line is None:
        raise db.DatabaseError(f"No line with id {args.line_id}.")
    mapping = conn.execute("SELECT ci_feast, status FROM feast_map WHERE source_id = ? AND feast = ?",
                           (line["source_id"], line["feast"])).fetchone()
    feast_note = f" -> {mapping['ci_feast'] or '(none)'} ({mapping['status']})" if mapping \
        else " (not mapped)"
    print(f"line {line['id']}  p.{line['page_start']} {line['signature_leaf'] or ''}  "
          f"{line['row_type']}  genre {line['genre'] or '-'}  office {line['office'] or '-'}  "
          f"position {line['position'] or '-'}")
    print(f"feast: {line['feast']}{feast_note}")
    text, is_cue = comparison_text(conn, line)
    print("standardized text:" + (" (cue only)" if is_cue else ""))
    print(_wrap(line["fulltext_standardized"]))
    if text != line["fulltext_standardized"]:
        print("compared with:")
        print(_wrap(text))

    flags = conn.execute("SELECT code, status, note FROM flag WHERE line_id = ? ORDER BY ord",
                         (line["id"],)).fetchall()
    if flags:
        print("flags:")
        for f in flags:
            print(f"  {f['code']}{' (cleared)' if f['status'] == 'cleared' else ''}"
                  f"{': ' + f['note'] if f['note'] else ''}")

    decision = conn.execute("SELECT * FROM current_decision WHERE line_id = ?", (line["id"],)).fetchone()
    if decision:
        extra = " ".join(x for x in (decision["cid"],
                                     f"parent {decision['parent_cid']}" if decision["parent_cid"] else None,
                                     f"- {decision['reason']}" if decision["reason"] else None) if x)
        print(f"decision: {decision['status']} {extra}  ({decision['decided_at']})")

    candidates = conn.execute("SELECT * FROM candidate WHERE line_id = ? ORDER BY method, rank",
                              (line["id"],)).fetchall()
    if not candidates:
        print("no candidates yet (run `python -m app fetch --line "
              f"{line['id']}`)")
        return 0
    cache = CantusIndex(conn, offline=True)
    try:
        known_feasts = ci_feasts(cache)
    except CantusError:
        known_feasts = []
    names = mapped_feast_names(conn, line["source_id"], line["feast"], known_feasts)
    print("candidates:")
    for c in candidates:
        print(f"  {c['rank']:>2}. {c['cid']:<12} genre {c['cand_genre'] or '?':<4} "
              f"{c['text_class'] or '-':<10} {c['score'] or 0:.2f}  {c['class_reason'] or ''}")
        print(_wrap(c["cand_text"], "        "))
        try:
            ev = evidence(c["cid"], cache.chant(c["cid"]), names, line["office"])
        except CantusError:
            ev = None
        if ev:
            dbs = ", ".join(f"{k} {v}" for k, v in sorted(ev.records.items(), key=lambda kv: -kv[1]))
            print(f"        Cantus Index feast: {ev.ci_feast or '-'};  CAO {ev.cao or '-'} "
                  f"{ev.cao_sigla or ''}")
            print(f"        concordances {ev.total}: {dbs}"
                  f"{'' if ev.complete else '  (incomplete: Cantus Index reports more)'}")
            if names:
                print(f"        same feast {sum(ev.same_feast.values())}, "
                      f"same feast and office {sum(ev.same_feast_office.values())}")
            if ev.related_cid:
                print(f"        related to {ev.related_cid}")
    return 0


def cmd_decide(args) -> int:
    conn = db.connect(args.db)
    status = DECISION_WORDS[args.decision]
    db.record_decision(conn, args.line_id, status, args.cid, args.note,
                       parent_cid=args.parent, reason=args.reason)
    detail = args.cid or (f"parent {args.parent}" if args.parent else "")
    print(f"line {args.line_id}: {status} {detail}".rstrip())
    return 0


def cmd_export_requests(args) -> int:
    conn = db.connect(args.db)
    source = _source(conn, args.source)
    n = export_requests(conn, source["id"], args.path)
    print(f"Wrote {n} request{'s' if n != 1 else ''} to {args.path}. "
          "Fill in assigned_cid, then run `python -m app import-codes` on the same file.")
    return 0


def cmd_import_codes(args) -> int:
    conn = db.connect(args.db)
    report = import_codes(conn, args.path)
    if report.problems:
        print("Nothing was recorded; please fix these rows first:")
        for problem in report.problems:
            print(f"  {problem}")
        return 1
    print(f"Recorded {report.recorded} assigned Cantus IDs "
          f"({report.already} already recorded, {report.blank} rows still blank).")
    return 0


def cmd_status(args) -> int:
    conn = db.connect(args.db)
    sources = conn.execute(
        "SELECT id, siglum, title FROM source WHERE ? IS NULL OR siglum = ? ORDER BY siglum",
        (args.source, args.source),
    ).fetchall()
    if not sources:
        print("No sources yet." if args.source is None else f"No source '{args.source}'.")
        return 0 if args.source is None else 1
    for src in sources:
        _print_status(conn, src)
    return 0


def _print_status(conn, src) -> None:
    sid = src["id"]

    def one(sql: str) -> int:
        return conn.execute(sql, (sid,)).fetchone()[0]

    def table(title: str, sql: str) -> None:
        rows = conn.execute(sql, (sid,)).fetchall()
        print(f"  {title}")
        if not rows:
            print("    (none)")
        for label, n in rows:
            print(f"    {label if label is not None else '(blank)':<26}{n}")

    print(f"{src['siglum']}: {src['title']}")
    for b in conn.execute(
            "SELECT id, file_name, page_first, page_last, line_count FROM import_batch "
            "WHERE source_id = ? ORDER BY page_first", (sid,)):
        print(f"  batch {b['id']}: {b['file_name']} (pp. {b['page_first']}-{b['page_last']}, "
              f"{b['line_count']} lines)")
    all_lines = one("SELECT COUNT(*) FROM line WHERE source_id = ?")
    import_rows = one("SELECT COUNT(*) FROM line_export WHERE source_id = ? AND in_import = 1")
    open_flag_lines = one("SELECT COUNT(DISTINCT f.line_id) FROM flag f "
                          "JOIN line l ON l.id = f.line_id "
                          "WHERE l.source_id = ? AND f.status = 'open'")
    looked_up = one("SELECT COUNT(DISTINCT c.line_id) FROM candidate c JOIN line l ON l.id = c.line_id "
                    "WHERE l.source_id = ? AND c.method = 'ci_text'")
    print(f"  {'all lines':<28}{all_lines}")
    print(f"  {'import rows':<28}{import_rows}")
    print(f"  {'lines with an open flag':<28}{open_flag_lines}")
    print(f"  {'looked up in Cantus Index':<28}{looked_up}")
    table("lines by genre",
          "SELECT genre, COUNT(*) FROM line WHERE source_id = ? AND genre IS NOT NULL "
          "GROUP BY genre ORDER BY COUNT(*) DESC")
    table("import rows by office",
          "SELECT office, COUNT(*) FROM line_export WHERE source_id = ? AND in_import = 1 "
          "GROUP BY office ORDER BY COUNT(*) DESC")
    table("feast headings by mapping",
          "SELECT COALESCE(m.status, 'not mapped'), COUNT(*) FROM "
          "(SELECT DISTINCT source_id, feast FROM line WHERE source_id = ? AND feast IS NOT NULL) h "
          "LEFT JOIN feast_map m ON m.source_id = h.source_id AND m.feast = h.feast "
          "GROUP BY 1 ORDER BY 1")
    table("open flags",
          "SELECT f.code, COUNT(*) FROM flag f JOIN line l ON l.id = f.line_id "
          "WHERE l.source_id = ? AND f.status = 'open' GROUP BY f.code ORDER BY COUNT(*) DESC")
    table("links",
          "SELECT k.kind, COUNT(*) FROM line_link k JOIN line l ON l.id = k.from_line "
          "WHERE l.source_id = ? GROUP BY k.kind ORDER BY k.kind")
    table("spot-check",
          "SELECT s.status, COUNT(*) FROM spot_check s JOIN line l ON l.id = s.line_id "
          "WHERE l.source_id = ? GROUP BY s.status ORDER BY s.status")
    table("CID decisions (current)",
          "SELECT d.status, COUNT(*) FROM current_decision d JOIN line l ON l.id = d.line_id "
          "WHERE l.source_id = ? GROUP BY d.status ORDER BY d.status")


COMMANDS = {
    "init": cmd_init,
    "migrate": cmd_migrate,
    "add-source": cmd_add_source,
    "import": cmd_import,
    "feasts": cmd_feasts,
    "map-feast": cmd_map_feast,
    "genre-map": cmd_genre_map,
    "fetch": cmd_fetch,
    "status": cmd_status,
    "show": cmd_show,
    "decide": cmd_decide,
    "export-requests": cmd_export_requests,
    "import-codes": cmd_import_codes,
}

if __name__ == "__main__":
    sys.exit(main())
