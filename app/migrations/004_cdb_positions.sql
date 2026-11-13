-- Version 4: Cantus Database positions.
--
-- Positions are derived by Cantus Database rules (app/positions.py); where the
-- rules need a judgement the line is flagged POSITION_CHECK, and a position set
-- by hand in position_override always wins. Hymn verses are their own genre in
-- Cantus Index (e.g. 830381a, ah51067:02), so HV is compared as HV.

ALTER TABLE line ADD COLUMN position_override TEXT;

UPDATE genre SET ci_genre = 'HV' WHERE code = 'HV' AND ci_genre IS NULL;

INSERT INTO flag_type (code, meaning) VALUES
    ('POSITION_CHECK', 'Cantus Database position needs a judgement (see note); set it with `edit LINE position_override VALUE` if the proposal is wrong')
ON CONFLICT(code) DO NOTHING;
