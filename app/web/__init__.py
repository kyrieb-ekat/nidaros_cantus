"""Review screen: python -m app serve

Runs on this computer only (127.0.0.1). Every form carries a token created
when the server starts, so other web pages cannot send changes to it. All
changes go through the same functions as the command line and are logged.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   url_for)

from .. import db
from ..cantus import CantusError, CantusIndex
from ..feasts import STATUSES, ci_feasts, feast_headings, find_feast, map_feast, search_feasts
from ..fetch import fetch_line
from ..positions import refresh_position_flags
from .review import Filters, filter_choices, line_view, progress, queue

EDITABLE_HERE = ("fulltext_standardized", "extra_note", "position_override", "office",
                 "indexing_notes")
DECISIONS = ("Confirm", "Reject", "Fixed", "Variant", "New CID")


def create_app(db_path: str | Path, siglum: str | None = None, transport=None,
               min_interval: float = 1.0) -> Flask:
    app = Flask(__name__)
    token = secrets.token_urlsafe(24)
    app.config.update(SECRET_KEY=token, FORM_TOKEN=token, DB_PATH=str(db_path),
                      SIGLUM=siglum, TRANSPORT=transport, MIN_INTERVAL=min_interval)

    @app.before_request
    def open_db():
        g.conn = db.connect(app.config["DB_PATH"])
        g.source = _source(g.conn, app.config["SIGLUM"])
        if request.method == "POST" and request.form.get("token") != app.config["FORM_TOKEN"]:
            abort(403)

    @app.teardown_request
    def close_db(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.errorhandler(403)
    def refused(_exc):
        back = request.referrer or url_for("lines")
        return (f"<!doctype html><title>Not saved</title><body style=\"font: 15px/1.5 system-ui; "
                f"margin: 3rem auto; max-width: 36rem\"><h1 style=\"font-weight: normal\">Not saved"
                f"</h1><p>This page was opened before the review screen was last started, so the "
                f"change was refused. Go back, reload the page, and try again.</p>"
                f"<p><a href=\"{back}\">Back to the page</a></p></body>", 403)

    @app.context_processor
    def common():
        return {"token": app.config["FORM_TOKEN"], "source": g.get("source")}

    # -- queue and lines ----------------------------------------------------

    @app.get("/")
    def index():
        return redirect(url_for("lines"))

    @app.get("/lines")
    def lines():
        filters = Filters.from_args(request.args)
        rows = queue(g.conn, g.source["id"], filters)
        if rows:
            return redirect(url_for("line", line_id=rows[0]["id"], **filters.query()))
        return render_template("line.html", view=None, rows=rows, filters=filters,
                               choices=filter_choices(g.conn, g.source["id"]),
                               progress=progress(g.conn, g.source["id"]), prev_id=None, next_id=None)

    @app.get("/line/<int:line_id>")
    def line(line_id: int):
        view = line_view(g.conn, line_id)
        if view is None or view.line["source_id"] != g.source["id"]:
            abort(404)
        filters = Filters.from_args(request.args)
        rows = queue(g.conn, g.source["id"], filters)
        prev_id, next_id = _neighbours(rows, line_id)
        return render_template("line.html", view=view, rows=rows, filters=filters,
                               choices=filter_choices(g.conn, g.source["id"]),
                               progress=progress(g.conn, g.source["id"]),
                               prev_id=prev_id, next_id=next_id)

    @app.post("/line/<int:line_id>/decide")
    def decide(line_id: int):
        status = request.form.get("decision")
        if status not in DECISIONS:
            abort(400)
        try:
            db.record_decision(g.conn, line_id, status, request.form.get("cid"),
                               request.form.get("note") or None,
                               parent_cid=request.form.get("parent"),
                               reason=request.form.get("reason") or None)
            flash(f"Recorded: {status} {request.form.get('cid') or ''}".strip(), "ok")
        except db.DatabaseError as exc:
            flash(str(exc), "error")
            return _back(line_id)
        if request.form.get("advance"):
            filters = Filters.from_args(request.form)
            _, next_id = _neighbours(queue(g.conn, g.source["id"], filters), line_id)
            if next_id:
                return redirect(url_for("line", line_id=next_id, **filters.query()))
        return _back(line_id)

    @app.post("/line/<int:line_id>/edit")
    def edit(line_id: int):
        field = request.form.get("field")
        if field not in EDITABLE_HERE:
            abort(400)
        value = (request.form.get("value") or "").strip() or None
        try:
            changed = db.update_line(g.conn, line_id, field, value,
                                     request.form.get("note") or "review screen")
            if field in ("position_override", "office"):
                refresh_position_flags(g.conn, g.source["id"])
            flash(f"Saved {field.replace('_', ' ')}." if changed else "No change.", "ok")
        except db.DatabaseError as exc:
            flash(str(exc), "error")
        return _back(line_id)

    @app.post("/line/<int:line_id>/flag")
    def flag(line_id: int):
        try:
            db.set_flag(g.conn, line_id, request.form.get("code", ""), request.form.get("action", ""),
                        request.form.get("note") or None)
        except db.DatabaseError as exc:
            flash(str(exc), "error")
        return _back(line_id)

    @app.post("/line/<int:line_id>/fetch")
    def fetch(line_id: int):
        line_row = g.conn.execute("SELECT * FROM line_export WHERE id = ?", (line_id,)).fetchone()
        if line_row is None:
            abort(404)
        client = CantusIndex(g.conn, transport=app.config["TRANSPORT"],
                             min_interval=app.config["MIN_INTERVAL"],
                             refresh=bool(request.form.get("refresh")))
        result = fetch_line(g.conn, client, line_row)
        if result.error:
            flash(f"Lookup failed: {result.error}", "error")
        else:
            best = result.best
            flash(f"Looked up in Cantus Index ({client.requests} requests): "
                  + (f"best {best.cid}, {best.comparison.text_class}" if best else "no results"), "ok")
        return _back(line_id)

    # -- feasts -----------------------------------------------------------------

    @app.get("/feasts")
    def feasts():
        term = (request.args.get("q") or "").strip()
        heading = request.args.get("heading")
        hits, error = [], None
        if term:
            try:
                hits = search_feasts(ci_feasts(_client(app, offline=False)), term)[:40]
            except CantusError as exc:
                error = str(exc)
        return render_template("feasts.html", headings=feast_headings(g.conn, g.source["id"]),
                               term=term, heading=heading, hits=hits, error=error,
                               statuses=STATUSES)

    @app.post("/feasts/map")
    def feasts_map():
        heading = request.form.get("heading", "")
        status = request.form.get("status", "exact")
        try:
            feast = None
            if status != "unmapped":
                feast = find_feast(ci_feasts(_client(app, offline=False)), request.form.get("ci", ""))
            map_feast(g.conn, g.source["id"], heading, status, feast, request.form.get("note") or None)
            flash(f"{heading} → {feast.name if feast else 'no Cantus feast'} ({status})", "ok")
        except (db.DatabaseError, CantusError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("feasts", heading=heading))
        following = [h["feast"] for h in feast_headings(g.conn, g.source["id"]) if not h["status"]]
        return redirect(url_for("feasts", heading=following[0]) if following else url_for("feasts"))

    # -- helpers ------------------------------------------------------------------

    def _back(line_id: int):
        filters = Filters.from_args(request.form)
        return redirect(url_for("line", line_id=line_id, **filters.query()))

    return app


def _client(app: Flask, offline: bool) -> CantusIndex:
    return CantusIndex(g.conn, transport=app.config["TRANSPORT"],
                       min_interval=app.config["MIN_INTERVAL"], offline=offline)


def _source(conn, siglum):
    if siglum:
        row = conn.execute("SELECT * FROM source WHERE siglum = ?", (siglum,)).fetchone()
    else:
        rows = conn.execute("SELECT * FROM source ORDER BY siglum").fetchall()
        row = rows[0] if rows else None
    if row is None:
        abort(500, "No book in the database yet; add one with `python -m app add-source`.")
    return row


def _neighbours(rows, line_id):
    ids = [r["id"] for r in rows]
    if line_id not in ids:
        return None, (ids[0] if ids else None)
    i = ids.index(line_id)
    return (ids[i - 1] if i > 0 else None), (ids[i + 1] if i + 1 < len(ids) else None)
