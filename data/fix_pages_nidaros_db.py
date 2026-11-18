#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_pages_nidaros_db.py -- correct the page fields of `line` in nidaros.sqlite after the page-marker bug in
parse_breviarium.py (page markers inside rubric lines were ignored, so some rows kept the previous page).

What it does (dry run unless --apply):
  * reads the CORRECTED master (the regenerated .xlsx, or master.csv)
  * matches each database line to its master row through origin_ref "master_seq N"
  * GUARD: a line is touched only if its genre and standardized text still equal the master's;
    anything else is reported and left alone
  * fixes page_start, page_end, facsimile_ref, signature_leaf where they differ
  * renumbers line_on_page (10, 20, 30 ... in master order) on the pages that lost or gained rows
  * logs every change in edit_log (note: "page-marker fix")
  * with --apply: backs the database up first, runs in one transaction, then checks integrity

Nothing else is changed: decisions, candidates, flags, spot-checks, feast_map and your notes stay as they are.
Running it twice is harmless (the second run finds nothing to do).

Usage
  python fix_pages_nidaros_db.py nidaros.sqlite breviarium_andrew-lucy_pilot_master.xlsx            # dry run
  python fix_pages_nidaros_db.py nidaros.sqlite breviarium_andrew-lucy_pilot_master.xlsx --apply
"""

import sys, re, csv, shutil, sqlite3, argparse, datetime
from collections import defaultdict, Counter

NOTE = "page-marker fix (parse_breviarium.py)"


def read_master(path):
    if path.lower().endswith(".xlsx"):
        from openpyxl import load_workbook

        ws = load_workbook(path, data_only=True)["Master"]
        head = [str(c.value) if c.value is not None else "" for c in ws[1]]
        rows = [
            dict(zip(head, r))
            for r in ws.iter_rows(min_row=2, values_only=True)
            if any(v not in (None, "") for v in r)
        ]
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    out = {}
    for r in rows:
        seq = str(r.get("master_seq", "")).strip().replace(".0", "")
        if seq.isdigit():
            out[int(seq)] = r
    return out


def s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def to_int(v):
    v = s(v)
    return int(v) if v.isdigit() else None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("db")
    ap.add_argument("master")
    ap.add_argument(
        "--apply", action="store_true", help="write the changes (default: dry run)"
    )
    ap.add_argument(
        "--siglum",
        default=None,
        help="restrict to one source (source.siglum); default: all",
    )
    a = ap.parse_args(argv)

    master = read_master(a.master)
    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    q = "SELECT l.* FROM line l"
    args = []
    if a.siglum:
        q += " JOIN source so ON so.id = l.source_id WHERE so.siglum = ?"
        args.append(a.siglum)
    lines = con.execute(q, args).fetchall()

    changes = []  # (line_id, origin_ref, field, old, new)
    skipped = []  # (origin_ref, reason)
    newpage = {}  # line_id -> new page_start
    for ln in lines:
        m = re.match(r"^master_seq (\d+)$", ln["origin_ref"] or "")
        if not m or int(m.group(1)) not in master:
            skipped.append((ln["origin_ref"], "no matching master_seq"))
            continue
        mr = master[int(m.group(1))]
        if s(ln["genre"]) != s(mr.get("genre")) or s(ln["fulltext_standardized"]) != s(
            mr.get("fulltext_standardized")
        ):
            skipped.append(
                (ln["origin_ref"], "genre/text differ from the master; not touched")
            )
            continue
        want = {
            "page_start": to_int(mr.get("page_start")),
            "page_end": to_int(mr.get("page_end")),
            "facsimile_ref": s(mr.get("facsimile_ref")) or None,
            "signature_leaf": s(mr.get("signature_leaf")) or None,
        }
        for f, v in want.items():
            if f == "page_start" and v is None:
                continue
            if ln[f] != v:
                changes.append((ln["id"], ln["origin_ref"], f, ln[f], v))
        if want["page_start"] is not None:
            newpage[ln["id"]] = want["page_start"]

    moved = [c for c in changes if c[2] == "page_start"]
    # --- line_on_page renumbering on the pages that lost or gained rows
    touched_pages = set()
    for _id, _ref, _f, old, new in moved:
        touched_pages.update([old, new])
    by_id = {ln["id"]: ln for ln in lines}
    pages = defaultdict(list)  # page -> [(master_seq, line_id)]
    for ln in lines:
        m = re.match(r"^master_seq (\d+)$", ln["origin_ref"] or "")
        if not m:
            continue
        pg = newpage.get(ln["id"], ln["page_start"])
        if pg in touched_pages:
            pages[pg].append((int(m.group(1)), ln["id"]))
    renum = []  # (line_id, origin_ref, old, new)
    for pg, items in pages.items():
        for k, (seq, lid) in enumerate(sorted(items), 1):
            new = 10 * k
            if by_id[lid]["line_on_page"] != new:
                renum.append(
                    (lid, by_id[lid]["origin_ref"], by_id[lid]["line_on_page"], new)
                )

    # --- report
    print("database lines checked: %d   master rows: %d" % (len(lines), len(master)))
    if skipped:
        print("\nnot touched (%d):" % len(skipped))
        for ref, why in skipped[:20]:
            print("  %s: %s" % (ref, why))
    print("\npage_start corrections: %d lines" % len(moved))
    for (old, new), n in sorted(Counter((c[3], c[4]) for c in moved).items()):
        print("  page %s -> %s : %d lines" % (old, new, n))
    other = Counter(c[2] for c in changes if c[2] != "page_start")
    if other:
        print("other field corrections: %s" % dict(other))
    print(
        "line_on_page renumbered on pages %s: %d lines"
        % (sorted(touched_pages), len(renum))
    )
    if not changes and not renum:
        print("\nnothing to do: the database already matches the master.")
        return
    if not a.apply:
        print(
            "\ndry run: nothing written. Re-run with --apply to write the changes (a backup is made first)."
        )
        return

    # --- apply
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak-%s" % (a.db, stamp)
    con.commit()
    shutil.copy2(a.db, bak)
    print("\nbackup: %s" % bak)
    try:
        con.execute("BEGIN")
        # park renumbered rows on negative numbers first: UNIQUE(source_id, page_start, line_on_page)
        for lid, _ref, _old, _new in renum:
            con.execute("UPDATE line SET line_on_page = ? WHERE id = ?", (-lid, lid))
        for lid, ref, f, old, new in changes:
            con.execute("UPDATE line SET %s = ? WHERE id = ?" % f, (new, lid))
            con.execute(
                "INSERT INTO edit_log (line_id, field, old_value, new_value, note) VALUES (?,?,?,?,?)",
                (
                    lid,
                    f,
                    None if old is None else str(old),
                    None if new is None else str(new),
                    NOTE,
                ),
            )
        for lid, ref, old, new in renum:
            con.execute("UPDATE line SET line_on_page = ? WHERE id = ?", (new, lid))
            con.execute(
                "INSERT INTO edit_log (line_id, field, old_value, new_value, note) VALUES (?,?,?,?,?)",
                (lid, "line_on_page", str(old), str(new), NOTE),
            )
        con.commit()
    except Exception:
        con.rollback()
        print(
            "FAILED: rolled back; the database is unchanged (backup kept).",
            file=sys.stderr,
        )
        raise
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    n_view = (
        con.execute("SELECT COUNT(*) FROM line_export").fetchone()[0]
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='line_export'"
        ).fetchone()
        else None
    )
    print(
        "integrity_check: %s | foreign_key_check problems: %d | line_export rows: %s"
        % (ok, len(fk), n_view)
    )
    print(
        "done: %d field corrections and %d line_on_page renumberings logged in edit_log."
        % (len(changes), len(renum))
    )


if __name__ == "__main__":
    main()
