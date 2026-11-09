# nidaros-cantus

Working database and review tools for preparing the Nidaros office and mass books
(Breviarium Nidrosiense, later Missale Nidrosiense) for import into the Cantus Database.

The SQLite database in `data/` is the source of truth. Parser output is imported into it
once per stretch of the book; workbooks and the Cantus import CSV are exports. Cantus IDs
are never assigned automatically: candidates are proposals, and only a Confirm or Fixed
decision puts a CID into the import file.

## Setup

Python 3.10 or later. In a fresh environment:

```
pip install -r requirements.txt
```

## Initialising the database

From the repository root:

```
python -m app init
python -m app add-source brev "Breviarium Nidrosiense" \
    --edition "Nasjonalbiblioteket (2019) HTML edition of the Nidaros breviary (printed 1519)"
python -m app import data/import/breviarium_andrew-lucy_pilot_master.xlsx --source brev
python -m app status
```

`init` creates `data/nidaros.sqlite` and will not overwrite an existing file. Every command
accepts `--db PATH` before the command name to use a different file.

For the pilot, `status` should report 565 lines, 319 import rows and 87 lines with an open
flag, matching the workbook's Summary sheet.

## What an import does

An import adds new pages to a book and never modifies lines already in the database. It is
refused if any page in the workbook already has lines for that book, or if the same file
has been imported before. The whole import runs in one transaction and is rolled back if
anything fails, including the final check that the values the database derives (incipit,
position, in_import, import_seq) reproduce the workbook's own columns.

Besides the lines, an import carries over the review flags (one row per flag), the
Spot-check list, any CID candidates and decisions already in the workbook, the flag legend
from the README sheet, and the connections between lines: responsory verse to responsory,
hymn stanza and refrain to hymn, and incipit-only item to the line holding its full text
when that line is in the same workbook.

## Layout

```
app/
  __main__.py   command line (init, add-source, import, status)
  schema.sql    database schema, views and reference vocabularies
  db.py         connection, initialisation, logged edits, decisions
  importer.py   master workbook import
pipeline/       parser scripts that produce the master workbooks
data/
  import/       workbooks that have been imported
  nidaros.sqlite
tests/
```

## Data model

| Table | Holds |
|---|---|
| `source` | One row per book |
| `import_batch` | Each imported workbook: file, checksum, page range |
| `line` | Every line of the book, chant or not, in book order |
| `line_link` | Connections between lines (verse_of, stanza_of, refrain_of, full_text_in) |
| `flag`, `flag_type` | Review flags per line, with status, and their meanings |
| `spot_check` | Lines picked out for checking, with status and notes |
| `candidate` | Proposed Cantus IDs per line, with score and method |
| `decision` | Confirm / Reject / Fixed decisions; append-only, latest one counts |
| `edit_log` | Every change made to a line after import |
| `api_cache` | Raw Cantus Index and Cantus Database responses |
| `genre`, `office` | Cantus genre and service codes |

Book order is `(page_start, line_on_page)`. `line_on_page` is numbered in steps of 10 so
lines can be inserted later without renumbering. `incipit`, `position`, `in_import` and
`import_seq` are derived rather than stored, so they stay correct when texts are corrected
or lines are added; the `line_export` view computes the last three.

## Tests

```
pip install pytest
python -m pytest
```
