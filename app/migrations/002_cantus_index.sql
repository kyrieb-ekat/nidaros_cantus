-- Version 2: Cantus Index lookup, feast mapping, and decisions for chants
-- that need a variant or a new Cantus ID.

-- Cantus Index genre code(s) that correspond to each local genre code.
-- Comma-separated; NULL means not yet confirmed, so genre is not compared.
ALTER TABLE genre ADD COLUMN ci_genre TEXT;

UPDATE genre SET ci_genre = code WHERE code IN ('A', 'R', 'V', 'W', 'H', 'I');

-- Each feast heading of a book, linked to a Cantus Index feast name.
--   exact:     the Cantus Index feast is the same occasion
--   variant:   the closest Cantus Index feast, but not an exact match
--   unmapped:  no Cantus Index feast fits
CREATE TABLE feast_map (
    source_id     INTEGER NOT NULL REFERENCES source(id),
    feast         TEXT NOT NULL,          -- heading as it appears in line.feast
    ci_feast      TEXT,                   -- Cantus Index feast name
    ci_feastcode  TEXT,
    status        TEXT NOT NULL CHECK (status IN ('exact', 'variant', 'unmapped')),
    note          TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (source_id, feast),
    CHECK ((status = 'unmapped') = (ci_feast IS NULL))
);

-- How each candidate's text relates to the line's text, following the
-- Cantus Index text-variant guidelines (advisory only):
--   identical, minor (keep the CID), incomplete (Cantus Index record has only
--   an incipit), suffix (looks like a variant of this CID), new (different text)
ALTER TABLE candidate ADD COLUMN text_class TEXT
    CHECK (text_class IN ('identical', 'minor', 'incomplete', 'suffix', 'new'));
ALTER TABLE candidate ADD COLUMN class_reason TEXT;
ALTER TABLE candidate ADD COLUMN genre_match TEXT
    CHECK (genre_match IN ('match', 'mismatch', 'unknown'));

-- Decisions gain two statuses. Neither carries a CID: Cantus Index assigns
-- the code later, and it is recorded then as a Fixed decision.
--   Variant:  a variant of parent_cid (required)
--   New CID:  needs a new Cantus ID; parent_cid optional, for reference
DROP VIEW current_decision;

CREATE TABLE decision_v2 (
    id          INTEGER PRIMARY KEY,
    line_id     INTEGER NOT NULL REFERENCES line(id) ON DELETE CASCADE,
    status      TEXT NOT NULL
                CHECK (status IN ('Confirm', 'Reject', 'Fixed', 'Variant', 'New CID')),
    cid         TEXT,
    parent_cid  TEXT,
    reason      TEXT,
    note        TEXT,
    decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (status NOT IN ('Confirm', 'Fixed') OR cid IS NOT NULL),
    CHECK (status NOT IN ('Variant', 'New CID') OR cid IS NULL),
    CHECK (status <> 'Variant' OR parent_cid IS NOT NULL)
);

INSERT INTO decision_v2 (id, line_id, status, cid, note, decided_at)
SELECT id, line_id, status, cid, note, decided_at FROM decision;

DROP TABLE decision;
ALTER TABLE decision_v2 RENAME TO decision;
CREATE INDEX decision_by_line ON decision (line_id, id);

CREATE VIEW current_decision AS
SELECT d.*
FROM decision AS d
WHERE d.id = (SELECT MAX(d2.id) FROM decision AS d2 WHERE d2.line_id = d.line_id);

-- Flags raised by the Cantus Index lookup. They are refreshed on every fetch;
-- a flag you have cleared stays cleared.
INSERT INTO flag_type (code, meaning) VALUES
    ('CI_VARIANT', 'Closest Cantus Index text of the same genre looks like a variant (see note for the probable parent)'),
    ('CI_GENRE_NEW_CID', 'The text matches a Cantus ID of a different genre; a new Cantus ID is needed'),
    ('CI_TEXT_INCOMPLETE', 'The matching Cantus Index record has only an incipit; this line has the full text'),
    ('CI_NO_MATCH', 'No Cantus Index text found that is close to this one')
ON CONFLICT(code) DO UPDATE SET meaning = excluded.meaning;
