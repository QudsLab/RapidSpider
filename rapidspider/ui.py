"""ui.py - the web interface (Flask, runs in a thread next to the engine).

    http://127.0.0.1:8800      Dashboard (monitor, pause/resume, threads) | every table as a sortable, filterable list
                               (Sites, Facts, Laya questions, Pages, Profiles, Steps, Logos, Log) | click a row for its detail

It only READS the sqlite files (own connections, WAL) and drives the engine through its small control surface
(paused / threads / quit).
"""
import glob
import json
import os
import re
import sqlite3
import threading
import time

from flask import Flask, abort, jsonify, request, send_from_directory


class Reader:
    """read-only access to track.db and the rolling shard files of the output folder."""

    def __init__(self, root):
        self.root = root
        self._cols = {}

    def con(self, path):
        c = sqlite3.connect(path, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def track(self):
        return self.con(os.path.join(self.root, "track.db"))

    def shards(self, name):
        if name == "track":
            return [(0, os.path.join(self.root, "track.db"))]
        out = []
        for f in glob.glob(os.path.join(self.root, f"{name}_*.db")):
            m = re.search(r"_(\d+)\.db$", f)
            if m:
                out.append((int(m.group(1)), f))
        return sorted(out, reverse=True)           # newest first

    def columns(self, name):
        """[(name, type)] from the schema the store creates - works before any row exists."""
        if name not in self._cols:
            from .store import SCHEMAS, TRACK_SCHEMA
            c = sqlite3.connect(":memory:")
            c.executescript(TRACK_SCHEMA if name == "track" else SCHEMAS[name])
            tbl = "track" if name == "track" else name
            self._cols[name] = [(r[1], (r[2] or "TEXT").upper()) for r in c.execute(f"PRAGMA table_info({tbl})")]
            c.close()
        return self._cols[name]

    def count(self, name, where="1", params=()):
        total = 0
        for _n, path in self.shards(name):
            c = self.con(path)
            try:
                total += c.execute(f"SELECT COUNT(*) FROM {self._t(name)} WHERE {where}", params).fetchone()[0]
            except sqlite3.Error:
                pass
            finally:
                c.close()
        return total

    @staticmethod
    def _t(name):
        return "track" if name == "track" else name

    def page(self, name, where="1", params=(), order="rowid DESC", limit=50, offset=0, cols="*"):
        """unsorted-by-user listing: newest shard first, `order` inside each shard."""
        total, rows, skip, need = 0, [], offset, limit
        for n, path in self.shards(name):
            c = self.con(path)
            try:
                cnt = c.execute(f"SELECT COUNT(*) FROM {self._t(name)} WHERE {where}", params).fetchone()[0]
                total += cnt
                if need <= 0 or skip >= cnt:
                    if need > 0:
                        skip -= cnt
                    continue
                got = c.execute(f"SELECT rowid AS rid, {cols} FROM {self._t(name)} WHERE {where} ORDER BY {order} "
                                f"LIMIT ? OFFSET ?", (*params, need, skip)).fetchall()
                for r in got:
                    d = dict(r)
                    d["rid"] = f"{n}:{d['rid']}"
                    rows.append(d)
                need -= len(got)
                skip = 0
            except sqlite3.Error:
                continue
            finally:
                c.close()
        return rows, total

    def page_sorted(self, name, where, params, col, desc, limit, offset, cols="*"):
        """user-sorted listing across all shards: each shard returns its best offset+limit rows, merged here."""
        want, merged, total = offset + limit, [], 0
        d = "DESC" if desc else "ASC"
        for n, path in self.shards(name):
            c = self.con(path)
            try:
                total += c.execute(f"SELECT COUNT(*) FROM {self._t(name)} WHERE {where}", params).fetchone()[0]
                for r in c.execute(f"SELECT rowid AS rid, {cols} FROM {self._t(name)} WHERE {where} "
                                   f"ORDER BY {col} {d}, rowid {d} LIMIT ?", (*params, want)):
                    x = dict(r)
                    x["rid"] = f"{n}:{x['rid']}"
                    merged.append(x)
            except sqlite3.Error:
                continue
            finally:
                c.close()

        def key(x):
            v = x.get(col)
            return (0, 0, "") if v is None else ((1, v, "") if isinstance(v, (int, float)) else (2, 0, str(v).lower()))
        merged.sort(key=key, reverse=desc)
        return merged[offset:offset + limit], total

    def all(self, name, where, params=(), order="rowid"):
        rows, _ = self.page(name, where, params, order, limit=100000)
        return rows

    def path_of(self, name, rid):
        n, row = str(rid).split(":")
        if name == "track":
            return os.path.join(self.root, "track.db"), int(row)
        return os.path.join(self.root, f"{name}_{int(n):04d}.db"), int(row)

    def row(self, name, rid):
        path, row = self.path_of(name, rid)
        if not os.path.isfile(path):
            return None
        c = self.con(path)
        try:
            r = c.execute(f"SELECT rowid AS rid, * FROM {self._t(name)} WHERE rowid=?", (row,)).fetchone()
        finally:
            c.close()
        if not r:
            return None
        d = dict(r)
        d["rid"] = str(rid)
        return d

    def distinct(self, name, col, limit=60):
        vals = set()
        for _n, path in self.shards(name):
            c = self.con(path)
            try:
                vals.update(r[0] for r in c.execute(
                    f"SELECT DISTINCT {col} FROM {self._t(name)} WHERE {col} IS NOT NULL LIMIT 200"))
            except sqlite3.Error:
                pass
            finally:
                c.close()
        return sorted(vals, key=lambda v: (not isinstance(v, (int, float)), v if isinstance(v, (int, float)) else str(v)))[:limit]


# what the data browser shows for each table: list = columns in the list, search = columns the search box scans,
# filters = columns with a drop-down, flags = named quick filters (SQL), default = newest-first order
TABLES = {
    "track": {"label": "Sites", "list": ["id", "name", "final_url", "status", "reason", "done", "processed", "attempts", "rung", "pages", "words", "finished_at", "elapsed_ms"],
              "search": ["name", "url", "final_url", "id", "reason"], "filters": ["status", "class", "rung", "done", "processed"], "flags": {},
              "default": "COALESCE(finished_at, started_at) DESC"},
    "sites": {"label": "Site text", "list": ["id", "name", "final_url", "status", "found_emails", "found_phones", "created_at"],
              "search": ["name", "final_url", "text", "found_emails", "found_phones"], "filters": ["status"], "flags": {}},
    "pages": {"label": "Pages", "list": ["site_id", "url", "http_status", "title", "words", "fetched_at"],
              "search": ["url", "title"], "filters": ["http_status"], "flags": {}},
    "facts": {"label": "Facts", "list": ["site_id", "category", "text", "value", "confidence", "votes", "zone", "loc", "review", "source"],
              "search": ["text", "value"], "filters": ["category", "review", "source"],
              "flags": {}},
    "profiles": {"label": "Profiles", "list": ["site_id", "name", "fact_count", "categories", "ready", "created_at"],
                 "search": ["name", "profile"], "filters": ["ready"], "flags": {}},
    "laya_calls": {"label": "Laya questions", "list": ["ts", "site_id", "task", "text", "label", "conf", "near_label", "near_sim", "ms"],
                   "search": ["text", "label"], "filters": ["task", "label"], "hidden": ["shots"],
                   "flags": {"disagrees with a solved example": "near_sim>=0.8 AND near_label IS NOT NULL AND near_label!=label",
                             "low confidence": "conf<0.7", "errors": "label='ERROR'"}},
    "incidents": {"label": "Steps", "list": ["site_id", "at", "step", "outcome", "detail"],
                  "search": ["detail", "outcome", "step"], "filters": ["step", "outcome"], "flags": {}},
    "site_logs": {"label": "Site logs", "list": ["site_id", "attempt", "created_at", "log"],
                  "search": ["log"], "filters": [], "flags": {}},
    "logos": {"label": "Logos", "list": ["site_id", "url", "source", "score", "laya_score", "is_logo", "placement", "error"],
              "search": ["url", "alt"], "filters": ["source", "is_logo"], "flags": {}},
    "run_log": {"label": "Log", "list": ["ts", "level", "msg"], "search": ["msg"], "filters": ["level"], "flags": {}},
}


def build(cfg, store, engine, laya, facts, log):
    root = cfg.output.root
    rd = Reader(root)
    app = Flask(__name__)
    app.json.sort_keys = False
    here = os.path.dirname(__file__)
    page_html = open(os.path.join(here, "ui_page.html"), encoding="utf-8").read()
    assets = os.path.normpath(os.path.join(here, "..", "assets"))
    cats = {c.key: {"label": c.label, "bullet": c.bullet} for c in (facts.cats if facts else [])}
    counts_cache = {"t": 0, "v": {}}

    @app.get("/")
    def index():
        return page_html

    @app.get("/assets/<path:fn>")
    def asset(fn):
        if fn == "mark-inline.svg":       # same drawing, but recolourable from the page css (classes lm-d / lm-b)
            svg = open(os.path.join(assets, "logo-mark.svg"), encoding="utf-8").read()
            svg = re.sub(r"<style>.*?</style>", "", svg, flags=re.S).replace('class="d"', 'class="lm-d"').replace('class="b"', 'class="lm-b"')
            return app.response_class(svg, mimetype="image/svg+xml")
        return send_from_directory(assets, fn)

    @app.get("/api/meta")
    def meta():
        tables = []
        for name, sp in TABLES.items():
            tables.append({"name": name, "label": sp["label"], "list": sp["list"], "filters": sp["filters"],
                           "flags": list(sp["flags"]), "columns": [{"name": c, "type": t} for c, t in rd.columns(name)]})
        return jsonify({"name": cfg.name, "root": root, "tables": tables, "categories": cats})

    # ------------------------------------------------------------ monitor + control
    @app.get("/api/state")
    def state():
        now = time.time()
        c = rd.track()
        counts = dict(c.execute("SELECT status, COUNT(*) FROM track GROUP BY status").fetchall())
        c.close()
        fin = list(engine.finished)
        recent = [t for t in fin if now - t < 300]
        rate = len(recent) / max(1.0, min(300.0, now - recent[0])) * 60 if len(recent) > 1 else 0.0
        remaining = (engine.q.qsize() if engine.q else 0) + len(engine.active)
        if now - counts_cache["t"] > 5:
            counts_cache.update(t=now, v={n: rd.count(n) for n in TABLES})
        through = [sum(1 for t in fin if now - (i + 1) * 60 <= t < now - i * 60) for i in range(19, -1, -1)]
        return jsonify({
            "mode": engine.mode, "threads": engine.threads, "max_threads": engine.MAX_WORKERS,
            "total": engine.total, "done": engine.done, "queued": engine.q.qsize() if engine.q else 0,
            "counts": counts, "tables": counts_cache["v"], "throughput": through,
            "active": [{"id": x.row.id, "name": x.row.name, "stage": x.stage, "sec": int(now - x.t0)}
                       for x in list(engine.active.values())],
            "rate_per_min": round(rate, 1), "eta_min": round(remaining / rate) if rate else None,
            "laya": laya.stats()})

    @app.post("/api/control")
    def control():
        d = request.get_json(silent=True) or {}
        a = d.get("action")
        if a == "pause":
            engine.paused = True
        elif a == "resume":
            engine.paused = False
        elif a == "stop":
            engine.quit = True
        elif a == "threads":
            engine.threads = max(1, min(engine.MAX_WORKERS, int(d.get("value") or 1)))
        else:
            return jsonify({"error": "unknown action"}), 400
        return jsonify({"ok": True, "mode": engine.mode, "threads": engine.threads})

    # ------------------------------------------------------------ the data browser (every table, same rules)
    def table_or_404(name):
        if name not in TABLES:
            abort(404)
        return TABLES[name]

    @app.get("/api/table/<name>")
    def table(name):
        sp, a = table_or_404(name), request.args
        cols = {c for c, _t in rd.columns(name)}
        where, params = ["1"], []
        for k, v in a.items():
            if k.startswith("f_") and k[2:] in cols and v != "":
                where.append(f"{k[2:]}=?")
                params.append(v)
        if a.get("q"):
            where.append("(" + " OR ".join(f"{c} LIKE ?" for c in sp["search"]) + ")")
            params += [f"%{a['q']}%"] * len(sp["search"])
        if a.get("flag") in sp["flags"]:
            where.append(sp["flags"][a["flag"]])
        limit, offset = max(1, min(int(a.get("limit", 50)), 200)), max(0, int(a.get("offset", 0)))
        select = ", ".join(sp["list"] + sp.get("hidden", []))
        w = " AND ".join(where)
        if a.get("sort") in cols:
            rows, total = rd.page_sorted(name, w, params, a["sort"], a.get("dir") == "desc", limit, offset, select)
        else:
            rows, total = rd.page(name, w, params, sp.get("default", "rowid DESC"), limit, offset, select)
        return jsonify({"rows": rows, "total": total})

    @app.get("/api/row/<name>/<rid>")
    def row(name, rid):
        table_or_404(name)
        r = rd.row(name, rid)
        if r is None:
            abort(404)
        return jsonify(r)

    @app.get("/api/distinct/<name>/<col>")
    def distinct(name, col):
        table_or_404(name)
        if col not in {c for c, _t in rd.columns(name)}:
            abort(404)
        return jsonify(rd.distinct(name, col))

    @app.get("/api/site/<sid>")
    def site(sid):
        c = rd.track()
        t = c.execute("SELECT * FROM track WHERE id=?", (sid,)).fetchone()
        c.close()
        if not t:
            abort(404)
        sites_rows = rd.all("sites", "id=?", (sid,), "attempt DESC")
        prof = rd.all("profiles", "site_id=?", (sid,), "attempt DESC")
        return jsonify({
            "track": dict(t), "site": sites_rows[0] if sites_rows else None,
            "pages": rd.all("pages", "site_id=?", (sid,)), "incidents": rd.all("incidents", "site_id=?", (sid,)),
            "logos": rd.all("logos", "site_id=?", (sid,)),
            "facts": rd.all("facts", "site_id=?", (sid,), "score DESC"),
            "profile": json.loads(prof[0]["profile"]) if prof else None})

    @app.get("/logos/<sid>/<path:fn>")
    def logo_file(sid, fn):
        if not re.fullmatch(r"[\w.\-]+", sid):
            abort(404)
        return send_from_directory(os.path.join(root, "logos", sid), fn)

    return app


def start(cfg, store, engine, laya, facts, log):
    from werkzeug.serving import make_server
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = build(cfg, store, engine, laya, facts, log)
    try:
        srv = make_server(cfg.ui.host, int(cfg.ui.port), app, threaded=True)
    except OSError as e:
        log.error("web ui could not start on %s:%s (%s) - the run continues without it", cfg.ui.host, cfg.ui.port, e)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("web ui: http://%s:%s", cfg.ui.host, cfg.ui.port)
    return srv
