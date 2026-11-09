"""Compare chant texts with Cantus Index and build search queries.

Classification follows the Cantus Index text-variant guidelines
(https://cantusindex.org/variants). It is advisory: it says how a candidate's
text relates to the line's text, and why, so the decision stays with the
editor. Two parts of the guidelines can only be approximated:

* case endings and tenses are detected as words sharing a stem;
* the liturgical-tradition exception cannot be detected at all, which is why
  minor differences are reported rather than hidden.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

RULES_PATH = Path(__file__).parent / "data" / "normalise.tsv"

# Best first. Used to rank candidates.
CLASS_ORDER = {"identical": 0, "minor": 1, "incomplete": 2, "suffix": 3, "new": 4}

ALLELUIA = "alleluia"

LIGATURES = {"æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe"}


@dataclass(frozen=True)
class Comparison:
    score: float       # word-level similarity, 0-1
    text_class: str    # a key of CLASS_ORDER
    reason: str


# --------------------------------------------------------------------------
# Normalisation

@lru_cache(maxsize=None)
def load_rules(path: str = str(RULES_PATH)) -> tuple[tuple[re.Pattern, str], ...]:
    rules = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            raise ValueError(f"{path}, line {number}: expected pattern<TAB>replacement")
        pattern, replacement = line.split("\t", 1)
        rules.append((re.compile(pattern), replacement))
    return tuple(rules)


def plain(text: str | None) -> str:
    """Lower case, accents removed, punctuation replaced by spaces ('*' kept)."""
    if not text:
        return ""
    for ligature, letters in LIGATURES.items():
        text = text.replace(ligature, letters)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z*\s]", " ", stripped).split())


def words(text: str | None) -> list[str]:
    """Normalised words for comparison; asterisks are dropped."""
    result = plain(text).replace("*", " ")
    for pattern, replacement in load_rules():
        result = pattern.sub(replacement, result)
    return result.split()


def is_incipit_only(ci_text: str | None) -> bool:
    """Cantus Index marks abbreviated texts with an asterisk."""
    return bool(ci_text) and "*" in ci_text


# --------------------------------------------------------------------------
# Comparison

def compare(local_text: str, ci_text: str, local_is_cue: bool = False) -> Comparison:
    """Classify a Cantus Index text against the line's text.

    local_is_cue: the line gives only the opening of the chant (an incipit-only
    item whose full text could not be found), so only openings are compared.
    """
    a, b = words(local_text), words(ci_text)
    if not a or not b:
        return Comparison(0.0, "new", "one of the texts is empty")

    ci_short = is_incipit_only(ci_text)
    if ci_short and not local_is_cue and len(b) < len(a):
        score = _ratio(a[:len(b)], b)
        if score >= 0.75:
            return Comparison(score, "incomplete",
                              f"the Cantus Index record gives only the opening ({len(b)} words); "
                              f"this text has {len(a)}")
        return Comparison(score * len(b) / len(a), "new", "differs from the Cantus Index incipit")

    if local_is_cue or ci_short:
        n = min(len(a), len(b))
        score = _ratio(a[:n], b[:n])
        who = "this line is a cue" if local_is_cue else "the Cantus Index record gives only the opening"
        if a[:n] == b[:n]:
            return Comparison(1.0, "identical", f"{who}; the opening matches")
        if score >= 0.6:
            return Comparison(score, "minor", f"{who}; the opening differs slightly")
        return Comparison(score, "new", f"{who}; the opening differs")

    result = _compare_full(a, b)
    if result.text_class == "identical" and plain(local_text) != plain(ci_text).replace("*", "").strip():
        return Comparison(1.0, "identical", "the texts are the same apart from spelling")
    return result


def _compare_full(a: list[str], b: list[str]) -> Comparison:
    if a == b:
        return Comparison(1.0, "identical", "the texts are the same")

    # Beginning with Alleluia makes a variant; Alleluia elsewhere does not.
    if (a[0] == ALLELUIA) != (b[0] == ALLELUIA):
        rest = _compare_full(_drop_leading_alleluia(a), _drop_leading_alleluia(b)) \
            if _drop_leading_alleluia(a) and _drop_leading_alleluia(b) else None
        if rest and rest.text_class in ("identical", "minor"):
            who = "this text" if a[0] == ALLELUIA else "the Cantus Index text"
            return Comparison(rest.score, "suffix",
                              f"{who} begins with Alleluia; otherwise {rest.reason}")

    a2 = [w for w in a if w != ALLELUIA]
    b2 = [w for w in b if w != ALLELUIA]
    if not a2 or not b2:
        return Comparison(_ratio(a, b), "new", "the texts have no words in common")
    if a2 == b2:
        return Comparison(_ratio(a, b), "minor", "the texts differ only in Alleluia")

    matcher = SequenceMatcher(None, a2, b2, autojunk=False)
    score = matcher.ratio()
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changes.append((tag, i1, i2, j1, j2, _word_differences(a2[i1:i2], b2[j1:j2])))
    total = sum(c[5] for c in changes)
    if total == 0:
        return Comparison(score, "minor", "the texts differ only in word endings (case or tense)")

    tag, i1, i2, j1, j2, count = changes[0]
    if i1 == 0 and j1 == 0 and count > 0:
        rest_a, rest_b = a2[i2:], b2[j2:]
        if len(rest_a) >= 3 and len(rest_b) >= 3 and _ratio(rest_a, rest_b) >= 0.8:
            n = max(i2 - i1, j2 - j1)
            return Comparison(score, "suffix",
                              f"the opening differs ({_n_words(n)}); the rest matches")

    long_changes = [c for c in changes if c[5] >= 4 and not (c[1] == 0 and c[3] == 0)]
    if long_changes:
        tag, i1, i2, j1, j2, count = max(long_changes, key=lambda c: c[5])
        where = "at the end" if i2 == len(a2) and j2 == len(b2) else "in the middle"
        what = {
            "insert": "phrase in the Cantus Index text only",
            "delete": "phrase in this text only",
            "replace": "phrase differs",
        }[tag]
        return Comparison(score, "suffix", f"{count}-word {what}, {where}")

    if total <= 3:
        verb = "differs" if total == 1 else "differ"
        return Comparison(score, "minor", f"{_n_words(total)} {verb}")
    if score >= 0.7:
        return Comparison(score, "suffix",
                          f"{total} words differ in several places; check whether this is a variant")
    return Comparison(score, "new", f"the texts differ throughout ({total} words)")


def _drop_leading_alleluia(ws: list[str]) -> list[str]:
    i = 0
    while i < len(ws) and ws[i] == ALLELUIA:
        i += 1
    return ws[i:]


def _same_stem(x: str, y: str) -> bool:
    common = len(os.path.commonprefix([x, y]))
    return common >= 4 or (common >= 2 and common >= min(len(x), len(y)) - 1)


def _word_differences(xs: list[str], ys: list[str]) -> int:
    """Words that differ, not counting changes of ending (case or tense)."""
    paired = sum(1 for x, y in zip(xs, ys) if not _same_stem(x, y))
    return paired + abs(len(xs) - len(ys))


def _ratio(a: list[str], b: list[str]) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _n_words(n: int) -> str:
    return f"{n} word" if n == 1 else f"{n} words"


# --------------------------------------------------------------------------
# Search queries
#
# Cantus Index text search matches from the start of the text (SQL LIKE) and
# passes the wildcards % and _ through. Its texts use classical spelling
# (haec, caeli, pretiosa, ejus) where the Nidaros texts often do not (hec,
# celi, preciosa, eius), so a spelling-tolerant pattern is tried when the
# literal words find nothing.

def query_words(text: str | None) -> list[str]:
    """Words of the line's text as spelled, for searching."""
    return plain(text).replace("*", " ").split()


def literal_query(ws: list[str], n: int) -> str:
    return " ".join(ws[:n])


def tolerant_query(ws: list[str], n: int) -> str:
    pattern = " ".join(_tolerant_word(w) for w in ws[:n])
    return re.sub(r"%+", "%", pattern)


def _tolerant_word(word: str) -> str:
    out = []
    for i, ch in enumerate(word):
        after = word[i + 1:i + 3]
        if ch in "ijyuv":
            out.append("_")                      # i/j/y and u/v
        elif ch in "ct" and len(after) == 2 and after[0] == "i" and after[1] in "aeiou":
            out.append("_")                      # preciosa / pretiosa
        elif ch == "e":
            out.append("%e")                     # hec / haec, celi / caeli / coeli
        else:
            out.append(ch)
    return "".join(out)


def distinctive_word(ws: list[str]) -> str | None:
    """The longest word, for a last-resort search anywhere in the text."""
    candidates = [w for w in ws if len(w) >= 5]
    return max(candidates, key=len) if candidates else None
