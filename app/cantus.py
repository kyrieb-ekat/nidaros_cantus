"""Cantus Index client: cached, rate-limited, and tolerant of its quirks.

Endpoints used (documented at https://github.com/dact-chant/cantus-index):

  json-text/{text}   chants whose full text starts with (then contains) text;
                     at most 100, alphabetical; fields cid, fulltext, genre
  json-cid/{cid}     the chant record ("info"), per-database counts
                     ("databases") and every concordance in the Cantus Index
                     network ("chants"), Cantus Database included
  json-feasts        all Cantus Index feasts, with alternative names

Responses begin with a UTF-8 byte-order mark, which is removed. Every
successful response is stored in api_cache and reused; the network is only
used for URLs not yet cached, or when refresh is requested.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "https://cantusindex.org"
RESULT_CAP = 100          # json-text returns at most this many chants
RETRY_DELAYS = (5, 15)    # seconds to wait before each retry of a failed request

Transport = Callable[[str], "tuple[int, str | None, bytes]"]


class CantusError(Exception):
    """A request to Cantus Index failed."""


def user_agent() -> str:
    agent = "nidaros-cantus/2 (chant indexing research tool)"
    contact = os.environ.get("NIDAROS_CONTACT")
    return f"{agent}; contact: {contact}" if contact else agent


def urllib_transport(url: str, timeout: float = 30) -> tuple[int, str | None, bytes]:
    request = Request(url, headers={"User-Agent": user_agent(), "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type") if exc.headers else None, exc.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise CantusError(f"could not reach {url}: {exc}") from exc


class CantusIndex:
    def __init__(self, conn: sqlite3.Connection, *, transport: Transport | None = None,
                 min_interval: float = 1.0, refresh: bool = False, offline: bool = False,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.conn = conn
        self.transport = transport or urllib_transport
        self.min_interval = min_interval
        self.refresh = refresh
        self.offline = offline
        self.sleep = sleep
        self.clock = clock
        self.requests = 0                 # network requests made
        self._last_request: float | None = None
        self._refreshed: set[str] = set()  # with refresh, fetch each URL once per run

    # -- endpoints ------------------------------------------------------------

    def text_search(self, query: str) -> list[dict]:
        return self.get("json-text/" + quote(query, safe="")) or []

    def chant(self, cid: str) -> dict | None:
        return self.get("json-cid/" + quote(cid, safe=""))

    def feasts(self) -> list[dict]:
        return self.get("json-feasts") or []

    def is_cached(self, path: str) -> bool:
        return self._cached(f"{BASE_URL}/{path}") is not None

    # -- caching and transport ----------------------------------------------

    def get(self, path: str) -> Any:
        url = f"{BASE_URL}/{path}"
        use_cache = not self.refresh or url in self._refreshed
        if use_cache:
            row = self._cached(url)
            if row is not None:
                return _decode(row["body"], url)
        if self.offline:
            raise CantusError(f"not in the cache, and working offline: {url}")
        status, content_type, body = self._download(url)
        text = body.decode("utf-8-sig", errors="replace")
        data = _decode(text, url) if status == 200 else None
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_cache (url, http_status, content_type, body) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET http_status = excluded.http_status, "
                "content_type = excluded.content_type, body = excluded.body, "
                "fetched_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
                (url, status, content_type, text if status == 200 else None),
            )
        self._refreshed.add(url)
        return data

    def _cached(self, url: str):
        return self.conn.execute(
            "SELECT http_status, body FROM api_cache WHERE url = ?", (url,)).fetchone()

    def _download(self, url: str) -> tuple[int, str | None, bytes]:
        """Fetch with rate limiting. 200 and 404 are answers; other codes are retried."""
        last_problem = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                self.sleep(RETRY_DELAYS[attempt - 1])
            self._wait_turn()
            try:
                status, content_type, body = self.transport(url)
            except CantusError as exc:
                last_problem = str(exc)
                continue
            if status in (200, 404):
                return status, content_type, body
            last_problem = f"HTTP {status} from {url}"
        raise CantusError(f"{last_problem} (gave up after {len(RETRY_DELAYS) + 1} attempts)")

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()
        self.requests += 1


def _decode(text: str | None, url: str) -> Any:
    if text is None or not text.strip():
        return None
    try:
        return json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise CantusError(f"not valid JSON from {url}: {exc}") from exc


# --------------------------------------------------------------------------
# Concordance evidence, read from a json-cid response

@dataclass
class Evidence:
    cid: str
    ci_text: str | None
    ci_genre: str | None
    ci_feast: str | None
    related_cid: str | None
    cao: str | None
    cao_sigla: str | None
    reported: dict[str, int] = field(default_factory=dict)   # per database, as Cantus Index counts
    records: dict[str, int] = field(default_factory=dict)    # per database, records returned
    same_feast: dict[str, int] = field(default_factory=dict)
    same_feast_office: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """False when Cantus Index reports more concordances than it returned."""
        return sum(self.records.values()) >= sum(self.reported.values())

    @property
    def total(self) -> int:
        return sum(self.records.values())


def evidence(cid: str, data: dict | None, feast_names: set[str] | None = None,
             office: str | None = None) -> Evidence | None:
    """Summarise a json-cid response for one candidate.

    feast_names: the Cantus Index feast name(s) mapped to the line's feast
    (case-insensitive); office: the line's office code.
    """
    if not data:
        return None
    info = data.get("info") or {}
    wanted = {f.casefold() for f in feast_names} if feast_names else set()
    ev = Evidence(
        cid=cid,
        ci_text=info.get("field_full_text"),
        ci_genre=info.get("field_genre"),
        ci_feast=info.get("field_feast"),
        related_cid=info.get("field_related_chant_id"),
        cao=info.get("field_cao"),
        cao_sigla=info.get("field_cao_concordances"),
    )
    for code, db in (data.get("databases") or {}).items():
        try:
            ev.reported[code] = int(db.get("count") or 0)
        except (TypeError, ValueError):
            ev.reported[code] = 0
    for chant in data.get("chants") or []:
        code = chant.get("db") or "?"
        ev.records[code] = ev.records.get(code, 0) + 1
        if wanted and (chant.get("feast") or "").casefold() in wanted:
            ev.same_feast[code] = ev.same_feast.get(code, 0) + 1
            if office and chant.get("office") == office:
                ev.same_feast_office[code] = ev.same_feast_office.get(code, 0) + 1
    return ev
