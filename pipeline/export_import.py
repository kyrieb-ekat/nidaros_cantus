#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_import.py -- build the Cantus Database import CSV from the (edited) master workbook.

The Master sheet is the source of truth: corrections you make there (office, text, genre, in_import ...)
are carried into the import file. `cantus_id` is filled ONLY where cid_decision is Confirm or Fixed:
  Confirm  -> the single CID left in cid_proposed (candidate scores in brackets are ignored)
  Fixed    -> the CID you typed into cid_proposed
  Reject / empty -> cantus_id stays blank
A Confirm/Fixed row whose cid_proposed holds no CID, or several, is NOT filled and is reported.

Usage
  python export_import.py master.xlsx --out import.csv [--source-id ID] [--seq-start 1] [--strict]
"""
import sys, argparse
from collections import Counter
from openpyxl import load_workbook
import parse_breviarium as P

def read_master(path, sheet='Master'):
    wb = load_workbook(path, data_only=True)
    ws = wb[sheet]
    head = [str(c.value) if c.value is not None else '' for c in ws[1]]
    rows = []
    for vals in ws.iter_rows(min_row=2, values_only=True):
        if all(v in (None, '') for v in vals):
            continue
        rows.append({h: v for h, v in zip(head, vals) if h})
    return head, rows

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('xlsx')
    ap.add_argument('--out', default='import.csv')
    ap.add_argument('--sheet', default='Master')
    ap.add_argument('--source-id', default='')
    ap.add_argument('--seq-start', type=int, default=1)
    ap.add_argument('--strict', action='store_true', help='exit with an error if any CID decision is unusable')
    a = ap.parse_args(argv)

    head, rows = read_master(a.xlsx, a.sheet)
    for need in ('in_import', 'fulltext_standardized', 'cid_proposed', 'cid_decision'):
        if need not in head:
            raise SystemExit('Master sheet lacks column: %s' % need)
    cfg = dict(source_id=a.source_id, seq_start=a.seq_start)
    imp, problems = P.import_rows_from_master(rows, cfg)
    P.write_csv(a.out, P.IMPORT_COLS, imp, enc='utf-8')

    dec = Counter(P._s(r.get('cid_decision')).lower() or '(none)' for r in rows if P._s(r.get('in_import')).lower() == 'yes')
    filled = sum(1 for r in imp if r['cantus_id'])
    undecided = sum(1 for r in rows if P._s(r.get('in_import')).lower() == 'yes'
                    and P._s(r.get('cid_proposed')) and not P._s(r.get('cid_decision')))
    print('wrote %d rows to %s' % (len(imp), a.out))
    print('cantus_id filled: %d   (decisions on import rows: %s)' % (filled, dict(dec)))
    print('proposed but not yet decided: %d' % undecided)
    bad = [(q, m) for q, m, ok in problems if not ok]
    warn = [(q, m) for q, m, ok in problems if ok]
    if warn:
        print('\nCIDs inserted with a warning (%d):' % len(warn))
        for seq, msg in warn:
            print('  master_seq %s: %s' % (seq, msg))
    if bad:
        print('\nCID decisions NOT applied (%d):' % len(bad))
        for seq, msg in bad:
            print('  master_seq %s: %s' % (seq, msg))
    if problems and a.strict:
        sys.exit(1)

if __name__ == '__main__':
    main()
