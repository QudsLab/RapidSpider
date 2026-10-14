"""store.py - output layout: ONE DIRECTORY per config, one db file per table, every file < output.max_db_mb.

    <output.dir>/
        track.db                 one row per site: name, url, status, reason, done(0-3), attempts ...  (tiny)
        sites_0001.db ...        site text + contacts          (rolls to the next file before 50 MB)
        pages_0001.db ...        one row per fetched page
        logos_0001.db ...        logo candidates (+ files in logos/<id>/)
        incidents_0001.db ...    every ladder step / laya review
        run_log_0001.db ...      run log
        logos/<id>/image_NN.*    saved logo files
        logs/run_YYYY-MM-DD.log

done codes in track.db:  0 queued/running | 1 finished with data (ok, third_party)
                         2 finished, nothing to take (dead, blocked, no_content) | 3 failed, will retry (error, timeout, laya_down)
"""
import glob
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

TRACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS track (
    id TEXT PRIMARY KEY, name TEXT, url TEXT, final_url TEXT,
    status TEXT, class TEXT, reason TEXT, done INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0, rung TEXT, pages INTEGER DEFAULT 0, words INTEGER DEFAULT 0,
    tor_used INTEGER DEFAULT 0, sites_part INTEGER, started_at TEXT, finished_at TEXT,
    elapsed_ms INTEGER, error TEXT,
    processed INTEGER DEFAULT 1            -- 0 = imported as-is, still to be cleaned/judged by laya later
);
CREATE INDEX IF NOT EXISTS idx_track_status ON track(status);
CREATE INDEX IF NOT EXISTS idx_track_done ON track(done);
"""
SCHEMAS = {
    "sites": """CREATE TABLE IF NOT EXISTS sites (
        id TEXT, attempt INTEGER, name TEXT, input_url TEXT, input_phone TEXT, input_address TEXT, input_email TEXT,
        extra TEXT, final_url TEXT, status TEXT, text TEXT, found_emails TEXT, found_phones TEXT, summary TEXT,
        created_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_sites_id ON sites(id);""",
    "pages": """CREATE TABLE IF NOT EXISTS pages (
        site_id TEXT, attempt INTEGER, url TEXT, http_status INTEGER, title TEXT, words INTEGER, text TEXT,
        fetched_at TEXT, raw TEXT);
        CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id);""",
    "logos": """CREATE TABLE IF NOT EXISTS logos (
        site_id TEXT, attempt INTEGER, url TEXT, source TEXT, score INTEGER, laya_score REAL, is_logo INTEGER,
        alt TEXT, placement TEXT, local_path TEXT, size INTEGER, content_type TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS idx_logos_site ON logos(site_id);""",
    "incidents": """CREATE TABLE IF NOT EXISTS incidents (
        site_id TEXT, attempt INTEGER, at TEXT, step TEXT, outcome TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS idx_inc_site ON incidents(site_id);""",
    "site_logs": """CREATE TABLE IF NOT EXISTS site_logs (
        site_id TEXT, attempt INTEGER, created_at TEXT, log TEXT);
        CREATE INDEX IF NOT EXISTS idx_slog_site ON site_logs(site_id);""",
    "run_log": """CREATE TABLE IF NOT EXISTS run_log (ts TEXT, level TEXT, msg TEXT);""",
    "facts": """CREATE TABLE IF NOT EXISTS facts (
        site_id TEXT, attempt INTEGER, category TEXT, text TEXT, value TEXT, confidence REAL, source TEXT, url TEXT,
        review TEXT, review_category TEXT, votes TEXT, score REAL, zone TEXT, loc TEXT);
        CREATE INDEX IF NOT EXISTS idx_facts_site ON facts(site_id);
        CREATE INDEX IF NOT EXISTS idx_facts_cat ON facts(category);""",
    "profiles": """CREATE TABLE IF NOT EXISTS profiles (
        site_id TEXT, attempt INTEGER, name TEXT, profile TEXT, fact_count INTEGER, categories INTEGER, ready INTEGER,
        created_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_profiles_site ON profiles(site_id);""",
    "laya_calls": """CREATE TABLE IF NOT EXISTS laya_calls (
        ts TEXT, site_id TEXT, task TEXT, qkey TEXT, text TEXT, shots TEXT, near_sim REAL, near_label TEXT,
        label TEXT, conf REAL, answer TEXT, prompt TEXT, ms INTEGER, batch INTEGER, learned TEXT);
        CREATE INDEX IF NOT EXISTS idx_lc_site ON laya_calls(site_id);
        CREATE INDEX IF NOT EXISTS idx_lc_task ON laya_calls(task);""",
}
DONE_OK = {"ok", "third_party"}
DONE_NEG = {"dead", "blocked", "no_content"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def done_code(status):
    if status in DONE_OK:
        return 1
    if status in DONE_NEG:
        return 2
    if status == "pending":
        return 0
    return 3


class Shards:
    """a table that lives in name_0001.db, name_0002.db ... each kept under the size limit."""

    def __init__(self, root, name, limit_bytes):
        self.root, self.name, self.limit = root, name, limit_bytes
        found = sorted(glob.glob(os.path.join(root, f"{name}_*.db")))
        self.n = max([int(re.search(r"_(\d+)\.db$", f).group(1)) for f in found] or [1])
        self.con = None
        self.lock = threading.RLock()

    def path(self, n=None):
        return os.path.join(self.root, f"{self.name}_{(n or self.n):04d}.db")

    def size(self, n=None):
        p = self.path(n)
        s = 0
        for f in (p, p + "-wal"):
            if os.path.exists(f):
                s += os.path.getsize(f)
        return s

    def _open(self):
        if self.con:
            self.con.close()
        while self.size() >= self.limit:
            self.n += 1
        self.con = sqlite3.connect(self.path(), isolation_level=None, check_same_thread=False, timeout=15)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(SCHEMAS[self.name])
        self._migrate()

    def _migrate(self):
        """older shard files get the columns that were added later."""
        mem = sqlite3.connect(":memory:")
        mem.executescript(SCHEMAS[self.name])
        have = {r[1] for r in self.con.execute(f"PRAGMA table_info({self.name})")}
        for _cid, col, typ, *_rest in mem.execute(f"PRAGMA table_info({self.name})"):
            if col not in have:
                self.con.execute(f"ALTER TABLE {self.name} ADD COLUMN {col} {typ or 'TEXT'}")
        mem.close()

    def conn(self):
        if self.con is None or self.size() >= self.limit:
            if self.con is not None:
                self.n += 1
            self._open()
        return self.con

    def insert(self, sql, params):
        with self.lock:
            c = self.conn()
            c.execute(sql, params)
            return self.n

    def insert_many(self, sql, rows):
        if not rows:
            return self.n
        with self.lock:
            c = self.conn()
            c.execute("BEGIN")
            c.executemany(sql, rows)
            c.execute("COMMIT")
            return self.n

    def close(self):
        if self.con:
            self.con.close()
            self.con = None


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.output.root
        os.makedirs(self.root, exist_ok=True)
        limit = int(float(cfg.output.max_db_mb) * 1024 * 1024)
        self.track = sqlite3.connect(os.path.join(self.root, "track.db"), isolation_level=None, check_same_thread=False, timeout=15)
        self.track.execute("PRAGMA journal_mode=WAL")
        self.track.executescript(TRACK_SCHEMA)
        if "processed" not in {r[1] for r in self.track.execute("PRAGMA table_info(track)")}:
            self.track.execute("ALTER TABLE track ADD COLUMN processed INTEGER DEFAULT 1")
        self.sh = {n: Shards(self.root, n, limit) for n in SCHEMAS}
        # rows a crashed run left running become retryable
        self.track.execute("UPDATE track SET status='error', done=3, error='previous run never finished' "
                           "WHERE status='pending'")

    @property
    def path(self):
        return self.root

    # ---- resume -------------------------------------------------------------
    def should_run(self, row):
        r = self.track.execute("SELECT status, attempts FROM track WHERE id=?", (row.id,)).fetchone()
        if r is None or not self.cfg.run.resume:
            return True
        status, attempts = r
        if done_code(status) in (1, 2):
            return False
        return status in self.cfg.run.retry_statuses and (attempts or 0) < self.cfg.run.max_attempts

    def begin(self, row, url):
        r = self.track.execute("SELECT attempts FROM track WHERE id=?", (row.id,)).fetchone()
        a = ((r[0] or 0) if r else 0) + 1
        self.track.execute(
            "INSERT INTO track (id,name,url,status,done,attempts,started_at) VALUES (?,?,?,'pending',0,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status='pending', done=0, attempts=?, started_at=?, error=NULL, "
            "name=excluded.name, url=excluded.url",
            (row.id, row.name, row.url, a, now(), a, now()))
        return a

    # ---- the one write per finished attempt -------------------------------------
    def save(self, s, attempt, elapsed_ms, store_text):
        rid, row = s.row.id, s.row
        done = done_code(s.status)
        sites_part = None
        if done in (1, 2):                       # terminal: write the heavy tables once
            text = None
            if store_text:
                text = "\n\n".join(f"## {p.url}\n{t}" for p, t in s.pages)[: self.cfg.crawl.max_site_chars]
            sites_part = self.sh["sites"].insert(
                "INSERT INTO sites VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, attempt, row.name, row.url, row.phone, row.address, row.email,
                 json.dumps(row.extra, ensure_ascii=False, default=str), s.final_url, s.status, text,
                 ", ".join(sorted(s.emails)[:10]), ", ".join(sorted(s.phones)[:10]), s.summary, now()))
            if self.cfg.output.store_pages and s.pages:
                from .parse import words
                keep = self.cfg.output.store_pages_text
                self.sh["pages"].insert_many(
                    "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                    [(rid, attempt, p.url, p.status, p.title, words(t), t if keep else None, now(), getattr(p, "raw", None))
                     for p, t in s.pages])
            if s.logos:
                self.sh["logos"].insert_many(
                    "INSERT INTO logos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(rid, attempt, l["url"], l["source"], l["score"], l["laya_score"], l["is_logo"], l["alt"],
                      l["placement"], l["local_path"], l["size"], l["content_type"], l["error"]) for l in s.logos])
        if done == 1 and getattr(s, "facts", None) is not None:
            if s.facts:
                self.sh["facts"].insert_many(
                    "INSERT INTO facts (site_id,attempt,category,text,value,confidence,source,url,review,votes,score,zone,loc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(rid, attempt, f["category"], f["text"], f.get("value"), f["confidence"], f["source"], f["url"],
                      f.get("review"), f.get("votes"), f.get("score"), f.get("zone"), f.get("loc")) for f in s.facts])
            prof = s.profile or {}
            self.sh["profiles"].insert(
                "INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?)",
                (rid, attempt, row.name, json.dumps(prof.get("profile", {}), ensure_ascii=False),
                 len(s.facts), prof.get("categories", 0), int(prof.get("ready", False)), now()))
        if done in (1, 2):                       # the whole story of this site, kept as one text for later inspection
            log_text = getattr(s, "log_text", None) or "\n".join(f"[{at}] {step}: {out} {det}".rstrip() for at, step, out, det in s.incidents)
            self.sh["site_logs"].insert("INSERT INTO site_logs VALUES (?,?,?,?)", (rid, attempt, now(), log_text))
        if s.incidents:
            self.sh["incidents"].insert_many(
                "INSERT INTO incidents VALUES (?,?,?,?,?,?)",
                [(rid, attempt, at, step, out, det) for at, step, out, det in s.incidents])
        from .parse import words as _w
        self.track.execute(
            "UPDATE track SET final_url=?, status=?, class=?, reason=?, done=?, rung=?, pages=?, words=?, tor_used=?, "
            "sites_part=?, finished_at=?, elapsed_ms=?, error=? WHERE id=?",
            (s.final_url, s.status, s.cls, s.reason[:200], done, s.rung, len(s.pages),
             sum(_w(t) for _, t in s.pages), int(s.tor), sites_part, now(), elapsed_ms, s.error or None, rid))

    def requeue(self, statuses):
        """make finished sites run again: their rows leave every table and track.db marks them retryable."""
        marks = ",".join("?" * len(statuses))
        ids = [r[0] for r in self.track.execute(f"SELECT id FROM track WHERE status IN ({marks})", list(statuses))]
        keys = {"sites": "id", "pages": "site_id", "logos": "site_id", "incidents": "site_id", "site_logs": "site_id", "facts": "site_id",
                "profiles": "site_id"}
        for name, col in keys.items():
            for f in glob.glob(os.path.join(self.root, f"{name}_*.db")):
                con = sqlite3.connect(f, isolation_level=None)
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    con.execute(f"DELETE FROM {name} WHERE {col} IN ({','.join('?' * len(chunk))})", chunk)
                con.close()
        self.track.execute(f"UPDATE track SET status='error', done=3, attempts=0, error='requeued' "
                           f"WHERE status IN ({marks})", list(statuses))
        return len(ids)

    def laya_calls(self, rows):
        self.sh["laya_calls"].insert_many(
            "INSERT INTO laya_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)", rows)

    def log(self, level, msg):
        try:
            self.sh["run_log"].insert("INSERT INTO run_log VALUES (?,?,?)", (now(), level, msg[:500]))
        except Exception:
            pass

    def counts(self):
        return dict(self.track.execute("SELECT status, COUNT(*) FROM track GROUP BY status").fetchall())

    def close(self):
        for sh in self.sh.values():
            sh.close()
        self.track.close()


def db_log_handler(store):
    class H(logging.Handler):
        def emit(self, rec):
            store.log(rec.levelname.lower(), self.format(rec))
    h = H()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.setLevel(logging.INFO)
    return h
