"""A stand-in for Cantus Index, used instead of the network in tests.

json-text behaves like the real endpoint: SQL LIKE on the full text
(case-insensitive, % and _ as wildcards), prefix matches first, then
"contains" matches if there were fewer than 50, at most 100, alphabetical.
Every response starts with a UTF-8 byte-order mark, as the real ones do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

FIXTURES = Path(__file__).parent / "fixtures"
BOM = "\ufeff"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _like(pattern: str) -> re.Pattern:
    out = []
    for ch in pattern:
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


class FakeCantusIndex:
    def __init__(self, corpus: list[dict] | None = None, chants: dict | None = None,
                 feasts: list | None = None, failures: dict | None = None):
        # corpus: [{"cid", "fulltext", "genre", optional "feast", "related"}]
        self.corpus = corpus or []
        self.chants = chants or {}          # cid -> full json-cid response
        self.feasts = feasts
        self.failures = dict(failures or {})  # url -> list of statuses to return first
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        pending = self.failures.get(url)
        if pending:
            return pending.pop(0), "text/html", b"error"
        path = url.split("cantusindex.org/", 1)[1]
        if path.startswith("json-text/"):
            return self._ok(self._text(unquote(path[len("json-text/"):])))
        if path.startswith("json-cid/"):
            cid = unquote(path[len("json-cid/"):])
            data = self.chants.get(cid) or self._chant(cid)
            if data is None:
                return 404, "text/html", b"not found"
            return self._ok(data)
        if path == "json-feasts" and self.feasts is not None:
            return self._ok(self.feasts)
        return 404, "text/html", b"not found"

    @staticmethod
    def _ok(data):
        return 200, "application/json", (BOM + json.dumps(data)).encode("utf-8")

    def _text(self, query: str) -> list[dict]:
        prefix = _like(query + "%")
        contains = _like("%" + query + "%")
        rows = sorted((c for c in self.corpus if prefix.fullmatch(c["fulltext"])),
                      key=lambda c: c["fulltext"])[:100]
        if len(rows) < 50:
            more = sorted((c for c in self.corpus if contains.fullmatch(c["fulltext"])
                           and c not in rows), key=lambda c: c["fulltext"])
            rows += more[:100]
        return [{"cid": c["cid"], "fulltext": c["fulltext"], "genre": c["genre"]} for c in rows]

    def _chant(self, cid: str):
        for c in self.corpus:
            if c["cid"] == cid:
                return {
                    "info": {"field_full_text": c["fulltext"], "field_genre": c["genre"],
                             "field_feast": c.get("feast"),
                             "field_related_chant_id": c.get("related"),
                             "field_cao": None, "field_cao_concordances": None},
                    "databases": {"CD": {"count": 1}},
                    "chants": [{"db": "CD", "feast": c.get("feast"), "office": "M",
                                "genre": c["genre"], "fulltext": c["fulltext"]}],
                }
        return None
