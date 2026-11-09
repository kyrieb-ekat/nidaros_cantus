#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_breviarium.py  --  pilot parser for the Nasjonalbiblioteket (2019) HTML edition
of the Breviarium / Missale Nidrosiense (1519), producing

  1. a MASTER table (every line of the section: chants, rubrics, readings, prayers,
     headings), with page/signature, rubrics, cross-references and review flags
  2. an IMPORT table that follows the Cantus Database chant-import template exactly
     (chants only by default; cantus_id is NEVER filled automatically)

Design rules
  * Markup does the segmentation: span.red = rubric, untagged text = sung/read text,
    span.pageRef = page / signature, <sup> = pointer to an endnote variant
    ("lemma] reading breviarium": lemma = edition reading, reading = the print).
  * Nothing is dropped: everything that is not an import row stays in the master.
  * fulltext_ms     = the print's reading (edition text with endnote variants applied)
  * fulltext_standardized = PROVISIONAL (see RULES below); to be proofread.
  * Cantus IDs: optionally PROPOSED (master only) from a Cantus Index export; never
    written to the import file.

Usage (see --help):
  python parse_breviarium.py HTML --outdir OUT [--source-id ID] [--seq-start N]
         [--start-heading ...] [--end-heading ...] [--ci-export CI.csv] [--include-prayers]
"""
import re, csv, sys, argparse, json, os, warnings
from collections import OrderedDict, defaultdict, Counter
from bs4 import BeautifulSoup, NavigableString, XMLParsedAsHTMLWarning
warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)

# ----------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------
SIGN_CHARS = {'\u023a': 'A', '\u211f': 'R', '\u2123': 'V'}      # Ⱥ ℟ ℣
SIGN_RE = re.compile('[\u023a\u211f\u2123]')
PAGE_RE = re.compile(r'\[\s*(\d+)\s*(?:\|\s*([^\]]*?)\s*)?\]')
LEAF_RE = re.compile(r'(?<![A-Za-z])([a-z]{2})\.\s?([ivxlj]+|\d+)\.?', re.I)
TONE_RE = re.compile(r'(?<![A-Za-z.])([a-z])\.\s?([ivxlj]+|\d+)\.?', re.I)
VOW = 'aeiouy'

# Cantus Database genre / service codes (checked against cantusdatabase.org/genres and /services)
GENRE_FOR_SIGN = {'A': 'A', 'R': 'R'}      # V / W decided from context
IMPORT_GENRES_DEFAULT = {'A', 'R', 'V', 'W', 'H', 'HV', 'I'}
PRAYER_GENRES = {'Cap', 'Or', 'Co', 'Sec', 'Pc'}

HOURS = [
    (re.compile(r'^Ad primas vesperas', re.I), 'V', False),
    (re.compile(r'^Ad secundas vesperas', re.I), 'V2', False),
    (re.compile(r'^Ad vesperas', re.I), 'V', True),            # unspecified vespers -> flag
    (re.compile(r'^Ad matutinas', re.I), 'M', False),
    (re.compile(r'^Ad laudes', re.I), 'L', False),
    (re.compile(r'^Ad primam', re.I), 'P', False),
    (re.compile(r'^Ad tertiam', re.I), 'T', False),
    (re.compile(r'^Ad sextam', re.I), 'S', False),
    (re.compile(r'^Ad nonam', re.I), 'N', False),
    (re.compile(r'^Ad horas diurn', re.I), 'D', False),
    (re.compile(r'^(In completorio|Ad completorium)', re.I), 'C', False),
]
NOCTURN_RE = re.compile(r'^In (primo|secundo|tertio) n\w*turno', re.I)
VIGIL_RE = re.compile(r'^IN\s+[UV]IGILIA\s+', re.I)
DAY_HOURS = {'L', 'P', 'T', 'S', 'N', 'D'}       # after any of these, an unqualified "Ad vesperas" is V2
SUPPLIED_NOTE = 'Feast heading not printed in the edition; supplied'   # goes to "extra" in the import file
NOCT_NUM = {'primo': 1, 'secundo': 2, 'tertio': 3}

FLAG_HELP = OrderedDict([
    ('NO_OFFICE', 'No hour rubric precedes this item; office left blank'),
    ('OFFICE_INFERRED', 'Office inferred (Invitatorium opens Matins)'),
    ('OFFICE_V_UNSPEC', '"Ad vesperas" without primas/secundas; coded V (first vespers) provisionally'),
    ('OFFICE_V2_INFERRED', '"Ad vesperas" without primas/secundas after Lauds or a day hour of the same feast; coded V2'),
    ('FEAST_SUPPLIED', 'Feast heading not printed; supplied (see extra_note). Map it to a Cantus feast in the app'),
    ('XREF_LEAF_RESOLVED', 'Incipit-only item; full text found on the cited leaf (see xref_resolved_*)'),
    ('XREF_LEAF_UNRESOLVED', 'Incipit-only item; cited leaf found but incipit not matched'),
    ('XREF_LEAF_AMBIGUOUS', 'Incipit-only item; several matches on the cited leaf'),
    ('XREF_LEAF_UNKNOWN', 'Cited signature/leaf not found in the edition'),
    ('XREF_INTERNAL_RESOLVED', 'Incipit-only item; full text found earlier in the same feast'),
    ('XREF_INTERNAL_UNRESOLVED', 'Incipit-only item with a verbal cross-reference (vt supra ...); not resolved'),
    ('VARIANT_UNRESOLVED', 'Endnote variant could not be applied automatically; check fulltext_ms'),
    ('MIXED_RUBRIC', 'Line mixes rubric and sung text without a chant sign; kept as rubric row'),
    ('SUPPLIED_LETTERS', 'Editorial supplied letters present (removed from ms, kept in standardized)'),
    ('REPETENDUM_NOT_FOUND', 'Responsory: repetendum could not be located'),
    ('ORPHAN_TEXT', 'Text block with no governing heading'),
    ('SIGN_IN_PLAIN_TEXT', 'Chant sign found outside rubric colour'),
    ('UNKNOWN_RUBRIC', 'Rubric not recognised by any rule; kept as generic rubric'),
    ('COMMEMORATION', 'Commemoration/memorial: CDB service R (Memorial) may be more apt'),
    ('PAGE_BREAK_IN_ROW', 'Row continues onto the next page (see page_end)'),
    ('HYMN_METHOD', 'Hymn stanzas entered as H + HV rows; confirm against the CDB hymn data-entry appendix'),
    ('CID_PROPOSED', 'One or more Cantus Index candidates proposed (master only; never imported)'),
])

# ----------------------------------------------------------------------------
# helpers: text
# ----------------------------------------------------------------------------
def collapse(s):
    return re.sub(r'\s+', ' ', s.replace('\xa0', ' ')).strip()

def norm_cmp(s):
    """loose comparison key: lowercase, u/v and i/j folded, punctuation stripped"""
    s = s.lower().replace('j', 'i').replace('v', 'u')
    s = re.sub(r'[\u2039\u203a|*]', '', s)
    s = re.sub(r'[^a-z ]+', ' ', s)
    return collapse(s)

ARAB = {'1': 'j', '2': 'ij', '3': 'iij', '4': 'iiij', '5': 'v', '6': 'vj', '7': 'vij', '8': 'viij'}

def norm_leaf(a, b):
    b = b.lower()
    b = ARAB.get(b, b)
    return '%s.%s.' % (a.lower(), b)

def parse_pageref(raw):
    m = PAGE_RE.search(raw)
    if not m:
        return None, None
    page = int(m.group(1))
    sig = m.group(2).strip() if m.group(2) else None
    if sig:
        sig = sig.strip()
    return page, sig

# ---- provisional standardisation ------------------------------------------
LEX = {'michi': 'mihi', 'nichil': 'nihil', 'nichilominus': 'nihilominus'}
PREFIX = ('in', 'con', 'ad', 'ob', 'sub', 'trans', 'inter', 'super', 'dis', 'circum')
LR_STEMS = ('paru', 'meru', 'aperu', 'operu', 'volu', 'dolu', 'colu', 'floru')   # perfects with vocalic u
LR_ENDS = ('it', 'isti', 'imus', 'istis', 'erunt', 'erat', 'erant', 'erit', 'isse', 'issent')

def lr_vocalic(w, i):
    """u at index i (after l/r) belongs to a perfect like apparuit/meruit/aperuit -> stays vocalic"""
    if w.startswith('eru') and i == 2 and w[i + 1:].startswith(LR_ENDS):
        return True
    return any(w[:i + 1].endswith(st) for st in LR_STEMS) and w[i + 1:].startswith(LR_ENDS)

def isv(c):
    """vowel test that is False for the empty string (end/start of word)"""
    return c != '' and c in 'aeiou'

def std_word(word):
    lw = word.lower().replace('j', 'i')
    if lw in LEX:
        new = LEX[lw]
        if word[:1].isupper():
            new = new[:1].upper() + new[1:]
        return new
    w = lw
    n = len(w)
    out = list(w)
    forced = {}
    for i, ch in enumerate(w):
        if ch not in 'uv':
            continue
        nxt = w[i + 1] if i + 1 < n else ''
        prev = out[i - 1] if i > 0 else ''
        if i in forced:
            out[i] = forced[i]
            continue
        if i == 0:                                  # print: v- at word start for both sounds
            out[i] = 'v' if isv(nxt) else 'u'
            continue
        if ch == 'v':                               # medial v in print: already consonantal
            continue
        lr_ok = (prev in ('l', 'r') and i >= 2 and isv(w[i - 2]) and not lr_vocalic(w, i)
                 and not w.startswith('eru'))
        # medial u
        if nxt == 'u':                              # 'uu' sequences
            nn = w[i + 2] if i + 2 < n else ''
            if isv(nn):                             # u + v + vowel (iuuenis, adiuuet, pluuia)
                out[i] = 'u'
                forced[i + 1] = 'v'
            else:                                   # v + u + cons/end (nouum, curuum, seruus)
                out[i] = 'v' if (prev != '' and (prev in 'aeioy' or lr_ok)) else 'u'
            continue
        if not isv(nxt):
            out[i] = 'u'
            continue
        # u followed by a vowel
        if prev in ('q', 'g', 's'):
            out[i] = 'u'
        elif prev != '' and prev in 'aeioy':
            out[i] = 'v'
        elif prev == 'u':
            out[i] = 'u'
        elif lr_ok:
            out[i] = 'v'
        elif any(w.startswith(p) and i == len(p) for p in PREFIX):
            out[i] = 'v'
        else:
            out[i] = 'u'
    res = ''.join(out)
    src = word.replace('j', 'i').replace('J', 'I')
    return ''.join(c.upper() if o.isupper() else c for c, o in zip(res, src)) if len(res) == len(src) else res

PUNCT_RE = re.compile('[\u201c\u201d\u201e"\u00ab\u00bb\'.,;:!?()\\[\\]\u2014\u2013]')

def clean_text(s, keep_stroke):
    s = PUNCT_RE.sub(' ', s)
    if not keep_stroke:
        s = s.replace('|', ' ')
    return collapse(s)

def standardise(s):
    s = re.sub(r'[\u2039\u203a]', '', s)                       # supplied letters kept
    s = clean_text(s, keep_stroke=False)
    s = collapse(re.sub(r'(?:\s*/\s*){2,}', ' / ', s))
    return re.sub(r'[A-Za-z]+', lambda m: std_word(m.group(0)), s)

def ms_clean(s):
    s = re.sub(r'\u2039[^\u203a]*\u203a', '', s)               # supplied letters dropped from print reading
    s = clean_text(s, keep_stroke=True)
    return collapse(re.sub(r'(?:\s*/\s*){2,}', ' / ', s))

# ----------------------------------------------------------------------------
# HTML tokenising
# ----------------------------------------------------------------------------
def walk(node, in_red, out):
    for ch in node.children:
        if isinstance(ch, NavigableString):
            t = str(ch)
            if t:
                out.append(['red' if in_red else 'blk', t])
        elif ch.name == 'span':
            cls = ch.get('class') or []
            if 'red' in cls:
                walk(ch, True, out)
            elif 'pageRef' in cls:
                out.append(['page', ch.get_text()])
            else:
                walk(ch, in_red, out)
        elif ch.name == 'sup':
            a = ch.find('a')
            out.append(['note', (a.get_text() if a else ch.get_text()).strip()])
        elif ch.name == 'div' and 'verseLine' in (ch.get('class') or []):
            walk(ch, in_red, out)
            out.append(['nl', ' / '])
        elif ch.name == 'br':
            continue
        else:
            walk(ch, in_red, out)

def merge_segments(segs):
    """merge neighbours of same type; attach red single-letter drop caps to following plain text"""
    merged = []
    for s in segs:
        if merged and merged[-1][0] == s[0] and s[0] in ('red', 'blk'):
            merged[-1][1] += s[1]
        else:
            merged.append([s[0], s[1]])
    out = []
    i = 0
    while i < len(merged):
        t, x = merged[i]
        if (t == 'red' and len(x.strip()) == 1 and x.strip().isalpha() and not SIGN_RE.search(x)
                and i + 1 < len(merged) and merged[i + 1][0] == 'blk' and merged[i + 1][1][:1] != ''
                and (merged[i + 1][1][:1].isalpha() or (merged[i + 1][1][:1] == ' ' and x.strip() in 'OAEI'
                                                        and merged[i + 1][1].strip()[:1].islower()))):
            merged[i + 1][1] = x.strip() + merged[i + 1][1]
            i += 1
            continue
        out.append([t, x])
        i += 1
    # stanza lines: drop-cap red may precede a note etc. -- good enough for this edition
    return out

def segments(el):
    segs = []
    walk(el, False, segs)
    return merge_segments(segs)

def seg_text(segs, types):
    return collapse(''.join(s[1] for s in segs if s[0] in types))

# ----------------------------------------------------------------------------
# endnote variants
# ----------------------------------------------------------------------------
def load_notes(soup):
    notes = {}
    for a in soup.find_all('a', attrs={'name': re.compile(r'^ton\d+$')}):
        nid = a.get_text().strip()
        txt = collapse(a.parent.get_text())
        txt = re.sub(r'^n\d+\.\s*', '', txt)
        if ']' in txt:
            lemma, rest = txt.split(']', 1)
            rest = rest.strip()
            m = re.match(r'^(.*?)\s+(breviarium|missale)\s*$', rest, re.I)
            reading = m.group(1).strip() if m else rest
            notes[nid] = ('variant', lemma.strip(), reading)
        else:
            notes[nid] = ('comment', txt, '')
    return notes

def replace_tail(s, lemma, reading):
    core = s.rstrip(' ,;:.!?"\u201c\u201d\n/')
    tail = s[len(core):]
    words = lemma.split()
    if not words:
        return None
    pat = r'[\s,;:.!?"\u201c\u201d/]*'.join(re.escape(w) for w in words) + r'$'
    m = re.search(pat, core)
    if not m:
        return None
    return core[:m.start()] + reading + tail

class Acc:
    """accumulates edition text and print-reading (ms) text for one row"""
    def __init__(self, notes):
        self.notes = notes
        self.ed = ''
        self.ms = ''
        self.var_applied = []
        self.var_unresolved = []
        self.comments = []
    def add(self, s):
        self.ed += s
        self.ms += s
    def note(self, nid):
        n = self.notes.get(nid)
        if not n:
            self.var_unresolved.append(nid)
            return
        kind, a, b = n
        if kind == 'comment':
            self.comments.append('%s: %s' % (nid, a))
            return
        new = replace_tail(self.ms, a, b)
        if new is None:
            self.var_unresolved.append('%s (%s] %s)' % (nid, a, b))
        else:
            self.ms = new
            self.var_applied.append('%s: %s] %s' % (nid, a, b))

# ----------------------------------------------------------------------------
# block collection
# ----------------------------------------------------------------------------
def collect_blocks(soup):
    out = []
    for el in soup.find_all(['div', 'table']):
        cls = el.get('class') or []
        if el.name == 'table':
            out.append(('table', el))
        elif 'paraNoIndent' in cls or 'paraCenter' in cls:
            out.append(('para', el))
        elif 'stanza' in cls:
            out.append(('stanza', el))
    return out

# ----------------------------------------------------------------------------
# prepass: index of every sign-line in the whole book by page (for leaf cross-references)
# ----------------------------------------------------------------------------
def build_leaf_index(blocks):
    page_sig = {}                      # page -> signature printed on it
    lines_by_page = defaultdict(list)  # page -> [(kind, edition_text)]
    page = None
    pending = None
    pending_hymn = False
    for kind, el in blocks:
        if kind == 'table':
            continue
        segs = segments(el)
        if kind == 'stanza' and pending_hymn:
            lines_by_page[page].append(('H', seg_text(segs, ('blk',))))
            pending_hymn = False
        for s in segs:
            if s[0] == 'page':
                p, sig = parse_pageref(s[1])
                if p:
                    page = p
                    if sig:
                        page_sig[p] = sig
        if kind != 'para':
            continue
        red = ''.join(s[1] for s in segs if s[0] == 'red')
        blk = seg_text(segs, ('blk',))
        redj = collapse(' '.join(s[1] for s in segs if s[0] == 'red'))
        m = SIGN_RE.search(red)
        if re.match(r'^inuitatorium\b', redj, re.I) and blk:
            lines_by_page[page].append(('I', blk)); pending = None; pending_hymn = False
            continue
        if re.match(r'^hymnus\b', redj, re.I):
            if blk:
                lines_by_page[page].append(('H', blk)); pending_hymn = False
            else:
                pending_hymn = True
            pending = None
            continue
        if m:
            sg = SIGN_CHARS[m.group(0)]
            if blk:
                lines_by_page[page].append((sg, blk))
                pending = None
            else:
                pending = sg
        elif pending and blk:
            lines_by_page[page].append((pending, blk))
            pending = None
        else:
            pending = None
    leaf_pages = defaultdict(list)
    for p, sig in page_sig.items():
        mm = re.match(r'^\s*([a-z]{2})\.\s?([ivxlj]+|\d+)\.?\s*$', sig, re.I)
        if mm:
            leaf_pages[norm_leaf(mm.group(1), mm.group(2))].append(p)
    return page_sig, lines_by_page, leaf_pages

def signature_leaf(page, page_sig):
    if page is None:
        return ''
    if page in page_sig:
        return page_sig[page].strip() + ' r'
    if (page - 1) in page_sig:
        return page_sig[page - 1].strip() + ' v'
    return ''

# ----------------------------------------------------------------------------
# row factory
# ----------------------------------------------------------------------------
MASTER_COLS = ['master_seq', 'row_type', 'in_import', 'import_seq', 'page_start', 'page_end', 'facsimile_ref',
               'signature_leaf', 'content_structure', 'feast', 'sub_section', 'office', 'office_printed',
               'nocturn', 'slot', 'genre', 'position', 'sign', 'incipit', 'fulltext_edition',
               'fulltext_ms', 'fulltext_standardized', 'rubric', 'xref_raw', 'xref_leaf',
               'incipit_only', 'xref_resolved_page', 'xref_resolved_text', 'repetendum_note',
               'variant_notes', 'indexing_notes', 'extra_note', 'review_flags', 'cid_proposed',
               'cid_candidate_text', 'cid_decision']

def new_row(**kw):
    r = OrderedDict((c, '') for c in MASTER_COLS)
    r['_flags'] = []
    r['_acc'] = None
    r['_incipit_only'] = False
    r['_ms_raw'] = ''
    r['_import_seq'] = ''
    r.update(kw)
    return r

SLOT_RE = [
    (re.compile(r'super psalmos', re.I), 'super psalmos'),
    (re.compile(r'ad magnificat', re.I), 'ad Magnificat'),
    (re.compile(r'ad benedictus', re.I), 'ad Benedictus'),
    (re.compile(r'in euangelio', re.I), 'in evangelio'),
    (re.compile(r'ad vesperas', re.I), 'ad vesperas'),
]

XREF_WORD = re.compile(r'\b(supra|ibidem|sequitur|cum ceteris|cetera|vt in|ut in)\b', re.I)

# ----------------------------------------------------------------------------
# the main pass over the chosen region
# ----------------------------------------------------------------------------
class Parser:
    def __init__(self, blocks, notes, leaf_info, region, cfg):
        self.blocks = blocks
        self.notes = notes
        self.page_sig, self.lines_by_page, self.leaf_pages = leaf_info
        self.start_i, self.end_i = region
        self.cfg = cfg
        self.rows = []
        self.page = None
        self.state = dict(content_structure='', feast='', sub_section='', office='', office_printed='',
                          nocturn='', office_inferred=False, office_v_unspec=False,
                          feast_printed='', vigil=False, hours_seen=set(), extra_note='')
        self.pending_sign = None       # (kind, lead_rubric, page, flags)
        self.pending_head = None       # row waiting for its text block(s)
        self.hymn_open = None          # list of rows for an open hymn
        self.last_row = None

    # --- page tracking ---------------------------------------------------
    def set_page_from(self, segs, until_content=True):
        """consume page markers; returns the page in force at start of content"""
        for s in segs:
            if s[0] == 'page':
                p, sig = parse_pageref(s[1])
                if p:
                    self.page = p
            elif s[0] in ('red', 'blk') and s[1].strip():
                if until_content:
                    break
        return self.page

    # --- row creation ------------------------------------------------------
    def emit(self, row):
        row['master_seq'] = len(self.rows) + 1
        row['content_structure'] = self.state['content_structure']
        row['feast'] = row['feast'] or self.state['feast']
        row['sub_section'] = row['sub_section'] or self.state['sub_section']
        row['office'] = row['office'] or self.state['office']
        row['office_printed'] = row['office_printed'] or self.state['office_printed']
        row['nocturn'] = row['nocturn'] or self.state['nocturn']
        row['_feast_printed'] = self.state['feast_printed']
        if self.state['extra_note'] and row['genre'] and not row['extra_note']:
            row['extra_note'] = self.state['extra_note']
        if row['page_start']:
            row['facsimile_ref'] = 's%s' % row['page_start']
            row['signature_leaf'] = signature_leaf(row['page_start'], self.page_sig)
        self.rows.append(row)
        self.last_row = row
        if row['genre'] in ('Or', 'Co') and self.state['sub_section']:
            self.state['sub_section'] = ''
        return row

    def finish_text(self, row, acc):
        row['fulltext_edition'] = collapse(acc.ed)
        row['_ms_raw'] = collapse(acc.ms)
        row['variant_notes'] = '; '.join(acc.var_applied + acc.comments)
        if acc.var_unresolved:
            row['_flags'].append('VARIANT_UNRESOLVED')
            row['variant_notes'] = (row['variant_notes'] + '; ' if row['variant_notes'] else '') + \
                'UNRESOLVED: ' + '; '.join(acc.var_unresolved)
        if '\u2039' in acc.ed:
            row['_flags'].append('SUPPLIED_LETTERS')

    # --- block dispatch ------------------------------------------------------
    def run(self):
        for idx in range(self.start_i, self.end_i):
            kind, el = self.blocks[idx]
            if kind == 'table':
                continue
            self.block(kind, el)
        self.close_hymn()
        return self.rows

    def block(self, kind, el):
        segs = segments(el)
        if kind == 'stanza':
            self.stanza(el, segs)
            return
        self.set_page_from(segs)
        red = collapse(' '.join(s[1] for s in segs if s[0] == 'red'))
        blk = seg_text(segs, ('blk',))
        content = [s for s in segs if s[0] in ('red', 'blk') and s[1].strip()]
        if not content:
            return
        first = content[0]
        # --- close an open hymn unless this is a plain refrain line
        if self.hymn_open is not None:
            if first[0] == 'blk' and not SIGN_RE.search(red):
                self.hymn_refrain(segs)
                return
            self.close_hymn()
        # --- feast heading ---------------------------------------------------
        if el.get('class') and 'paraCenter' in el.get('class') and not blk and self.is_heading(red):
            self.heading(red, segs)
            return
        # --- chant signs ---------------------------------------------------
        if any(s[0] == 'red' and SIGN_RE.search(s[1]) for s in segs):
            self.chant_block(segs)
            return
        if any(s[0] == 'blk' and SIGN_RE.search(s[1]) for s in segs):
            self.pending_head = None
        # --- hours / nocturns ------------------------------------------------
        if first[0] == 'red' and not blk:
            if self.hour(red, segs):
                return
            if self.nocturn(red, segs):
                return
        # --- keyword-led rubric lines -------------------------------------
        if first[0] == 'red':
            if self.keyword_line(red, blk, segs):
                return
        # --- plain text block: belongs to the pending heading --------------------
        if first[0] == 'blk' or (first[0] == 'red' and len(first[1].strip()) <= 2) or \
                (first[0] == 'red' and False):
            self.text_block(segs)
            return
        if first[0] == 'red' and blk:
            self.mixed_rubric(red, blk, segs)
            return
        self.generic_rubric(red, segs)

    # --- individual handlers ------------------------------------------------
    def is_heading(self, red):
        letters = [c for c in red if c.isalpha()]
        if len(letters) < 8:
            return False
        up = sum(1 for c in letters if c.isupper())
        return up / float(len(letters)) > 0.75

    def heading(self, red, segs):
        self.close_pending()
        row = new_row(row_type='heading', page_start=self.page, rubric=red)
        if re.match(r'^INCIPIT PROPRIUM DE SANCTIS', red):
            self.state['content_structure'] = 'Proprium de sanctis'
            self.state.update(feast='', sub_section='', office='', office_printed='', nocturn='')
        else:
            self.state.update(feast=red, sub_section='', office='', office_printed='', nocturn='',
                              office_inferred=False, office_v_unspec=False, feast_printed=red,
                              vigil=bool(VIGIL_RE.match(red)), hours_seen=set(), extra_note='')
            row['feast'] = red
        self.emit(row)

    def close_pending(self):
        self.pending_head = None
        self.pending_sign = None

    def hour(self, red, segs):
        for rx, code, unspec in HOURS:
            if rx.search(red):
                self.close_pending()
                flags = []
                if code == 'V' and self.state['vigil']:
                    # first Vespers under a vigil heading: the feast proper begins here
                    base = VIGIL_RE.sub('', self.state['feast']).strip()
                    self.supplied_heading('[%s]' % base)
                elif code == 'M' and 'V2' in self.state['hours_seen']:
                    # Matins after Second Vespers: a following day's office, heading not printed
                    base = self.state['feast'].strip('[]')
                    self.supplied_heading('[%s, post secundas vesperas]' % base)
                if unspec and code == 'V' and self.state['hours_seen'] & DAY_HOURS:
                    code, unspec = 'V2', False
                    flags.append('OFFICE_V2_INFERRED')
                elif unspec:
                    flags.append('OFFICE_V_UNSPEC')
                self.state.update(office=code, office_printed=red, nocturn='', office_inferred=False,
                                  office_v_unspec=unspec, sub_section='')
                self.state['hours_seen'].add(code)
                row = new_row(row_type='office_rubric', page_start=self.page, rubric=red)
                row['_flags'].extend(flags)
                self.emit(row)
                return True
        return False

    def supplied_heading(self, label, note=SUPPLIED_NOTE):
        """Start a new feast part whose heading the edition does not print."""
        self.state.update(feast=label, sub_section='', office='', office_printed='', nocturn='',
                          office_inferred=False, office_v_unspec=False, vigil=False,
                          hours_seen=set(), extra_note=note)
        row = new_row(row_type='heading', page_start=self.page, feast=label, extra_note=note)
        row['_flags'].append('FEAST_SUPPLIED')
        self.emit(row)

    def nocturn(self, red, segs):
        m = NOCTURN_RE.search(red)
        if not m:
            return False
        self.close_pending()
        self.state['nocturn'] = str(NOCT_NUM[m.group(1).lower()])
        self.state['sub_section'] = ''
        row = new_row(row_type='nocturn_rubric', page_start=self.page, rubric=red)
        self.emit(row)
        return True

    def keyword_line(self, red, blk, segs):
        r0 = red.strip()
        low = r0.lower()
        acc = None
        def trailing_red(after_black):
            return collapse(' '.join(s[1] for s in after_black if s[0] == 'red'))
        # antiphon rubrics lacking the Ⱥ sign in the edition
        if re.match(r'^antiphon[ea]\b', low) and not blk:
            self.pending_head = None
            self.pending_sign = ('A', [red], self.page, ['NOSIGN'])
            return True
        if re.match(r'^super psalmos', low) and blk:
            self.close_pending()
            self.chant_row(segs, 'antiphon', 'A', sign='')
            return True
        # psalm cues
        if re.match(r'^(psalmus|psalmi|canticum)\b', low):
            self.close_pending()
            row = new_row(row_type='psalm_cue', page_start=self.page, rubric=red,
                          fulltext_edition=blk)
            self.fill_leaf(row, red)
            self.emit(row)
            return True
        if re.match(r'^inuitatorium\b', low):
            self.close_pending()
            self.office_from_invitatorium()
            row = self.chant_row(segs, 'invitatory', 'I', sign='')
            return True
        if re.match(r'^hymnus\b', low):
            self.close_pending()
            if blk:
                self.chant_row(segs, 'hymn', 'H', sign='')
            else:
                row = new_row(row_type='hymn', genre='H', page_start=self.page, rubric=red, sign='')
                self.pending_head = None
                self.hymn_open = [row]
                self.hymn_head = row
                self.hymn_head_page = self.page
            return True
        if re.match(r'^capitulum\b', low):
            self.close_pending()
            if blk:
                self.chant_row(segs, 'capitulum', 'Cap', sign='')
            else:
                row = new_row(row_type='capitulum', genre='Cap', page_start=self.page, rubric=red)
                self.pending_head = row
            return True
        if re.match(r'^(oratio|collecta|collecte)\b', low):
            self.close_pending()
            g = 'Co' if low.startswith('collect') else 'Or'
            if blk:
                self.chant_row(segs, 'prayer', g, sign='')
            else:
                row = new_row(row_type='prayer', genre=g, page_start=self.page, rubric=red)
                self.pending_head = row
            return True
        if re.match(r'^(lectio|secundum|matthei|luce|lucae|marci|ioannis|initium|homilia)\b', low):
            self.close_pending()
            rt = 'homily' if low.startswith('homilia') else 'lesson'
            row = new_row(row_type=rt, page_start=self.page, rubric=red)
            if blk:
                row['fulltext_edition'] = blk
            self.pending_head = row
            return True
        if re.match(r'^(commemoratio|de sancto .* commemoratio)', low) or low.endswith('commemoratio'):
            self.close_pending()
            self.state['sub_section'] = red
            row = new_row(row_type='commemoration_rubric', page_start=self.page, rubric=red)
            row['_flags'].append('COMMEMORATION')
            self.emit(row)
            return True
        return False

    def office_from_invitatorium(self):
        self.state['sub_section'] = ''
        if 'V2' in self.state['hours_seen'] and self.state['office'] != 'M':
            base = self.state['feast'].strip('[]')
            self.supplied_heading('[%s, post secundas vesperas]' % base)
        if self.state['office'] != 'M':
            self.state['office'] = 'M'
            self.state['office_printed'] = '(Invitatorium)'
            self.state['office_inferred'] = True
            self.state['inv_flag'] = True
        self.state['hours_seen'].add('M')

    def text_block(self, segs):
        acc = Acc(self.notes)
        self.consume_text(segs, acc)
        text = collapse(acc.ed)
        if self.pending_sign:
            kind, lead, page, flags = self.pending_sign
            self.pending_sign = None
            self.make_chant(kind, lead, segs, page, flags, force_text=True)
            return
        if self.pending_head is not None:
            row = self.pending_head
            self.pending_head = None
            self.finish_text(row, acc)
            self.fill_leaf(row, ' '.join(s[1] for s in segs if s[0] == 'red'))
            if row['genre'] in PRAYER_GENRES or row['row_type'] in ('prayer', 'capitulum'):
                row['incipit_only'] = ''
            self.emit(row)
            return
        row = new_row(row_type='text_orphan', page_start=self.page)
        self.finish_text(row, acc)
        row['_flags'].append('ORPHAN_TEXT')
        self.emit(row)

    def consume_text(self, segs, acc):
        """feed black text and notes of segs into acc; update page state; return page_end"""
        started = False
        for s in segs:
            if s[0] == 'blk':
                acc.add(s[1]); started = True
            elif s[0] == 'red' and not SIGN_RE.search(s[1]):
                pass
            elif s[0] == 'note':
                acc.note(s[1])
            elif s[0] == 'page':
                p, sig = parse_pageref(s[1])
                if p:
                    self.page = p
            elif s[0] == 'nl':
                acc.add(s[1])

    def mixed_rubric(self, red, blk, segs):
        self.close_pending()
        row = new_row(row_type='rubric_mixed', page_start=self.page, rubric=red)
        parts = []
        for s in segs:
            if s[0] in ('red', 'blk') and s[1].strip():
                parts.append(('[%s] ' % ('R' if s[0] == 'red' else 'T')) + collapse(s[1]))
        row['fulltext_edition'] = blk
        row['indexing_notes'] = 'mixed line: ' + ' '.join(parts)
        row['_flags'].append('MIXED_RUBRIC')
        self.emit(row)

    def generic_rubric(self, red, segs):
        self.close_pending()
        row = new_row(row_type='rubric', page_start=self.page, rubric=red)
        if re.search(r'commemoratio', red, re.I) and len(red) < 120:
            self.state['sub_section'] = red
            row['row_type'] = 'commemoration_rubric'
            row['_flags'].append('COMMEMORATION')
        self.emit(row)

    # --- chants -------------------------------------------------------------
    def chant_block(self, segs):
        """one paragraph carrying one or more chant signs"""
        self.close_pending_head_only()
        # split segments at signs
        cur = None
        groups = []     # each: dict(kind, lead[], text_segs[], trail[])
        lead_buf = []
        for s in segs:
            if s[0] == 'red' and SIGN_RE.search(s[1]):
                m = SIGN_RE.search(s[1])
                before = collapse(s[1][:m.start()])
                after = collapse(s[1][m.end():])
                if cur is not None and cur['seen_text']:
                    groups.append(cur)
                    cur = None
                    lead_buf = []
                if before:
                    lead_buf.append(before)
                cur = dict(kind=SIGN_CHARS[m.group(0)], lead=list(lead_buf), text=[], trail=[],
                           seen_text=False, page=self.page)
                lead_buf = []
                if after:
                    cur['lead'].append(after)
            elif cur is None:
                if s[0] == 'red' and s[1].strip():
                    lead_buf.append(collapse(s[1]))
                elif s[0] == 'page':
                    p, sig = parse_pageref(s[1])
                    if p:
                        self.page = p
            else:
                if s[0] == 'blk' and s[1].strip():
                    cur['text'].append(s); cur['seen_text'] = True
                elif s[0] in ('blk',):
                    cur['text'].append(s)
                elif s[0] == 'red' and s[1].strip():
                    (cur['trail'] if cur['seen_text'] else cur['lead']).append(collapse(s[1]))
                elif s[0] in ('note', 'nl'):
                    cur['text'].append(s)
                elif s[0] == 'page':
                    cur['text'].append(s)
        if cur is not None:
            groups.append(cur)
        for g in groups:
            if g['seen_text']:
                self.make_chant_from_group(g)
            else:
                self.pending_sign = (g['kind'], g['lead'], self.page, [])

    def close_pending_head_only(self):
        self.pending_head = None

    def make_chant_from_group(self, g):
        # page at start: the row page captured when the sign was met
        acc = Acc(self.notes)
        page_start = g['page']
        page_end = page_start
        for s in g['text']:
            if s[0] == 'blk':
                acc.add(s[1])
            elif s[0] == 'note':
                acc.note(s[1])
            elif s[0] == 'page':
                p, sig = parse_pageref(s[1])
                if p:
                    self.page = p
                    page_end = p
        self.build_chant(g['kind'], g['lead'], g['trail'], acc, page_start, page_end)

    def make_chant(self, kind, lead, segs, page, flags, force_text=False):
        """pending sign + text block"""
        acc = Acc(self.notes)
        page_end = page
        trail = []
        for s in segs:
            if s[0] == 'blk':
                acc.add(s[1])
            elif s[0] == 'red' and s[1].strip() and not (len(s[1].strip()) <= 2):
                trail.append(collapse(s[1]))
            elif s[0] == 'note':
                acc.note(s[1])
            elif s[0] == 'page':
                p, sig = parse_pageref(s[1])
                if p:
                    self.page = p
                    page_end = p
        row = self.build_chant(kind, lead, trail, acc, page, page_end)
        if 'NOSIGN' in flags:
            row['sign'] = ''
            row['indexing_notes'] = (row['indexing_notes'] + '; ' if row['indexing_notes'] else '') + \
                'no Ⱥ sign in the edition (antiphon identified from the rubric)'
        return row

    def chant_row(self, segs, row_type, genre, sign):
        """rubric-led chant lines without sign glyph (Hymnus X, Inuitatorium X, Capitulum X, Oratio X)"""
        acc = Acc(self.notes)
        page_start = self.page
        page_end = page_start
        lead, trail = [], []
        seen = False
        for s in segs:
            if s[0] == 'blk' and s[1].strip():
                seen = True
                acc.add(s[1])
            elif s[0] == 'blk':
                acc.add(s[1])
            elif s[0] == 'red' and s[1].strip():
                (trail if seen else lead).append(collapse(s[1]))
            elif s[0] == 'note':
                acc.note(s[1])
            elif s[0] == 'page':
                p, sig = parse_pageref(s[1])
                if p:
                    self.page = p
                    page_end = p
        row = self.build_chant(sign, lead, trail, acc, page_start, page_end, genre=genre, row_type=row_type)
        if genre == 'A':
            row['indexing_notes'] = (row['indexing_notes'] + '; ' if row['indexing_notes'] else '') + \
                'no Ⱥ sign in the edition (antiphon identified from the rubric)'
        return row

    def build_chant(self, kind, lead, trail, acc, page_start, page_end, genre=None, row_type=None):
        text_ed = collapse(acc.ed)
        rubric_parts = [x for x in lead + trail if x]
        rubric = ' | '.join(rubric_parts)
        # genre from sign
        if genre is None:
            if kind == 'A':
                genre, row_type = 'A', 'antiphon'
            elif kind == 'R':
                genre, row_type = 'R', 'responsory'
            else:
                lr = self.last_row
                if lr is not None and lr['row_type'] == 'responsory' and not lr.get('_verse_taken') \
                        and lr['master_seq'] == len(self.rows):
                    genre, row_type = 'V', 'responsory_verse'
                    lr['_verse_taken'] = True
                else:
                    genre, row_type = 'W', 'versicle'
        row = new_row(row_type=row_type, genre=genre, sign=('Ⱥ℟℣'['ARV'.index(kind)] if kind else ''),
                      page_start=page_start, page_end=page_end if page_end != page_start else '',
                      rubric=rubric)
        if page_end != page_start and page_end:
            row['_flags'].append('PAGE_BREAK_IN_ROW')
        slot_src = ' '.join(lead)
        for rx, lab in SLOT_RE:
            if rx.search(slot_src):
                row['slot'] = lab
                break
        self.finish_text(row, acc)
        # xref detection in trailing red
        tr = ' '.join(trail)
        leafs = [norm_leaf(a, b) for a, b in LEAF_RE.findall(tr)]
        if kind in ('A', 'R', 'V') or genre in ('H', 'I', 'Cap', 'Or', 'Co'):
            self.fill_leaf(row, ' '.join(lead + trail))
        row['_incipit_only'] = bool(leafs or XREF_WORD.search(tr))
        row['incipit_only'] = 'yes' if row['_incipit_only'] else ''
        if self.state['sub_section'] and genre in ('A', 'R', 'V', 'W', 'H', 'I', 'Or', 'Co'):
            row['sub_section'] = self.state['sub_section']
        # office checks
        if not self.state['office'] and genre in ('A', 'R', 'V', 'W', 'H', 'I'):
            row['_flags'].append('NO_OFFICE')
        if self.state.pop('inv_flag', False):
            row['_flags'].append('OFFICE_INFERRED')
        self.emit(row)
        return row

    def fill_leaf(self, row, rubric_text):
        leafs = [norm_leaf(a, b) for a, b in LEAF_RE.findall(rubric_text or '')]
        tones = ['%s.%s.' % (a.lower(), b.lower()) for a, b in TONE_RE.findall(rubric_text or '')]
        # TONE_RE also matches the tail of a double-letter ref; remove those
        tones = [t for t in tones if not any(l.endswith(t) for l in leafs)]
        bits = []
        if leafs:
            bits.append('leaf ref ' + ', '.join(leafs))
            row['xref_leaf'] = ', '.join(leafs)
        if tones:
            bits.append('single-letter ref ' + ', '.join(tones) + ' (psalm tone/differentia?)')
        if rubric_text and re.search(r'\b(supra|ibidem|cum ceteris|sequitur)\b', rubric_text, re.I):
            row['xref_raw'] = collapse(' '.join(x for x in re.findall(
                r'(?:vt|ut)?\s*(?:supra|ibidem|cum ceteris|sequitur)[^|]*', rubric_text, re.I)))
        elif leafs and not row['xref_raw']:
            row['xref_raw'] = ', '.join(leafs)
        if bits:
            row['indexing_notes'] = (row['indexing_notes'] + '; ' if row['indexing_notes'] else '') + '; '.join(bits)

    # --- hymns -----------------------------------------------------------
    def stanza(self, el, segs):
        self.set_page_from(segs)
        if self.hymn_open is None:
            # stanza with no open hymn: keep as orphan hymn text
            row = new_row(row_type='text_orphan', page_start=self.page)
            acc = Acc(self.notes)
            self.consume_text(segs, acc)
            self.finish_text(row, acc)
            row['_flags'].append('ORPHAN_TEXT')
            self.emit(row)
            return
        acc = Acc(self.notes)
        pstart = self.page
        self.consume_text(segs, acc)
        pend = self.page
        txt = collapse(acc.ed).rstrip('/ ').strip()
        acc.ed = txt
        acc.ms = collapse(acc.ms).rstrip('/ ').strip()
        head = self.hymn_open[0]
        if head.get('_has_text') and txt.count(' / ') <= 1:
            row = new_row(row_type='hymn_refrain', page_start=pstart)
            self.finish_text(row, acc)
            self.hymn_open.append(row)
            return
        if not head.get('_has_text'):
            row = head
            row['_has_text'] = True
            row['page_start'] = pstart
        else:
            row = new_row(row_type='hymn_verse', genre='HV', page_start=pstart)
            row['_hymn_head'] = head
            self.hymn_open.append(row)
        if pend != pstart:
            row['page_end'] = pend
            row['_flags'].append('PAGE_BREAK_IN_ROW')
        self.finish_text(row, acc)
        if row is head:
            row['_flags'].append('HYMN_METHOD')
        row['sign'] = ''

    def hymn_refrain(self, segs):
        acc = Acc(self.notes)
        pstart = self.page
        self.consume_text(segs, acc)
        row = new_row(row_type='hymn_refrain', page_start=pstart)
        self.finish_text(row, acc)
        self.hymn_open.append(row)

    def close_hymn(self):
        if self.hymn_open is None:
            return
        rows = self.hymn_open
        self.hymn_open = None
        if not rows[0].get('_has_text'):
            rows[0]['_flags'].append('ORPHAN_TEXT')
        for r in rows:
            self.emit(r)

# ----------------------------------------------------------------------------
# post-processing
# ----------------------------------------------------------------------------
REP_SPLIT = re.compile(r'(?<=[.!?])\s+')

def verse_tail(verse_ed):
    """incipit cue at the end of a responsory verse"""
    v = collapse(verse_ed).strip()
    v = re.sub(r'\s*Gloria\.?\s*$', '', v).strip()
    mq = re.search(r'["\u201c]([^"\u201d]{1,40}?)[.!?]?["\u201d]\s*[.!?]?\s*$', v)
    if mq and len(mq.group(1).split()) <= 4:
        return mq.group(1).strip()
    frags = [f.strip(' .') for f in REP_SPLIT.split(v) if f.strip(' .')]
    if not frags:
        return ''
    # drop a trailing "Gloria"
    frags = [f for f in frags if f.lower() != 'gloria']
    tail = frags[-1] if frags else ''
    return tail if 0 < len(tail.split()) <= 5 else ''

def find_repetendum(respond_ed, tail):
    """return char offset in respond_ed where the repetendum begins, or None"""
    if not tail:
        return None
    words = [re.escape(w) for w in tail.replace('.', '').split()]
    if not words:
        return None
    pat = re.compile(r'(?:(?<=^)|(?<=[\s"\u201c]))' + r'[\s,;:.]*'.join(words) + r'(?![A-Za-z])', re.I)
    cands = [m for m in pat.finditer(respond_ed) if m.start() > 0]
    if not cands:
        return None
    caps = [m for m in cands if respond_ed[m.start()].isupper()
            and re.search(r'[,;:."\u201c]\s*$', respond_ed[:m.start()])]
    m = (caps or cands)[-1]
    return m.start()

STARTERS = {'Et', 'Qui', 'Quia', 'Quod', 'Ut', 'Ne', 'Cuius', 'Quorum', 'Que', 'Qua', 'Quem', 'Quam',
            'Per', 'Pro', 'Sed', 'Nam', 'Illi', 'Hic', 'Cum', 'Ad', 'In', 'A'}

def fallback_repetendum(ed):
    """no usable cue in the verse: capital after comma/semicolon, else a typical clause starter in capitals"""
    ms = list(re.finditer(r'[,;]\s+([A-Z][a-z]*)\b', ed))
    if ms:
        return ms[-1].start(1)
    ms = [m for m in re.finditer(r'\s([A-Z][a-z]*)\b', ed) if m.group(1) in STARTERS and m.start(1) > 0]
    if ms:
        return ms[-1].start(1)
    return None

def insert_stroke(ms, ed, pos_ed):
    """insert ' | ' into the print reading at the word where the repetendum starts in the edition text"""
    def key(w):
        return re.sub(r'[^a-z]', '', w.lower().replace('j', 'i').replace('v', 'u'))
    ed_tokens = [t for t in re.findall(r'\S+', ed[:pos_ed]) if re.search(r'[A-Za-z]', t)]
    target = len(ed_tokens)
    ed_next = [t for t in re.findall(r'\S+', ed[pos_ed:]) if re.search(r'[A-Za-z]', t)]
    if not ed_next:
        return ms
    want = key(ed_next[0])
    toks = list(re.finditer(r'[^\s|/]+', ms))
    best = None
    for off in (0, -1, 1, -2, 2, -3, 3):
        j = target + off
        if 0 <= j < len(toks) and key(toks[j].group(0)) == want:
            best = j
            break
    if best is None or best == 0:
        return ms
    cut = toks[best].start()
    return ms[:cut].rstrip() + ' | ' + ms[cut:]

def postprocess(rows, cfg, leaf_info):
    page_sig, lines_by_page, leaf_pages = leaf_info
    include = set(cfg['import_genres'])
    # --- pair responsories and verses, repetendum
    for i, r in enumerate(rows):
        r['_ms_raw'] = r.get('_ms_raw', '')
    for i, r in enumerate(rows):
        if r['row_type'] == 'responsory' and not r['_incipit_only']:
            verse = None
            if i + 1 < len(rows) and rows[i + 1]['row_type'] == 'responsory_verse':
                verse = rows[i + 1]
            if verse is not None:
                tail = verse_tail(verse['fulltext_edition'])
                pos = find_repetendum(r['fulltext_edition'], tail)
                if pos is not None:
                    r['_ms_raw'] = insert_stroke(r['_ms_raw'], r['fulltext_edition'], pos)
                    r['repetendum_note'] = 'repetendum begins "%s" (cue in verse: "%s")' % (
                        collapse(r['fulltext_edition'][pos:pos + 25]).split(' ')[0], tail)
                else:
                    pos = fallback_repetendum(r['fulltext_edition'])
                    if pos is not None:
                        r['_ms_raw'] = insert_stroke(r['_ms_raw'], r['fulltext_edition'], pos)
                        r['repetendum_note'] = 'repetendum begins "%s" (from capital after comma; no cue in verse)' % \
                            collapse(r['fulltext_edition'][pos:pos + 25]).split(' ')[0]
                    elif len(r['fulltext_edition'].split()) > 6:
                        r['_flags'].append('REPETENDUM_NOT_FOUND')
                        r['repetendum_note'] = 'verse cue "%s" not located in respond' % tail
                    else:
                        r['repetendum_note'] = 'short cue only'
                r['indexing_notes'] = (r['indexing_notes'] + '; ' if r['indexing_notes'] else '') + \
                    'verse in next row (V)'
                verse['indexing_notes'] = (verse['indexing_notes'] + '; ' if verse['indexing_notes'] else '') + \
                    'verse of R at master_seq %d' % r['master_seq']
            else:
                if len(r['fulltext_edition'].split()) > 6:
                    r['_flags'].append('REPETENDUM_NOT_FOUND')
                    r['repetendum_note'] = 'no verse follows; repetendum not determined'
                else:
                    r['repetendum_note'] = 'short cue only'
    # --- texts
    for r in rows:
        ed = r['fulltext_edition']
        if r['_ms_raw'] or ed:
            r['fulltext_ms'] = ms_clean(r['_ms_raw'] or ed)
            r['fulltext_standardized'] = standardise(ed)
        if r['genre'] in ('A', 'R', 'V', 'W', 'H', 'HV', 'I', 'Cap', 'Or', 'Co', 'Sec', 'Pc'):
            words = r['fulltext_standardized'].split()
            r['incipit'] = ' '.join(words[:6])
    # --- hymn head rubric text
    for r in rows:
        if r['row_type'] == 'hymn_verse' and r.get('_hymn_head'):
            pass
    # --- cross-reference resolution
    kind_of = {'\u023a': 'A', '\u211f': 'R', '\u2123': 'V'}
    seen_by_feast = defaultdict(list)
    for r in rows:
        if r['genre'] in ('A', 'R', 'V', 'W', 'H', 'I'):
            key = (r.get('_feast_printed') or r['feast'], r['genre'])
            if r['_incipit_only']:
                inc = norm_cmp(r['fulltext_edition'])
                nwords = len(inc.split())
                want = {'A': 'A', 'R': 'R', 'V': 'V', 'W': 'V', 'H': 'H', 'I': 'I'}.get(r['genre'], '')
                if r['xref_leaf']:
                    leaf = r['xref_leaf'].split(', ')[0]
                    pages = leaf_pages.get(leaf)
                    if not pages:
                        r['_flags'].append('XREF_LEAF_UNKNOWN')
                    else:
                        found = OrderedDict()
                        for pg0 in pages:
                            for pg in (pg0, pg0 + 1):
                                for kind, txt in lines_by_page.get(pg, []):
                                    if want and kind != want:
                                        continue
                                    if inc and norm_cmp(txt).startswith(inc):
                                        found[(pg, norm_cmp(txt))] = (pg, kind, txt)
                        found = list(found.values())
                        if len(found) == 1:
                            r['xref_resolved_page'] = found[0][0]
                            r['xref_resolved_text'] = found[0][2]
                            r['_flags'].append('XREF_LEAF_RESOLVED')
                        elif len(found) > 1:
                            r['xref_resolved_page'] = ', '.join(str(f[0]) for f in found)
                            r['_flags'].append('XREF_LEAF_AMBIGUOUS')
                        else:
                            r['_flags'].append('XREF_LEAF_UNRESOLVED')
                else:
                    prev = [x for x in seen_by_feast[key] if not x['_incipit_only']
                            and inc and nwords >= 2 and norm_cmp(x['fulltext_edition']).startswith(inc)]
                    if len(prev) == 1:
                        r['xref_resolved_page'] = prev[0]['page_start']
                        r['xref_resolved_text'] = prev[0]['fulltext_edition']
                        r['indexing_notes'] = (r['indexing_notes'] + '; ' if r['indexing_notes'] else '') + \
                            'full text at master_seq %d' % prev[0]['master_seq']
                        r['_flags'].append('XREF_INTERNAL_RESOLVED')
                    else:
                        r['_flags'].append('XREF_INTERNAL_UNRESOLVED')
            seen_by_feast[key].append(r)
    # --- positions
    counters = defaultdict(int)
    for r in rows:
        if r['genre'] in ('A', 'R', 'V', 'W', 'H', 'HV', 'I', 'Cap', 'Or', 'Co', 'Sec', 'Pc'):
            key = (r['feast'], r['office'])
            counters[key] += 1
            r['position'] = '%02d' % counters[key]
    # --- import flag + sequence
    seq = cfg['seq_start'] - 1
    for r in rows:
        if r['genre'] in include:
            r['in_import'] = 'yes'
            seq += 1
            r['_import_seq'] = seq
            r['import_seq'] = seq
        else:
            r['in_import'] = ''
    # --- notes composition & flags
    for r in rows:
        r['review_flags'] = '; '.join(OrderedDict((f, 1) for f in r['_flags']).keys())
    return rows

# ----------------------------------------------------------------------------
# Cantus Index proposals (optional, master only)
# ----------------------------------------------------------------------------
def load_ci(path):
    rows = []
    with open(path, newline='', encoding='utf-8') as fh:
        rd = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in rd.fieldnames}
        ccid = next((cols[k] for k in ('cid', 'cantus_id', 'cantus id') if k in cols), None)
        ctxt = next((cols[k] for k in ('fulltext', 'full_text', 'text', 'incipit', 'fulltext_standardized')
                     if k in cols), None)
        cgen = next((cols[k] for k in ('genre', 'genre_id') if k in cols), None)
        if not ccid or not ctxt:
            raise SystemExit('CI export needs a CID column and a text/incipit column')
        for rec in rd:
            rows.append((rec[ccid].strip(), norm_cmp(rec[ctxt]), rec.get(cgen, '') if cgen else ''))
    return rows

def propose_cids(rows, ci):
    index = defaultdict(list)
    for cid, key, gen in ci:
        index[' '.join(key.split()[:3])].append((cid, key, gen))
    for r in rows:
        if r['genre'] not in ('A', 'R', 'V', 'W', 'H', 'I'):
            continue
        key = norm_cmp(re.sub(r'[\u2039\u203a|*]', '', r['fulltext_standardized']))
        k3 = ' '.join(key.split()[:3])
        cands = []
        for cid, ckey, gen in index.get(k3, []):
            n = min(len(key.split()), len(ckey.split()))
            same = sum(1 for a, b in zip(key.split(), ckey.split()) if a == b)
            score = same / float(max(1, n))
            cands.append((score, cid))
        cands.sort(reverse=True)
        if cands:
            r['cid_proposed'] = '; '.join('%s (%.2f)' % (c, s) for s, c in cands[:3])
            r['_flags'].append('CID_PROPOSED')
            r['review_flags'] = '; '.join(OrderedDict((f, 1) for f in r['_flags']).keys())

# ----------------------------------------------------------------------------
# outputs
# ----------------------------------------------------------------------------
IMPORT_COLS = ['source_id', 'marginalia', 'folio', 'content structure', 'sequence', 'incipit', 'feast', 'office',
               'genre', 'position', 'cantus_id', 'mode', 'finalis', 'differentia', 'fulltext_standardized',
               'fulltext_ms', 'fulltext_standardized_proofread', 'fulltext_ms_proofread', 'volpiano',
               'volpiano_proofread', 'image_link', 'melody_id', 'cao_concordances', 'addendum', 'extra',
               'indexing_notes', 'node_id']

def import_rows(rows, cfg):
    out = []
    for r in rows:
        if r['in_import'] != 'yes':
            continue
        std = r['fulltext_standardized']
        if r['_incipit_only'] and std:
            std = std + '*'
        notes = []
        if r['indexing_notes']:
            notes.append(r['indexing_notes'])
        if r['variant_notes']:
            notes.append('print variants: ' + r['variant_notes'])
        if r['repetendum_note']:
            notes.append(r['repetendum_note'])
        if r['xref_resolved_text']:
            notes.append('full text elsewhere (p. %s)' % r['xref_resolved_page'])
        notes.append('edition p. %s%s' % (r['page_start'], ('-%s' % r['page_end']) if r['page_end'] else ''))
        d = OrderedDict((c, '') for c in IMPORT_COLS)
        d['source_id'] = cfg['source_id']
        d['folio'] = r['page_start']
        d['content structure'] = r['content_structure']
        d['sequence'] = r['_import_seq']
        d['incipit'] = r['incipit']
        d['feast'] = r['feast']
        d['office'] = r['office']
        d['genre'] = r['genre']
        d['position'] = r['position']
        d['fulltext_standardized'] = std
        d['fulltext_ms'] = r['fulltext_ms']
        d['extra'] = ' | '.join(x for x in (r['rubric'], r['extra_note']) if x)
        d['indexing_notes'] = '; '.join(notes)
        out.append(d)
    return out

def write_csv(path, cols, rows, enc='utf-8-sig'):
    with open(path, 'w', newline='', encoding=enc) as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)

def std_vocab(rows):
    """words whose standardized form differs from the edition form"""
    voc = Counter()
    for r in rows:
        ed = re.sub(r'[\u2039\u203a]', '', r['fulltext_edition'])
        ed = clean_text(ed, False)
        for w in re.findall(r'[A-Za-z]+', ed):
            s = std_word(w)
            if s != w:
                voc[(w, s)] += 1
    return voc

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('html')
    ap.add_argument('--outdir', default='out')
    ap.add_argument('--source-id', default='')
    ap.add_argument('--seq-start', type=int, default=1)
    ap.add_argument('--start-heading', default='INCIPIT PROPRIUM DE SANCTIS')
    ap.add_argument('--end-heading', default='IN UIGILIA BEATI THOME APOSTOLI')
    ap.add_argument('--ci-export', default=None, help='optional Cantus Index export (CSV) for CID PROPOSALS')
    ap.add_argument('--include-prayers', action='store_true', help='also put Cap/Or/Co/Sec/Pc rows in the import CSV')
    ap.add_argument('--json', action='store_true', help='also dump master rows as JSON')
    a = ap.parse_args(argv)

    html = open(a.html, encoding='utf-8').read()
    soup = BeautifulSoup(html, 'lxml')
    notes = load_notes(soup)
    blocks = collect_blocks(soup)
    leaf_info = build_leaf_index(blocks)

    def block_red(i):
        k, el = blocks[i]
        if k == 'table':
            return ''
        return collapse(''.join(s[1] for s in segments(el) if s[0] == 'red'))
    start = next((i for i in range(len(blocks)) if a.start_heading in block_red(i)
                  and blocks[i][0] == 'para'), None)
    if start is None:
        raise SystemExit('start heading not found')
    end = next((i for i in range(start + 1, len(blocks)) if block_red(i).strip() == a.end_heading), None)
    if end is None:
        raise SystemExit('end heading not found')

    cfg = dict(source_id=a.source_id, seq_start=a.seq_start,
               import_genres=sorted(IMPORT_GENRES_DEFAULT | (PRAYER_GENRES if a.include_prayers else set())))
    p = Parser(blocks, notes, leaf_info, (start, end), cfg)
    rows = p.run()
    rows = postprocess(rows, cfg, leaf_info)
    if a.ci_export:
        propose_cids(rows, load_ci(a.ci_export))

    os.makedirs(a.outdir, exist_ok=True)
    imp = import_rows(rows, cfg)
    write_csv(os.path.join(a.outdir, 'import.csv'), IMPORT_COLS, imp, enc='utf-8')
    mrows = [OrderedDict((c, r[c]) for c in MASTER_COLS) for r in rows]
    write_csv(os.path.join(a.outdir, 'master.csv'), MASTER_COLS, mrows)
    voc = std_vocab(rows)
    with open(os.path.join(a.outdir, 'std_vocab.json'), 'w', encoding='utf-8') as fh:
        json.dump([[k[0], k[1], v] for k, v in sorted(voc.items(), key=lambda x: x[0][0].lower())], fh,
                  ensure_ascii=False)
    if a.json:
        with open(os.path.join(a.outdir, 'master.json'), 'w', encoding='utf-8') as fh:
            json.dump(mrows, fh, ensure_ascii=False, indent=1)
    print('rows: master=%d import=%d  blocks %d..%d' % (len(rows), len(imp), start, end))
    return rows

if __name__ == '__main__':
    main()
