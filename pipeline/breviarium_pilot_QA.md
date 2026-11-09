# Pilot QA: Breviarium Nidrosiense, St Andrew to Lucy (pp. 553-577)

**Input:** NB 2019 HTML edition (full book, parsed once; the pilot range is cut by heading).
**Output:** 565 master lines, of which 319 are chant rows in the import CSV
(A 132, R 61, V 51, W 37, H 15, HV 15, I 8). Everything else (lessons, homilies, psalm cues,
orations, capitula, rubrics, headings) stays in the master.

## Checked
- Import header is identical to `chant-import-template.csv` (27 columns, same order).
- Sign glyphs in the source range: 130 Ⱥ, 61 ℟, 88 ℣. Parsed: 130 + 2 antiphons printed without Ⱥ = 132 A,
  61 R, 88 V+W. No glyph is left inside any chant text.
- Workbook totals (formulas) recomputed with LibreOffice: 0 errors, and they equal an independent count from the CSV.
- `cantus_id`, `mode`, `finalis`, `differentia`, `volpiano`, `source_id` are empty in every import row.

## Needs your eyes (87 flagged rows; see Spot-check sheet for 35 rows to compare with the facsimile)
- 18 items with no hour rubric before them (mostly Nicholas first vespers); office left blank.
- 6 offices inferred from an Invitatorium; 2 "Ad vesperas" coded V provisionally.
- 24 incipit-only items resolved to the cited leaf; 2 ambiguous, 4 unresolved (3 hymns, 1 antiphon);
  3 verbal cross-references ("vt sequitur", "ibidem") not resolved.
- 1 responsory (p. 562, "Qui cum audissent...") with no locatable repetendum.
- 3 mixed rubric/text lines kept as rubric rows; 3 text blocks without a heading.

## Not done / limits
- Standardized spelling is provisional: u/v, i/j, michi>mihi, nichil>nihil only (338 changed forms listed in
  "Std changes"). ae/oe are not restored and capitals are untouched. The CDB text-entry guideline sits behind a
  Google Drive link that could not be opened, so it has not been applied.
- No Cantus IDs yet. `enrich_cids.py` (run locally) proposes candidates from Cantus Index `json-text`; it was
  tested only against a mock server, never the live API. `cantus_id` is filled only by `export_import.py`,
  for rows whose `cid_decision` is Confirm or Fixed.
- The single-letter references after psalms (a.vj., d.v. ...) are copied to indexing_notes, not interpreted.
- Responsory repetendum placement (the | stroke) is a heuristic based on the cue at the end of the verse.

## To decide
1. Cantus source_id for the Breviary.
2. Is edition page = facsimile page? (sample images s490/s530/s536/s732 suggest yes.)
3. Hymns as H + HV rows, or another method (Appendix 1.H)?
4. Share the CDB text-entry guideline PDF so the standardization rules can be matched to it.

## Workflow after the pilot
1. Fix rows in Master (the workbook is now the source of truth).
2. `python enrich_cids.py breviarium_andrew-lucy_pilot_master.xlsx` (makes a .bak copy; use `--limit 20 --dry-run` first).
3. Set `cid_decision` (Confirm / Reject / Fixed) in Master.
4. `python export_import.py breviarium_andrew-lucy_pilot_master.xlsx --out import.csv --source-id <ID>`.
