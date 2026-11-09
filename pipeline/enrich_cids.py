#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_cids.py -- PROPOSE Cantus Index candidates for the chant rows of the master workbook.

  * Writes only `cid_proposed` ("CID (score); CID (score)"), `cid_candidate_text` (text of the best
    candidate) and the review flags CID_PROPOSED / CID_NO_MATCH.  It never touches `cantus_id`, and it never
    overwrites a row that already has a `cid_decision` (or, without --refresh, a `cid_proposed`).
  * Run it on your own machine (the Cantus Index host is not reachable from the sandbox this was written in).

Two sources:
  api  (default)  GET {base}/json-text/{search string}  -- chants whose full text starts with the string
                  (documented at github.com/dact-chant/cantus-index). The JSON key names are not documented, so the
                  parser accepts several spellings; use --dump-raw 2 to save raw responses if nothing is read.
  csv             --ci-export FILE.csv with a CID column and a text column (offline; no network).

Examples
  python enrich_cids.py master.xlsx                      # in place (a .bak copy is made first)
  python enrich_cids.py master.xlsx --limit 20 --dry-run # try 20 rows, change nothing
  python enrich_cids.py master.xlsx --ci-export ci.csv   # offline
"""
import re, sys, os, csv, json, time, shutil, argparse, urllib.request, urllib.parse, urllib.error
from collections import OrderedDict, defaultdict
from openpyxl import load_workbook

CHANT_GENRES = {'A', 'R', 'V', 'W', 'H', 'I'}
GENRE_CODES = {'A': 'A', 'R': 'R', 'V': 'V', 'W': 'W', 'H': 'H', 'I': 'I'}
UA = 'nidaros-cid-enrich/0.1 (research use)'

# ----------------------------------------------------------------------------
# text normalisation (same folding as the parser: case, u/v, i/j, punctuation)
# ----------------------------------------------------------------------------
def norm_words(s):
    s = (s or '').lower().replace('j', 'i').replace('v', 'u')
    s = re.sub(r'[\u2039\u203a|*]', ' ', s)
    s = re.sub(r'[^a-z ]+', ' ', s)
    return s.split()

def score_candidate(row_words, cand_words):
    """share of the shorter text that agrees from the first word on (0..1)"""
    if not row_words or not cand_words:
        return 0.0
    lead = 0
    for a, b in zip(row_words, cand_words):
        if a != b:
            break
        lead += 1
    return lead / float(min(len(row_words), len(cand_words)))

# ----------------------------------------------------------------------------
# backends
# ----------------------------------------------------------------------------
def _first(d, keys):
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ''):
            return low[k]
    return ''

def parse_results(data):
    """-> list of dict(cid, text, genre); tolerant about the response shape"""
    if isinstance(data, dict):
        for key in ('results', 'chants', 'data'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = list(data.values())
    out = []
    for v in data if isinstance(data, list) else []:
        if not isinstance(v, dict):
            continue
        cid = _first(v, ['cid', 'cantus_id', 'cantusid', 'cantus id', 'id'])
        text = _first(v, ['fulltext', 'full_text', 'full text', 'text', 'incipit', 'chant'])
        genre = _first(v, ['genre', 'genre_id', 'genrecode'])
        if cid and text:
            out.append(dict(cid=str(cid).strip(), text=str(text), genre=str(genre).strip()))
    return out

class ApiBackend:
    def __init__(self, base, sleep, cache_path, retries=3, timeout=30, dump_raw=0):
        self.base = base.rstrip('/')
        self.sleep = sleep
        self.cache_path = cache_path
        self.cache = {}
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding='utf-8') as fh:
                self.cache = json.load(fh)
        self.retries = retries
        self.timeout = timeout
        self.dump_raw = dump_raw
        self.errors = 0
        self.calls = 0
        self.unparsed = 0

    def fetch_json(self, url):
        last = None
        for k in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode('utf-8-sig'))   # CI sends a BOM before the JSON
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                last = e
                time.sleep(1.5 * (k + 1))
        raise RuntimeError('request failed: %s (%s)' % (url, last))

    def search(self, text):
        key = text
        if key in self.cache:
            return self.cache[key]
        url = '%s/json-text/%s' % (self.base, urllib.parse.quote(text, safe=''))
        data = self.fetch_json(url)
        self.calls += 1
        if self.dump_raw > 0:
            self.dump_raw -= 1
            with open('cantus_index_raw_%d.json' % self.calls, 'w', encoding='utf-8') as fh:
                json.dump({'url': url, 'response': data}, fh, ensure_ascii=False, indent=1)
        res = parse_results(data)
        if data and not res:
            self.unparsed += 1
        self.cache[key] = res
        time.sleep(self.sleep)
        return res

    def save(self):
        if self.cache_path:
            with open(self.cache_path, 'w', encoding='utf-8') as fh:
                json.dump(self.cache, fh, ensure_ascii=False)

class CsvBackend:
    def __init__(self, path):
        self.idx2 = defaultdict(list)
        self.idx1 = defaultdict(list)
        with open(path, newline='', encoding='utf-8-sig') as fh:
            rd = csv.DictReader(fh)
            cols = {c.lower().strip(): c for c in rd.fieldnames}
            ccid = next((cols[k] for k in ('cid', 'cantus_id', 'cantus id') if k in cols), None)
            ctxt = next((cols[k] for k in ('fulltext', 'full_text', 'text', 'incipit', 'fulltext_standardized')
                         if k in cols), None)
            cgen = next((cols[k] for k in ('genre', 'genre_id') if k in cols), None)
            if not ccid or not ctxt:
                raise SystemExit('CI export needs a CID column and a text/incipit column')
            for rec in rd:
                w = norm_words(rec[ctxt])
                item = dict(cid=rec[ccid].strip(), text=rec[ctxt], genre=(rec.get(cgen) or '').strip() if cgen else '')
                if len(w) >= 2:
                    self.idx2[(w[0], w[1])].append(item)
                if w:
                    self.idx1[w[0]].append(item)
        self.errors = self.calls = self.unparsed = 0

    def search(self, text):
        w = norm_words(text)
        if len(w) >= 2:
            return list(self.idx2.get((w[0], w[1]), []))
        return list(self.idx1.get(w[0], [])) if w else []

    def save(self):
        pass

# ----------------------------------------------------------------------------
# core
# ----------------------------------------------------------------------------
def candidates_for(backend, text, genre, nwords, top, min_score, try_lower=False):
    words = text.replace('*', ' ').split()
    if not words:
        return [], 'no text'
    tries = []
    for n in (nwords, 3):
        n = min(n, len(words))
        variants = [' '.join(words[:n])]
        if try_lower:
            variants.append(' '.join([words[0].lower()] + words[1:n]))
        for q in variants:
            if q and q not in tries:
                tries.append(q)
    rw = norm_words(text)
    found = None
    for q in tries:
        res = backend.search(q)
        if res:
            found = res
            break
    if found is None:
        return [], 'ok'
    best = {}
    for c in found:
        g = c.get('genre', '')
        want = GENRE_CODES.get(genre, '')
        if want and g and len(g) <= 3 and g.upper() != want:
            continue
        sc = score_candidate(rw, norm_words(c['text']))
        if sc < min_score:
            continue
        gap = abs(len(norm_words(c['text'])) - len(rw))
        cur = best.get(c['cid'])
        if cur is None or (sc, -gap) > (cur[0], -cur[1]):
            best[c['cid']] = (sc, gap, c['text'])
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))[:top]
    return [(cid, v[0], v[2]) for cid, v in ranked], 'ok'

def add_flag(cell, flag):
    cur = (cell.value or '')
    flags = [x.strip() for x in str(cur).split(';') if x.strip()]
    flags = [x for x in flags if x not in ('CID_PROPOSED', 'CID_NO_MATCH')]
    flags.append(flag)
    cell.value = '; '.join(flags)

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('xlsx')
    ap.add_argument('--out', default=None, help='write here instead of in place (in place makes FILE.bak.xlsx)')
    ap.add_argument('--sheet', default='Master')
    ap.add_argument('--ci-export', default=None, help='offline CSV instead of the API')
    ap.add_argument('--base-url', default='https://cantusindex.org')
    ap.add_argument('--words', type=int, default=5, help='words of the standardized text used as the search string')
    ap.add_argument('--top', type=int, default=3)
    ap.add_argument('--min-score', type=float, default=0.6)
    ap.add_argument('--sleep', type=float, default=1.0, help='seconds between API calls')
    ap.add_argument('--cache', default='cantus_index_cache.json')
    ap.add_argument('--refresh', action='store_true', help='re-query rows that already have cid_proposed')
    ap.add_argument('--try-lower', action='store_true', help='also retry with a lower-cased first word (if the search is case-sensitive)')
    ap.add_argument('--limit', type=int, default=0, help='stop after N queried rows (testing)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--dump-raw', type=int, default=0, help='save the first N raw API responses to files')
    a = ap.parse_args(argv)

    wb = load_workbook(a.xlsx)
    ws = wb[a.sheet]
    head = {str(c.value): i + 1 for i, c in enumerate(ws[1]) if c.value}
    need = ['genre', 'in_import', 'fulltext_standardized', 'cid_proposed', 'cid_candidate_text', 'cid_decision',
            'review_flags', 'master_seq']
    missing = [n for n in need if n not in head]
    if missing:
        raise SystemExit('Master sheet lacks columns: %s' % ', '.join(missing))

    backend = CsvBackend(a.ci_export) if a.ci_export else ApiBackend(a.base_url, a.sleep, a.cache, dump_raw=a.dump_raw)
    todo = sum(1 for r in range(2, ws.max_row + 1)
               if str(ws.cell(r, head['in_import']).value or '').strip().lower() == 'yes'
               and str(ws.cell(r, head['genre']).value or '').strip() in CHANT_GENRES
               and not str(ws.cell(r, head['cid_decision']).value or '').strip()
               and (a.refresh or not str(ws.cell(r, head['cid_proposed']).value or '').strip()))
    if not a.ci_export:
        print('%d rows to query; about %.0f minutes at %.1f s per request (cached answers are reused)'
              % (todo, todo * 2 * a.sleep / 60.0, a.sleep))
    done = nomatch = skipped = failures = 0
    consecutive = 0
    for r in range(2, ws.max_row + 1):
        g = lambda name: ws.cell(r, head[name]).value
        genre = str(g('genre') or '').strip()
        if str(g('in_import') or '').strip().lower() != 'yes' or genre not in CHANT_GENRES:
            continue
        if str(g('cid_decision') or '').strip():
            skipped += 1
            continue
        if str(g('cid_proposed') or '').strip() and not a.refresh:
            skipped += 1
            continue
        text = str(g('fulltext_standardized') or '')
        try:
            cands, status = candidates_for(backend, text, genre, a.words, a.top, a.min_score, a.try_lower)
            consecutive = 0
        except RuntimeError as e:
            failures += 1
            consecutive += 1
            print('seq %s: %s' % (g('master_seq'), e), file=sys.stderr)
            if consecutive >= 5:
                print('5 failures in a row: stopping (check your connection / the base URL).', file=sys.stderr)
                break
            continue
        done += 1
        if cands:
            ws.cell(r, head['cid_proposed']).value = '; '.join('%s (%.2f)' % (c, s) for c, s, _ in cands)
            ws.cell(r, head['cid_candidate_text']).value = cands[0][2][:160]
            add_flag(ws.cell(r, head['review_flags']), 'CID_PROPOSED')
        else:
            ws.cell(r, head['cid_proposed']).value = None
            ws.cell(r, head['cid_candidate_text']).value = None
            add_flag(ws.cell(r, head['review_flags']), 'CID_NO_MATCH')
            nomatch += 1
        if a.limit and done >= a.limit:
            break
        if done % 25 == 0:
            print('  %d rows queried ...' % done)
            if not a.dry_run:
                backend.save()
    backend.save()
    print('queried %d rows: %d with candidates, %d without; %d skipped (already proposed/decided); %d failed'
          % (done, done - nomatch, nomatch, skipped, failures))
    if getattr(backend, 'unparsed', 0):
        print('WARNING: %d responses were not empty but no CID/text could be read from them. '
              'Re-run with --dump-raw 2 and check the key names in cantus_index_raw_*.json.' % backend.unparsed,
              file=sys.stderr)
    if a.dry_run:
        print('dry run: nothing saved')
        return
    out = a.out or a.xlsx
    if not a.out:
        bak = re.sub(r'\.xlsx$', '', a.xlsx) + '.bak.xlsx'
        shutil.copy2(a.xlsx, bak)
        print('backup: %s' % bak)
    wb.calculation.fullCalcOnLoad = True
    wb.save(out)
    print('saved: %s' % out)

if __name__ == '__main__':
    main()
