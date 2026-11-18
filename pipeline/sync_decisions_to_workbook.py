#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_decisions_to_workbook.py -- copy the CID decisions you already made in nidaros.sqlite into the master
workbook, so nothing has to be entered twice. One way only: the database is never changed.

For each line, matched through origin_ref "master_seq N" (the database's current decision is used):
  Confirm / Fixed -> cid_decision = Confirm / Fixed, cid_proposed = that CID (bare, as text)
  Variant         -> cid_decision = Variant,  cid_proposed = the parent CID (if the database has one)
  New CID         -> cid_decision = New CID   (cid_proposed left as it is)
  Reject          -> cid_decision = Reject    (cid_proposed left as it is)
Only Confirm and Fixed ever produce a cantus_id (export_import.py). Variant, New CID and Reject are carried
over as statuses so the workbook matches the database.

Safeguards
  * GUARD: a row is filled only if the database line and the workbook row have the same genre and standardized
    text; otherwise it is reported and skipped.
  * A cid_decision already in the workbook that differs from the database is NOT overwritten (reported),
    unless --overwrite is given.
  * The default output is a new file (<name>_synced.xlsx); --in-place keeps a .bak.xlsx copy first.
  * The Master dropdown for cid_decision is extended with Variant and New CID (older workbooks included).

Usage
  python sync_decisions_to_workbook.py nidaros.sqlite WORKBOOK.xlsx --dry-run
  python sync_decisions_to_workbook.py nidaros.sqlite WORKBOOK.xlsx            # writes WORKBOOK_synced.xlsx
  python sync_decisions_to_workbook.py nidaros.sqlite WORKBOOK.xlsx --in-place
"""
import re, sys, shutil, sqlite3, argparse
from collections import Counter
from openpyxl import load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

STATUSES = ['Confirm', 'Reject', 'Fixed', 'Variant', 'New CID']

def s(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()

def read_db(path, siglum):
    con = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    con.row_factory = sqlite3.Row
    args = []
    if siglum:
        q = ('SELECT l.origin_ref, l.genre, l.fulltext_standardized, d.status, d.cid, d.parent_cid '
             'FROM line l JOIN source so ON so.id = l.source_id '
             'LEFT JOIN current_decision d ON d.line_id = l.id WHERE so.siglum = ?')
        args.append(siglum)
    else:
        q = ('SELECT l.origin_ref, l.genre, l.fulltext_standardized, d.status, d.cid, d.parent_cid '
             'FROM line l LEFT JOIN current_decision d ON d.line_id = l.id')
    rows = con.execute(q, args).fetchall()
    imported = {r[0] for r in con.execute('SELECT code FROM genre WHERE imported = 1')}
    con.close()
    return rows, imported

def extend_dropdown(ws, col_letter, last_row):
    """make sure the cid_decision dropdown offers all five statuses (older workbooks only had three)"""
    want = '"%s"' % ','.join(STATUSES)
    found = False
    for dv in ws.data_validations.dataValidation:
        if any(col_letter in str(rng) for rng in dv.sqref.ranges):
            dv.formula1 = want
            found = True
    if not found:
        dv = DataValidation(type='list', formula1=want, allow_blank=True)
        ws.add_data_validation(dv)
        dv.add('%s2:%s%d' % (col_letter, col_letter, last_row))
    # colours for the two new statuses (added once)
    rng = '%s2:%s%d' % (col_letter, col_letter, last_row)
    have = ' '.join(str(r.formula) for cf in ws.conditional_formatting for r in cf.rules)
    if 'Variant' not in have:
        ws.conditional_formatting.add(rng, FormulaRule(formula=['$%s2="Variant"' % col_letter],
                                      fill=PatternFill('solid', bgColor='FFE699', fgColor='FFE699')))
    if 'New CID' not in have:
        ws.conditional_formatting.add(rng, FormulaRule(formula=['$%s2="New CID"' % col_letter],
                                      fill=PatternFill('solid', bgColor='BDD7EE', fgColor='BDD7EE')))

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('db')
    ap.add_argument('xlsx')
    ap.add_argument('--out', default=None)
    ap.add_argument('--in-place', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--overwrite', action='store_true', help='replace a different cid_decision already in the workbook')
    ap.add_argument('--siglum', default=None)
    ap.add_argument('--sheet', default='Master')
    a = ap.parse_args(argv)

    db_rows, imported_genres = read_db(a.db, a.siglum)
    wb = load_workbook(a.xlsx)
    ws = wb[a.sheet]
    head = {str(c.value): i + 1 for i, c in enumerate(ws[1]) if c.value}
    for need in ('master_seq', 'genre', 'fulltext_standardized', 'cid_proposed', 'cid_decision'):
        if need not in head:
            raise SystemExit('%s sheet lacks column: %s' % (a.sheet, need))
    row_of = {}
    for r in range(2, ws.max_row + 1):
        v = s(ws.cell(r, head['master_seq']).value)
        if v.isdigit():
            row_of[int(v)] = r

    done = Counter(); same = Counter(); conflicts = []; skipped = []; undecided = []; missing = []
    cids_set = 0
    for d in db_rows:
        m = re.match(r'^master_seq (\d+)$', d['origin_ref'] or '')
        if not m:
            continue
        seq = int(m.group(1))
        status = d['status']
        if not status:
            if d['genre'] in imported_genres:
                undecided.append(seq)
            continue
        r = row_of.get(seq)
        if r is None:
            missing.append(seq)
            continue
        if s(ws.cell(r, head['genre']).value) != s(d['genre']) or \
                s(ws.cell(r, head['fulltext_standardized']).value) != s(d['fulltext_standardized']):
            skipped.append(seq)
            continue
        cur = s(ws.cell(r, head['cid_decision']).value)
        cid = s(d['cid'])
        parent = s(d['parent_cid'])
        want_prop = cid if status in ('Confirm', 'Fixed') else (parent if status == 'Variant' else None)
        cur_prop = s(ws.cell(r, head['cid_proposed']).value)
        if cur and not a.overwrite:
            if cur == status and (want_prop is None or want_prop == cur_prop):
                same[status] += 1
            else:
                conflicts.append((seq, cur, cur_prop, status, want_prop or ''))
            continue
        ws.cell(r, head['cid_decision']).value = status
        if want_prop:
            c = ws.cell(r, head['cid_proposed'])
            c.value = want_prop
            c.number_format = '@'              # keep leading zeros (e.g. 003058)
            if status in ('Confirm', 'Fixed'):
                cids_set += 1
        done[status] += 1

    print('database lines: %d   workbook rows: %d' % (len(db_rows), len(row_of)))
    print('decisions copied to the workbook: %s  (total %d)' % (dict(done), sum(done.values())))
    if same:
        print('already identical in the workbook: %s' % dict(same))
    print('CIDs written to cid_proposed (Confirm + Fixed): %d' % cids_set)
    if undecided:
        print('import lines with no decision in the database (%d): master_seq %s' % (len(undecided), ', '.join(map(str, sorted(undecided)))))
    if conflicts:
        print('\nNOT overwritten: the workbook already has a different decision (%d):' % len(conflicts))
        for seq, cur, cp, st, wp in conflicts[:25]:
            print('  master_seq %d: workbook "%s" (%s) vs database "%s" (%s)' % (seq, cur, cp, st, wp))
        print('  (use --overwrite to take the database values)')
    if skipped:
        print('\nskipped, genre/text differ between database and workbook (%d): master_seq %s' % (len(skipped), ', '.join(map(str, skipped[:25]))))
    if missing:
        print('\nin the database but not in the workbook (%d): master_seq %s' % (len(missing), ', '.join(map(str, missing[:25]))))

    if a.dry_run:
        print('\ndry run: nothing written.')
        return
    extend_dropdown(ws, get_column_letter(head['cid_decision']), max(ws.max_row, 2))
    wb.calculation.fullCalcOnLoad = True
    if a.in_place:
        bak = re.sub(r'\.xlsx$', '', a.xlsx) + '.bak.xlsx'
        shutil.copy2(a.xlsx, bak)
        out = a.xlsx
        print('\nbackup: %s' % bak)
    else:
        out = a.out or (re.sub(r'\.xlsx$', '', a.xlsx) + '_synced.xlsx')
    wb.save(out)
    print('saved: %s' % out)

if __name__ == '__main__':
    main()
