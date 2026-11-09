-- Nidaros Cantus working database: initial schema (version 1).
--
-- The database is the source of truth. Workbooks and the Cantus import CSV
-- are exports. Values that can be computed from other columns (incipit,
-- position, in_import, import_seq) are not stored; see the line_export view.

-- Reference vocabularies --------------------------------------------------

CREATE TABLE genre (
    code      TEXT PRIMARY KEY,
    label     TEXT NOT NULL,
    imported  INTEGER NOT NULL CHECK (imported IN (0, 1))  -- 1 = goes into the Cantus import file
);

CREATE TABLE office (
    code   TEXT PRIMARY KEY,
    label  TEXT NOT NULL
);

CREATE TABLE flag_type (
    code     TEXT PRIMARY KEY,
    meaning  TEXT
);

-- Books and imports -------------------------------------------------------

CREATE TABLE source (
    id             INTEGER PRIMARY KEY,
    siglum         TEXT NOT NULL UNIQUE,   -- short key used on the command line
    title          TEXT NOT NULL,
    edition        TEXT,
    cdb_source_id  INTEGER
);

CREATE TABLE import_batch (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES source(id),
    file_name    TEXT NOT NULL,
    file_sha256  TEXT NOT NULL UNIQUE,
    page_first   INTEGER NOT NULL,
    page_last    INTEGER NOT NULL,
    line_count   INTEGER NOT NULL,
    imported_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Lines: every line of the book, chant or not ------------------------------
-- Order within a book is (page_start, line_on_page). line_on_page is numbered
-- in steps of 10 so that lines can be inserted later without renumbering.

CREATE TABLE line (
    id                     INTEGER PRIMARY KEY,
    source_id              INTEGER NOT NULL REFERENCES source(id),
    batch_id               INTEGER NOT NULL REFERENCES import_batch(id),
    origin_ref             TEXT,           -- e.g. "master_seq 4" in the imported workbook
    page_start             INTEGER NOT NULL,
    line_on_page           INTEGER NOT NULL,
    page_end               INTEGER,
    facsimile_ref          TEXT,
    signature_leaf         TEXT,
    row_type               TEXT NOT NULL,
    content_structure      TEXT,
    feast                  TEXT,
    sub_section            TEXT,
    office                 TEXT REFERENCES office(code),
    office_printed         TEXT,
    nocturn                INTEGER,
    slot                   TEXT,
    genre                  TEXT REFERENCES genre(code),
    sign                   TEXT,
    fulltext_edition       TEXT,
    fulltext_ms            TEXT,
    fulltext_standardized  TEXT,
    rubric                 TEXT,
    xref_raw               TEXT,
    xref_leaf              TEXT,
    incipit_only           INTEGER NOT NULL DEFAULT 0 CHECK (incipit_only IN (0, 1)),
    xref_resolved_page     TEXT,           -- text: can hold several pages ("496, 496")
    xref_resolved_text     TEXT,
    repetendum_note        TEXT,
    variant_notes          TEXT,
    indexing_notes         TEXT,
    UNIQUE (source_id, page_start, line_on_page)
);

CREATE INDEX line_by_feast ON line (source_id, feast, office);
CREATE INDEX line_by_genre ON line (genre);

-- Connections between lines --------------------------------------------------
-- verse_of:      responsory verse -> its responsory
-- stanza_of:     hymn stanza (HV) -> the hymn's first stanza (H)
-- refrain_of:    hymn refrain -> the hymn's first stanza (H)
-- full_text_in:  incipit-only line -> the line holding its full text

CREATE TABLE line_link (
    id         INTEGER PRIMARY KEY,
    from_line  INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    to_line    INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('verse_of', 'stanza_of', 'refrain_of', 'full_text_in')),
    origin     TEXT NOT NULL CHECK (origin IN ('import', 'manual')),
    UNIQUE (from_line, kind, to_line)
);

-- Review flags, one row per flag -------------------------------------------

CREATE TABLE flag (
    id          INTEGER PRIMARY KEY,
    line_id     INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    code        TEXT NOT NULL REFERENCES flag_type(code),
    ord         INTEGER NOT NULL,       -- order as printed in the workbook
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'cleared')),
    cleared_at  TEXT,
    note        TEXT,
    UNIQUE (line_id, code)
);

-- Spot-check list carried over from the workbook --------------------------

CREATE TABLE spot_check (
    line_id     INTEGER PRIMARY KEY REFERENCES line(id) ON DELETE CASCADE,
    reason      TEXT,
    status      TEXT NOT NULL CHECK (status IN ('To check', 'OK', 'Fixed in Master', 'Needs discussion')),
    note        TEXT,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Cantus ID candidates and decisions ---------------------------------------
-- Candidates are proposals only. A CID reaches the import file solely through
-- a Confirm or Fixed decision.

CREATE TABLE candidate (
    id          INTEGER PRIMARY KEY,
    line_id     INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    cid         TEXT NOT NULL,
    score       REAL,
    rank        INTEGER,
    method      TEXT NOT NULL,          -- how it was found, e.g. 'enrich_cids', 'ci_text_search'
    cand_text   TEXT,
    cand_genre  TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (line_id, cid, method)
);

-- Append-only: the current decision for a line is its latest row.
CREATE TABLE decision (
    id          INTEGER PRIMARY KEY,
    line_id     INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    status      TEXT NOT NULL CHECK (status IN ('Confirm', 'Reject', 'Fixed')),
    cid         TEXT,
    note        TEXT,
    decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (status = 'Reject' OR cid IS NOT NULL)
);

CREATE INDEX decision_by_line ON decision (line_id, id);

CREATE VIEW current_decision AS
SELECT d.*
FROM decision AS d
WHERE d.id = (SELECT MAX(d2.id) FROM decision AS d2 WHERE d2.line_id = d.line_id);

-- Change history for edits made to lines after import ----------------------

CREATE TABLE edit_log (
    id         INTEGER PRIMARY KEY,
    line_id    INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    note       TEXT,
    edited_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Raw responses from Cantus Index / Cantus Database ------------------------

CREATE TABLE api_cache (
    url           TEXT PRIMARY KEY,
    http_status   INTEGER NOT NULL,
    content_type  TEXT,
    body          TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Derived values ---------------------------------------------------------------
-- (incipit, the first six words of the standardized text, is computed in
-- app/db.py because SQLite has no convenient word-splitting function.)
-- position:    running count of genre-bearing lines within book, section,
--              feast and office, in book order (nocturns continue the count)
-- in_import:   1 if the genre is an imported genre
-- import_seq:  running order of imported lines within the book

CREATE VIEW line_export AS
WITH positioned AS (
    SELECT id,
           printf('%02d', ROW_NUMBER() OVER (
               PARTITION BY source_id, content_structure, feast, office
               ORDER BY page_start, line_on_page)) AS position
    FROM line
    WHERE genre IS NOT NULL
),
imported AS (
    SELECT l.id,
           ROW_NUMBER() OVER (PARTITION BY l.source_id
                              ORDER BY l.page_start, l.line_on_page) AS import_seq
    FROM line AS l
    JOIN genre AS g ON g.code = l.genre
    WHERE g.imported = 1
)
SELECT l.*,
       p.position,
       CASE WHEN i.id IS NULL THEN 0 ELSE 1 END AS in_import,
       i.import_seq
FROM line AS l
LEFT JOIN positioned AS p ON p.id = l.id
LEFT JOIN imported AS i ON i.id = l.id;

-- Seed data ----------------------------------------------------------------

-- Genres used by the pilot (labels for Cap, Or, Co follow the printed rubrics).
INSERT INTO genre (code, label, imported) VALUES
    ('A',   'Antiphon',          1),
    ('R',   'Responsory',        1),
    ('V',   'Responsory verse',  1),
    ('W',   'Versicle',          1),
    ('H',   'Hymn',              1),
    ('HV',  'Hymn verse',        1),
    ('I',   'Invitatory',        1),
    ('Cap', 'Capitulum',         0),
    ('Or',  'Oratio',            0),
    ('Co',  'Collecta',          0);

-- Cantus Database service codes.
INSERT INTO office (code, label) VALUES
    ('V',  'First Vespers'),
    ('V2', 'Second Vespers'),
    ('M',  'Matins'),
    ('L',  'Lauds'),
    ('P',  'Prime'),
    ('T',  'Terce'),
    ('S',  'Sext'),
    ('N',  'None'),
    ('C',  'Compline'),
    ('D',  'Day Hours'),
    ('R',  'Memorial'),
    ('E',  'Antiphons for the Magnificat or Benedictus (in evangelio)'),
    ('H',  'Antiphons based on texts from the Historia'),
    ('CA', 'Chapter'),
    ('MI', 'Mass'),
    ('X',  'Supplementary chants');
