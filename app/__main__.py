"""Command line: python -m app <command> ...

  init         create a new, empty database
  add-source   register a book (e.g. the Breviarium)
  import       import a parser-generated master workbook for a book
  status       counts per book, for checking an import against its workbook
"""

from __future__ import annotations

import argparse
import sys

from . import db
from .importer import import_workbook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(db.DEFAULT_DB),
                        help=f"database file (default: {db.DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create a new, empty database")

    p = sub.add_parser("add-source", help="register a book")
    p.add_argument("siglum", help="short key, e.g. brev")
    p.add_argument("title", help='e.g. "Breviarium Nidrosiense"')
    p.add_argument("--edition", help="edition the text is taken from")
    p.add_argument("--cdb-source-id", type=int, help="Cantus Database source ID, once known")

    p = sub.add_parser("import", help="import a master workbook")
    p.add_argument("workbook")
    p.add_argument("--source", required=True, help="siglum of the book")

    p = sub.add_parser("status", help="show counts")
    p.add_argument("--source", help="limit to one book")

    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except db.DatabaseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_init(args) -> int:
    db.init(args.db)
    print(f"Created {args.db}")
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
    batches = conn.execute(
        "SELECT id, file_name, page_first, page_last, line_count FROM import_batch "
        "WHERE source_id = ? ORDER BY page_first", (sid,)).fetchall()
    for b in batches:
        print(f"  batch {b['id']}: {b['file_name']} (pp. {b['page_first']}-{b['page_last']}, "
              f"{b['line_count']} lines)")
    all_lines = one("SELECT COUNT(*) FROM line WHERE source_id = ?")
    import_rows = one("SELECT COUNT(*) FROM line_export WHERE source_id = ? AND in_import = 1")
    print(f"  {'all lines':<28}{all_lines}")
    print(f"  {'import rows':<28}{import_rows}")
    open_flag_lines = one("SELECT COUNT(DISTINCT f.line_id) FROM flag f "
                          "JOIN line l ON l.id = f.line_id "
                          "WHERE l.source_id = ? AND f.status = 'open'")
    print(f"  {'lines with an open flag':<28}{open_flag_lines}")
    table("lines by genre",
          "SELECT genre, COUNT(*) FROM line WHERE source_id = ? AND genre IS NOT NULL "
          "GROUP BY genre ORDER BY COUNT(*) DESC")
    table("import rows by office",
          "SELECT office, COUNT(*) FROM line_export WHERE source_id = ? AND in_import = 1 "
          "GROUP BY office ORDER BY COUNT(*) DESC")
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
    candidate_lines = one("SELECT COUNT(DISTINCT c.line_id) FROM candidate c "
                          "JOIN line l ON l.id = c.line_id WHERE l.source_id = ?")
    print(f"  {'lines with CID candidates':<28}{candidate_lines}")


COMMANDS = {
    "init": cmd_init,
    "add-source": cmd_add_source,
    "import": cmd_import,
    "status": cmd_status,
}

if __name__ == "__main__":
    sys.exit(main())
