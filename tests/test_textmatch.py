from __future__ import annotations

import pytest

from app.textmatch import (compare, distinctive_word, literal_query, query_words,
                           tolerant_query, words)

CI = ("Homo dei ducebatur ut crucifigerent eum populus autem clamabat voce magna "
      "dicens innocens ejus sanguis sine causa damnatur")
LOCAL = ("Homo dei ducebatur ut crucifigeretur populus autem clamabat voce magna "
         "dicens innocens eius sanguis sine causa damnatur")


def test_normalisation_bridges_spelling():
    assert words("Hec celi preciosa eius alleluya") == \
        words("Haec caeli pretiosa ejus alleluia")
    assert words("Quídam, / michi!") == ["quidam", "mihi"]
    assert words("Hæc cœli") == words("Haec coeli") == ["hec", "celi"]


@pytest.mark.parametrize("local, ci, cls, reason_part", [
    (LOCAL, CI, "minor", "1 word differs"),
    ("Hec est vera fraternitas", "Haec est vera fraternitas", "identical", "apart from spelling"),
    ("Hic est vir", "Hic est vir.", "identical", "the same"),
    (LOCAL + " alleluia alleluia", CI, "minor", "1 word differs"),
    ("Alleluia " + LOCAL, CI, "suffix", "begins with Alleluia"),
    ("Ecce " + LOCAL, CI, "suffix", "opening differs (1 word)"),
    (LOCAL, CI + " et iterum clamabant crucifige crucifige eum", "suffix",
     "6-word phrase in the Cantus Index text only, at the end"),
    (LOCAL, "Homo dei ducebatur ut crucifigerent eum magna turba sequebatur et mulieres flebant "
            "populus autem clamabat voce magna dicens innocens ejus sanguis sine causa damnatur",
     "suffix", "in the middle"),
    (LOCAL, "Vidit dominus Petrum et Andream et vocavit eos", "new", "differ throughout"),
    ("Dominus regit me", "Domine regis me", "minor", "word endings"),
])
def test_classification(local, ci, cls, reason_part):
    result = compare(local, ci)
    assert result.text_class == cls
    assert reason_part in result.reason


def test_incipit_only_record_is_incomplete():
    result = compare(LOCAL, "Homo dei*")
    assert result.text_class == "incomplete"
    assert "only the opening (2 words)" in result.reason


def test_cue_compares_openings_only():
    assert compare("Homo dei", CI, local_is_cue=True).text_class == "identical"
    assert compare("Homo dey", CI, local_is_cue=True).text_class == "identical"
    assert compare("Vidit dominus", CI, local_is_cue=True).text_class == "new"


def test_scores_rank_closer_texts_higher():
    close = compare(LOCAL, CI).score
    far = compare(LOCAL, CI + " et iterum clamabant crucifige crucifige eum").score
    assert 0 < far < close <= 1


def test_queries():
    ws = query_words("Hec est, vera preciosa!")
    assert literal_query(ws, 3) == "hec est vera"
    assert tolerant_query(ws, 4) == "h%ec %est _%era pr%e__osa"
    assert distinctive_word(query_words(LOCAL)) == "crucifigeretur"
    assert distinctive_word(["et", "in"]) is None
