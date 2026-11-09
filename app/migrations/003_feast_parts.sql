-- Version 3: an editorial note per line for the import file's "extra" column
-- (after the printed rubric), and flags for the feast headings and Second
-- Vespers that the parser now supplies or infers.

ALTER TABLE line ADD COLUMN extra_note TEXT;

INSERT INTO flag_type (code, meaning) VALUES
    ('FEAST_SUPPLIED', 'Feast heading not printed; supplied (see extra_note). Map it to a Cantus feast'),
    ('OFFICE_V2_INFERRED', '"Ad vesperas" without primas/secundas after Lauds or a day hour of the same feast; coded V2')
ON CONFLICT(code) DO NOTHING;
