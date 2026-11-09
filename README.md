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

## Upgrading the database

When the code moves to a new schema version, commands stop with a message asking for a
migration. Commit the database first, then run:

```
python -m app migrate
```

Each migration runs in a single transaction: it either completes or leaves the file as it was.

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

## Looking up Cantus IDs

The lookup uses the Cantus Index API, which covers Cantus Database and the other databases
in the Cantus Index network in one request. Every response is cached in the database, so a
lookup is only slow the first time; `--offline` re-ranks from the cache without the network.

1. Map each feast heading to a Cantus Index feast. This is what lets concordances count as
   evidence (same feast, same feast and office):

   ```
   python -m app feasts                      # headings and their mapping
   python -m app feasts --search andre       # find the Cantus Index name
   python -m app map-feast "IN UIGILIA SANCTI ANDREE APOSTOLI" --ci "Andreae"
   python -m app map-feast "<heading>" --ci "<Cantus Index name>" --variant
   python -m app map-feast "<heading>" --unmapped
   ```

   `--variant` marks the closest Cantus Index feast where none matches exactly; `--unmapped`
   records that none fits. Both are flagged in `feasts` and `status`.

2. Fetch candidates, for the whole book or one feast or line:

   ```
   python -m app fetch
   python -m app fetch --feast "LUCIE UIRGINIS ET MARTYRIS"
   python -m app fetch --line 46
   ```

   The command makes at most one request per second (`--interval` to change it) and can be
   stopped and restarted at any point. Set the `NIDAROS_CONTACT` environment variable to an
   email address to include it in the requests' User-Agent.

3. Review a line, then record a decision:

   ```
   python -m app show 46
   python -m app decide 46 confirm 006868
   python -m app decide <line> variant --parent <CID> --reason "phrase added at the end"
   python -m app decide <line> new-cid --parent <CID> --reason "same text is a versicle there"
   ```

   `confirm` and `fixed` take a Cantus ID. `variant` and `new-cid` never do: Cantus Index
   assigns the code later.

4. Send the variant and new-ID requests to Cantus Index, and record the codes they assign:

   ```
   python -m app export-requests data/requests.csv
   python -m app import-codes data/requests.csv
   ```

   The export has an empty `assigned_cid` column. Once it is filled in, importing the same
   file records each code as a Fixed decision; rows still blank are left waiting.

### How candidates are found and ranked

The search starts with the line's first three words, widening to six while it hits the
100-result cap. If nothing close turns up, it tries two words, then words two to four (which
finds a different opening), then the longest word. Searches tolerate spelling differences
between the Nidaros texts and Cantus Index (*hec/haec*, *celi/caeli*, *preciosa/pretiosa*,
*eius/ejus*). For an incipit-only line, the full text it refers to is used when known.

Each candidate is compared with the line's text following the Cantus Index text-variant
guidelines (https://cantusindex.org/variants) and given a class with a reason: identical,
minor (keep the CID), incomplete (the Cantus Index record is only an incipit), suffix
(looks like a variant), or new. Candidates of the line's genre always rank first. Texts are
compared after spelling normalisation; the rules are in `app/data/normalise.tsv` and can be
extended.

The lookup raises these flags, refreshed on every fetch (a flag you have cleared stays
cleared):

| Flag | Meaning |
|---|---|
| `CI_VARIANT` | The closest text of the same genre looks like a variant; the note names the probable parent |
| `CI_GENRE_NEW_CID` | The text matches a Cantus ID of a different genre, so a new ID is needed |
| `CI_TEXT_INCOMPLETE` | The matching Cantus Index record is only an incipit; this line has the full text |
| `CI_NO_MATCH` | Nothing close was found |

Genres are compared through `genre.ci_genre`. Hymn verses (HV) are not mapped yet; `fetch`
reports which Cantus Index genre their close matches have, and `genre-map HV <code>` sets it.

## Layout

```
app/
  __main__.py     command line
  migrations/     schema, one numbered SQL file per version
  data/           normalise.tsv (spelling rules)
  db.py           connection, migrations, logged edits, decisions
  importer.py     master workbook import
  cantus.py       Cantus Index client, cache, concordance evidence
  textmatch.py    normalisation, text comparison, search queries
  fetch.py        candidate lookup and CI flags
  feasts.py       feast list and feast mapping
  variants.py     requests for new Cantus IDs, and assigned codes
pipeline/         parser scripts that produce the master workbooks
data/
  import/         workbooks that have been imported
  nidaros.sqlite
tests/            run without network access, using saved Cantus Index responses
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
| `candidate` | Proposed Cantus IDs per line, with score, text class and reason |
| `decision` | Confirm / Reject / Fixed / Variant / New CID; append-only, latest one counts |
| `edit_log` | Every change made to a line after import |
| `api_cache` | Cantus Index responses |
| `feast_map` | Each feast heading's Cantus Index feast, and whether it is exact |
| `genre`, `office` | Genre and service codes, with each genre's Cantus Index code |

Book order is `(page_start, line_on_page)`. `line_on_page` is numbered in steps of 10 so
lines can be inserted later without renumbering. `incipit`, `position`, `in_import` and
`import_seq` are derived rather than stored, so they stay correct when texts are corrected
or lines are added; the `line_export` view computes the last three.

## Tests

```
pip install pytest
python -m pytest
```
