#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_xlsx.py -- turns the parser output (master.csv, import.csv, std_vocab.json)
into a review workbook:  README | Summary | Master | Import (template) | Spot-check | Std changes
Usage:  python build_xlsx.py OUTDIR  OUT.xlsx  [--label "Breviarium Nidrosiense, pilot: St Andrew -> Lucy"]
"""
import sys, json, csv, argparse
from collections import OrderedDict
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import FormulaRule

import parse_breviarium as P

FONT = 'Arial'
HDR_FILL = PatternFill('solid', fgColor='1F3864')
GREY = PatternFill('solid', fgColor='EDEDED')
AMBER = PatternFill('solid', fgColor='FFF2CC')
thin = Side(style='thin', color='BFBFBF')

def f(bold=False, color='000000', size=10, italic=False):
    return Font(name=FONT, bold=bold, color=color, size=size, italic=italic)

def style_header(ws, ncols, row=1):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = f(True, 'FFFFFF')
        cell.fill = HDR_FILL
        cell.alignment = Alignment(vertical='center', wrap_text=True)
    ws.freeze_panes = ws.cell(row=row + 1, column=1)

def read_csv(path):
    with open(path, newline='', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))

WIDTHS = {'master_seq': 8, 'row_type': 16, 'in_import': 8, 'import_seq': 8, 'page_start': 8, 'page_end': 8,
          'facsimile_ref': 9, 'signature_leaf': 10, 'content_structure': 16, 'feast': 30, 'sub_section': 24,
          'office': 7, 'office_printed': 18, 'nocturn': 7, 'slot': 14, 'genre': 7, 'position': 8, 'sign': 5,
          'incipit': 34, 'fulltext_edition': 60, 'fulltext_ms': 60, 'fulltext_standardized': 60, 'rubric': 40,
          'xref_raw': 18, 'xref_leaf': 11, 'incipit_only': 9, 'xref_resolved_page': 11, 'xref_resolved_text': 50,
          'repetendum_note': 30, 'variant_notes': 30, 'indexing_notes': 40, 'extra_note': 40, 'review_flags': 34, 'cid_proposed': 26,
          'cid_candidate_text': 50, 'cid_decision': 13}

COLUMN_DOC = OrderedDict([
    ('master_seq', 'Running number of every line in this section (chants, rubrics, readings, prayers, headings).'),
    ('row_type', 'What the line is: antiphon, responsory, responsory_verse, versicle, hymn, hymn_verse, hymn_refrain, invitatory, capitulum, prayer, lesson, homily, psalm_cue, office_rubric, nocturn_rubric, commemoration_rubric, rubric, rubric_mixed, heading, text_orphan.'),
    ('in_import', '"yes" if the row goes into the Cantus import CSV (chant genres only unless run with --include-prayers).'),
    ('import_seq', 'Value written to the template column "sequence" (global running order of import rows; assumption).'),
    ('page_start / page_end', 'Edition page. The sample facsimile images use the same numbers (s490, s530 ...); to be confirmed against the full facsimile. page_end is filled only when the row runs onto the next page.'),
    ('facsimile_ref', 's + page number, e.g. s554 (naming pattern of the sample images; to be confirmed).'),
    ('signature_leaf', 'Printer\'s signature and side, e.g. "ee.j. r" (recto) / "ee.j. v" (verso). Derived from the page markers; signatures are printed on rectos only.'),
    ('feast / sub_section', 'Feast heading as printed, or a supplied heading in [brackets] where the edition prints none (flag FEAST_SUPPLIED): the feast proper after a vigil, or an office after Second Vespers. sub_section = the commemoration rubric governing the item, if any.'),
    ('office / office_printed / nocturn / slot', 'Cantus service code (V, V2, M, L, P, T, S, N, C, D), the rubric as printed, nocturn 1-3, and slot (super psalmos, ad Magnificat, ad Benedictus, in evangelio).'),
    ('genre / position / sign', 'Cantus genre code (A, R, V = responsory verse, W = versicle, H, HV, I, Cap, Or, Co ...), order within feast + office, and the sign printed in the source.'),
    ('incipit', 'First six words of the standardized text.'),
    ('fulltext_edition', 'Text exactly as in the 2019 edition (punctuation, quotation marks, editorial brackets).'),
    ('fulltext_ms', 'The print\'s own reading (edition text with endnote variants applied; editorial supplied letters removed; punctuation removed). A vertical stroke marks the start of a responsory repetendum.'),
    ('fulltext_standardized', 'PROVISIONAL standardized spelling (see Std changes sheet). Incipit-only items get the trailing asterisk in the import file.'),
    ('rubric', 'All rubric (red) text belonging to the line, in order, separated by " | ". Goes to "extra" in the import file.'),
    ('xref_raw / xref_leaf / incipit_only', 'Cross-reference as printed ("vt supra ..."), leaf references parsed from it (e.g. aa.v.), and whether the line is only an incipit.'),
    ('xref_resolved_page / xref_resolved_text', 'Where the full text of an incipit-only item was found (by leaf + incipit, or earlier in the same feast). Never copied into fulltext_ms.'),
    ('repetendum_note / variant_notes', 'How the repetendum was located; endnote variants applied (edition reading ] print reading).'),
    ('indexing_notes / review_flags', 'Free notes; machine flags (see legend below).'),
    ('extra_note', 'Editorial note for the import file: added to "extra" after the rubric (e.g. that the feast heading is supplied).'),
    ('cid_proposed', 'Candidates written by enrich_cids.py as "CID (score); CID (score)". To confirm one, leave a single CID; to correct, type the right CID. Scores are ignored on export.'),
    ('cid_candidate_text', 'Text of the best candidate, for comparison with fulltext_standardized.'),
    ('cid_decision', 'Confirm / Reject / Fixed. Only Confirm and Fixed put the CID from cid_proposed into cantus_id of the import file.'),
])

DECISIONS = [
    ('Rubrics', 'Own column in the master; "extra" in the import file.', 'Kyrie (default confirmed)'),
    ('Non-chant lines', 'Kept in the master (lessons, homilies, psalm cues, orations, capitula, rubrics); import file = chants only (A, R, V, W, H, HV, I).', 'Kyrie (default confirmed)'),
    ('Cantus IDs', 'Never assigned automatically. enrich_cids.py proposes; cantus_id in the import file is filled only where cid_decision = Confirm or Fixed.', 'Kyrie'),
    ('Genre codes', 'A antiphon, R responsory, V responsory verse, W versicle, H hymn, HV hymn verse, I invitatory; Cap / Or / Co tagged but not imported.', 'Checked against cantusdatabase.org/genres'),
    ('Service codes', 'V first vespers, V2 second vespers, M, L, P, T, S, N, C, D. "Ad vesperas" without primas/secundas: V2 after Lauds or a day hour of the same feast, otherwise V; both flagged.', 'Checked against cantusdatabase.org/services'),
    ('Supplied feast headings', 'After a vigil heading, the first Vespers starts the feast proper; Matins after Second Vespers starts a further part. Each gets a [bracketed] heading taken from the printed one; the Cantus feast name is assigned when mapping feasts in the app.', 'Kyrie'),
    ('Responsory verses', 'Separate V rows following their R (CDB genre V = responsory verse).', 'Assumed from CDB genre list'),
    ('Hymns', 'Stanza 1 = H row, later stanzas = HV rows; short refrain lines kept as hymn_refrain (master only). Confirm against the CDB hymn data-entry appendix.', 'Assumed'),
    ('folio', 'Edition page number; facsimile page is the same number (sample images). Signature/leaf in the master.', 'Assumed, please confirm'),
    ('sequence', 'Global running order of import rows (--seq-start sets the first number).', 'Assumed'),
    ('position', 'Order within feast + office (nocturns continue the count).', 'Assumed'),
    ('fulltext_ms', 'Print reading from the edition endnotes; no punctuation; | marks repetendum; supplied letters removed.', 'Assumed from CDB records and Graduals guide'),
    ('fulltext_standardized', 'Provisional: u/v, i/j, michi>mihi, nichil>nihil only. ae/oe are NOT restored; capitals unchanged. Asterisk added to incipit-only items. Must be proofread against the CDB text-entry guidelines (not available to the parser).', 'Provisional'),
    ('source_id', 'Left blank unless --source-id is given.', 'To supply'),
]

def build(outdir, out_xlsx, label):
    master = read_csv('%s/master.csv' % outdir)
    imp = read_csv('%s/import.csv' % outdir)
    voc = json.load(open('%s/std_vocab.json' % outdir, encoding='utf-8'))
    cols = P.MASTER_COLS
    ci = {c: i + 1 for i, c in enumerate(cols)}
    L = {c: get_column_letter(i) for c, i in ci.items()}
    n = len(master)
    last = n + 1

    wb = Workbook()
    # ------------------------------------------------------------------ README
    ws = wb.active
    ws.title = 'README'
    ws.column_dimensions['A'].width = 34
    ws.column_dimensions['B'].width = 110
    ws.column_dimensions['C'].width = 34
    ws['A1'] = label
    ws['A1'].font = f(True, size=14)
    ws['A2'] = ('Source: Nasjonalbiblioteket (2019) HTML edition of the Nidaros breviary (printed 1519). '
                'Generated by parse_breviarium.py + build_xlsx.py. Nothing here is a Cantus ID attribution.')
    ws['A2'].font = f(italic=True)
    ws['A3'] = 'Workflow'
    ws['A3'].font = f(True, size=11)
    steps = ['1. Fix rows in Master (the Master sheet is the source of truth; use Spot-check to track what you have looked at).',
             '2. Propose Cantus IDs (run locally):  python enrich_cids.py <this file>.xlsx   (makes a .bak copy first)',
             '3. Review cid_proposed / cid_candidate_text and set cid_decision to Confirm, Reject or Fixed.',
             '4. Export:  python export_import.py <this file>.xlsx --out import.csv --source-id <ID>   (reports unusable decisions)']
    for k, t in enumerate(steps):
        ws.cell(4 + k, 1, t).font = f()
    r = 9
    ws.cell(r, 1, 'Decisions and assumptions').font = f(True, size=11)
    r += 1
    for h, c in enumerate(('Topic', 'What the pilot does', 'Status'), 1):
        ws.cell(r, h, c)
    style_header(ws, 3, r)
    ws.freeze_panes = None
    for t, d, s in DECISIONS:
        r += 1
        ws.cell(r, 1, t).font = f(True)
        ws.cell(r, 2, d).font = f()
        ws.cell(r, 3, s).font = f()
        for c in (1, 2, 3):
            ws.cell(r, c).alignment = Alignment(wrap_text=True, vertical='top')
    r += 2
    ws.cell(r, 1, 'Master columns').font = f(True, size=11)
    r += 1
    ws.cell(r, 1, 'Column'); ws.cell(r, 2, 'Meaning')
    style_header(ws, 2, r)
    ws.freeze_panes = None
    for k, d in COLUMN_DOC.items():
        r += 1
        ws.cell(r, 1, k).font = f(True)
        ws.cell(r, 2, d).font = f()
        for c in (1, 2):
            ws.cell(r, c).alignment = Alignment(wrap_text=True, vertical='top')
    r += 2
    ws.cell(r, 1, 'Review flags (live counts from the Master sheet)').font = f(True, size=11)
    r += 1
    ws.cell(r, 1, 'Flag'); ws.cell(r, 2, 'Meaning'); ws.cell(r, 3, 'Rows')
    style_header(ws, 3, r)
    ws.freeze_panes = None
    for k, d in P.FLAG_HELP.items():
        r += 1
        ws.cell(r, 1, k).font = f(True)
        ws.cell(r, 2, d).font = f()
        ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical='top')
        ws.cell(r, 3, '=COUNTIF(Master!$%s$2:$%s$%d,"*%s*")' % (L['review_flags'], L['review_flags'], last, k)).font = f()

    # ------------------------------------------------------------------ Summary
    sm = wb.create_sheet('Summary')
    sm.column_dimensions['A'].width = 38
    sm.column_dimensions['B'].width = 12
    sm['A1'] = 'Counts (formulas over the Master sheet)'
    sm['A1'].font = f(True, size=12)
    rng = lambda c: 'Master!$%s$2:$%s$%d' % (L[c], L[c], last)
    items = [('All lines', '=COUNTA(%s)' % rng('master_seq')),
             ('Rows in the import file', '=COUNTIF(%s,"yes")' % rng('in_import')),
             ('Rows with at least one review flag', '=COUNTIF(%s,"?*")' % rng('review_flags')),
             ('Chants: antiphons (A)', '=COUNTIF(%s,"A")' % rng('genre')),
             ('Chants: responsories (R)', '=COUNTIF(%s,"R")' % rng('genre')),
             ('Chants: responsory verses (V)', '=COUNTIF(%s,"V")' % rng('genre')),
             ('Chants: versicles (W)', '=COUNTIF(%s,"W")' % rng('genre')),
             ('Chants: hymns (H) + hymn verses (HV)', '=COUNTIF(%s,"H")+COUNTIF(%s,"HV")' % (rng('genre'), rng('genre'))),
             ('Chants: invitatories (I)', '=COUNTIF(%s,"I")' % rng('genre')),
             ('Prayers and capitula tagged (Cap, Or, Co)',
              '=COUNTIF(%s,"Cap")+COUNTIF(%s,"Or")+COUNTIF(%s,"Co")' % (rng('genre'), rng('genre'), rng('genre'))),
             ('Incipit-only items', '=COUNTIF(%s,"yes")' % rng('incipit_only')),
             ('Lessons + homilies (master only)',
              '=COUNTIF(%s,"lesson")+COUNTIF(%s,"homily")' % (rng('row_type'), rng('row_type')))]
    sm['A3'] = 'Measure'; sm['B3'] = 'Rows'
    style_header(sm, 2, 3)
    sm.freeze_panes = None
    for i, (lab, fm) in enumerate(items, 4):
        sm.cell(i, 1, lab).font = f()
        sm.cell(i, 2, fm).font = f()
    base = 4 + len(items) + 1
    sm.cell(base, 1, 'Import rows by office').font = f(True)
    for j, code in enumerate(['V', 'V2', 'M', 'L', 'P', 'T', 'S', 'N', 'C', 'D', '']):
        sm.cell(base + 1 + j, 1, code if code else '(blank: not stated)').font = f()
        crit = '"%s"' % code if code else '""'
        sm.cell(base + 1 + j, 2, '=COUNTIFS(%s,"yes",%s,%s)' % (rng('in_import'), rng('office'), crit)).font = f()

    # ------------------------------------------------------------------ Master
    ms = wb.create_sheet('Master')
    for j, c in enumerate(cols, 1):
        ms.cell(1, j, c)
        ms.column_dimensions[get_column_letter(j)].width = WIDTHS.get(c, 14)
    style_header(ms, len(cols))
    for i, rec in enumerate(master, 2):
        for j, c in enumerate(cols, 1):
            v = rec.get(c, '')
            if c in ('master_seq', 'import_seq', 'page_start', 'page_end', 'xref_resolved_page') and v.isdigit():
                v = int(v)
            cell = ms.cell(i, j, v)
            cell.font = f()
            cell.alignment = Alignment(vertical='top')
            if rec.get('in_import') != 'yes':
                cell.fill = GREY
        if rec.get('review_flags'):
            ms.cell(i, ci['review_flags']).fill = AMBER
    ms.auto_filter.ref = 'A1:%s%d' % (get_column_letter(len(cols)), last)
    ms.freeze_panes = 'D2'
    dcol = L['cid_decision']
    dvm = DataValidation(type='list', formula1='"Confirm,Reject,Fixed"', allow_blank=True)
    ms.add_data_validation(dvm)
    dvm.add('%s2:%s%d' % (dcol, dcol, last))
    ms.conditional_formatting.add('%s2:%s%d' % (dcol, dcol, last), FormulaRule(
        formula=['OR($%s2="Confirm",$%s2="Fixed")' % (dcol, dcol)], fill=PatternFill('solid', bgColor='C6E0B4', fgColor='C6E0B4')))
    ms.conditional_formatting.add('%s2:%s%d' % (dcol, dcol, last), FormulaRule(
        formula=['$%s2="Reject"' % dcol], fill=PatternFill('solid', bgColor='F4B6B6', fgColor='F4B6B6')))

    # ------------------------------------------------------------------ Import
    im = wb.create_sheet('Import (template)')
    for j, c in enumerate(P.IMPORT_COLS, 1):
        im.cell(1, j, c)
        im.column_dimensions[get_column_letter(j)].width = 14
    for c, w in (('fulltext_standardized', 60), ('fulltext_ms', 60), ('indexing_notes', 50), ('extra', 40),
                 ('feast', 30), ('incipit', 32), ('content structure', 18)):
        im.column_dimensions[get_column_letter(P.IMPORT_COLS.index(c) + 1)].width = w
    style_header(im, len(P.IMPORT_COLS))
    for i, rec in enumerate(imp, 2):
        for j, c in enumerate(P.IMPORT_COLS, 1):
            v = rec.get(c, '')
            if c in ('sequence', 'folio') and str(v).isdigit():
                v = int(v)
            im.cell(i, j, v).font = f()
    im.auto_filter.ref = 'A1:%s%d' % (get_column_letter(len(P.IMPORT_COLS)), len(imp) + 1)

    # ------------------------------------------------------------------ Spot-check
    sc = wb.create_sheet('Spot-check')
    heads = ['master_seq', 'page', 'facsimile_ref', 'signature_leaf', 'genre', 'office', 'incipit',
             'governing rubric (commemoration)', 'review_flags', 'why check this row', 'status', 'your note / fix']
    for j, h in enumerate(heads, 1):
        sc.cell(1, j, h)
    for j, w in enumerate((10, 7, 12, 12, 7, 8, 40, 34, 30, 52, 16, 50), 1):
        sc.column_dimensions[get_column_letter(j)].width = w
    style_header(sc, len(heads))
    picks = []
    def pick(pred, why, k=2):
        got = 0
        for rec in master:
            if rec['master_seq'] in [p[0]['master_seq'] for p in picks]:
                continue
            if pred(rec):
                picks.append((rec, why)); got += 1
                if got >= k:
                    break
    feasts = []
    for rec in master:
        if rec['feast'] and rec['feast'] not in feasts and rec['genre'] in ('A', 'R'):
            feasts.append(rec['feast'])
    for fe in feasts:
        pick(lambda r_, fe=fe: r_['feast'] == fe and r_['genre'] == 'A', 'first antiphon of "%s": feast, office, text' % fe, 1)
    pick(lambda r_: r_['genre'] == 'R' and ' | ' in r_['fulltext_ms'], 'repetendum stroke placement', 3)
    pick(lambda r_: 'REPETENDUM_NOT_FOUND' in r_['review_flags'], 'repetendum not located', 2)
    pick(lambda r_: 'XREF_LEAF_RESOLVED' in r_['review_flags'], 'incipit-only item resolved by leaf: check the match', 3)
    pick(lambda r_: 'XREF_LEAF_AMBIGUOUS' in r_['review_flags'], 'several candidates on the cited leaf', 2)
    pick(lambda r_: 'XREF_INTERNAL_RESOLVED' in r_['review_flags'], 'incipit-only item resolved within the feast', 2)
    pick(lambda r_: 'PAGE_BREAK_IN_ROW' in r_['review_flags'], 'row spans a page break', 2)
    pick(lambda r_: 'SUPPLIED_LETTERS' in r_['review_flags'], 'editorial supplied letters', 2)
    pick(lambda r_: r_['variant_notes'] and 'UNRESOLVED' not in r_['variant_notes'], 'endnote variant applied to print reading', 3)
    pick(lambda r_: 'OFFICE_INFERRED' in r_['review_flags'], 'office inferred from the invitatory', 1)
    pick(lambda r_: 'OFFICE_V_UNSPEC' in r_['review_flags'], '"Ad vesperas" coded V', 1)
    pick(lambda r_: 'NO_OFFICE' in r_['review_flags'], 'no hour rubric before this item', 2)
    pick(lambda r_: r_['row_type'] == 'hymn_verse', 'hymn stanza entered as HV', 1)
    pick(lambda r_: r_['row_type'] == 'rubric_mixed', 'line mixing rubric and text', 2)
    pick(lambda r_: r_['row_type'] == 'text_orphan', 'text without heading', 2)
    picks.sort(key=lambda p: int(p[0]['master_seq']))
    for i, (rec, why) in enumerate(picks, 2):
        seq = int(rec['master_seq'])
        vals = [seq, int(rec['page_start']) if rec['page_start'].isdigit() else rec['page_start'],
                rec['facsimile_ref'], rec['signature_leaf'], rec['genre'], rec['office'],
                rec['incipit'] or rec['fulltext_edition'][:60], rec['sub_section'], rec['review_flags'], why,
                'To check', '']
        for j, v in enumerate(vals, 1):
            c = sc.cell(i, j, v)
            c.font = f()
            c.alignment = Alignment(vertical='top', wrap_text=(j in (7, 8, 9, 10, 12)))
        link = sc.cell(i, 1)
        link.hyperlink = "#'Master'!A%d" % (seq + 1)
        link.font = Font(name=FONT, size=10, color='0563C1', underline='single')
    nlast = len(picks) + 1
    dv = DataValidation(type='list', formula1='"To check,OK,Fixed in Master,Needs discussion"', allow_blank=False)
    sc.add_data_validation(dv)
    dv.add('K2:K%d' % nlast)
    sc.conditional_formatting.add('K2:K%d' % nlast, FormulaRule(formula=['OR($K2="OK",$K2="Fixed in Master")'],
                                  fill=PatternFill('solid', bgColor='C6E0B4', fgColor='C6E0B4')))
    sc.conditional_formatting.add('K2:K%d' % nlast, FormulaRule(formula=['$K2="Needs discussion"'],
                                  fill=PatternFill('solid', bgColor='FFE699', fgColor='FFE699')))
    sc.auto_filter.ref = 'A1:L%d' % nlast
    # CID decision tally + status tally on the Summary sheet
    drow = base + 14
    sm.cell(drow, 1, 'Cantus ID decisions (import rows)').font = f(True)
    for k, lab in enumerate(['Confirm', 'Fixed', 'Reject']):
        sm.cell(drow + 1 + k, 1, lab).font = f()
        sm.cell(drow + 1 + k, 2, '=COUNTIFS(%s,"yes",%s,"%s")' % (rng('in_import'), rng('cid_decision'), lab)).font = f()
    sm.cell(drow + 4, 1, 'Proposed, not yet decided').font = f()
    sm.cell(drow + 4, 2, '=COUNTIFS(%s,"yes",%s,"?*",%s,"")' % (rng('in_import'), rng('cid_proposed'), rng('cid_decision'))).font = f()
    srow = drow + 7
    sm.cell(srow, 1, 'Spot-check progress').font = f(True)
    for k, lab in enumerate(['To check', 'OK', 'Fixed in Master', 'Needs discussion']):
        sm.cell(srow + 1 + k, 1, lab).font = f()
        sm.cell(srow + 1 + k, 2, "=COUNTIF('Spot-check'!$K$2:$K$%d,\"%s\")" % (nlast, lab)).font = f()

    # ------------------------------------------------------------------ Std changes
    sd = wb.create_sheet('Std changes')
    for j, h in enumerate(('edition form', 'standardized form', 'occurrences'), 1):
        sd.cell(1, j, h)
        sd.column_dimensions[get_column_letter(j)].width = 26
    style_header(sd, 3)
    for i, (a, b, c) in enumerate(voc, 2):
        sd.cell(i, 1, a).font = f()
        sd.cell(i, 2, b).font = f()
        sd.cell(i, 3, c).font = f()
    sd.auto_filter.ref = 'A1:C%d' % (len(voc) + 1)

    wb.save(out_xlsx)
    return dict(master=n, imp=len(imp), voc=len(voc), picks=len(picks))

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('xlsx')
    ap.add_argument('--label', default='Breviarium Nidrosiense - pilot: St Andrew to Lucy (pp. 553-577)')
    a = ap.parse_args()
    print(build(a.outdir, a.xlsx, a.label))
