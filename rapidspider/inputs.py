"""inputs.py - read the site list from CSV / SQLite / JSON into Row objects."""
import csv
import json
import os
import sqlite3
from dataclasses import dataclass, field

CANON = ("id", "url", "name", "phone", "address", "email")


@dataclass
class Row:
    id: str
    url: str
    name: str = ""
    phone: str = ""
    address: str = ""
    email: str = ""
    extra: dict = field(default_factory=dict)


def _pick(rec, spec, default=""):
    """spec: column name or list of names (joined with ', ')."""
    if not spec:
        return default
    names = spec if isinstance(spec, (list, tuple)) else [spec]
    parts = []
    for n in names:
        v = rec.get(n)
        if v is not None and str(v).strip():
            parts.append(str(v).strip())
    return ", ".join(parts) if parts else default


def _records(cfg):
    inp = cfg.input
    fmt = inp.format
    path = inp.path
    if fmt == "auto":
        ext = os.path.splitext(path)[1].lower()
        fmt = {".csv": "csv", ".tsv": "csv", ".json": "json", ".sqlite": "sqlite", ".db": "sqlite",
               ".sqlite3": "sqlite"}.get(ext)
        if not fmt:
            raise SystemExit(f"cannot guess input format from '{ext}', set input.format")
    if fmt == "csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            yield from csv.DictReader(f, dialect=dialect)
    elif fmt == "json":
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        yield from data
    elif fmt == "sqlite":
        if not inp.table:
            raise SystemExit("input.table is required for sqlite input")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = f'SELECT * FROM "{inp.table}"'
        if inp.where:
            sql += f" WHERE {inp.where}"
        try:
            for r in con.execute(sql):
                yield dict(r)
        finally:
            con.close()
    else:
        raise SystemExit(f"unknown input.format '{fmt}'")


def load_rows(cfg, only=None):
    cols = cfg.input.columns
    extras = cfg.input.extra_columns or []
    rows, seen = [], set()
    for rec in _records(cfg):
        rid = _pick(rec, cols.get("id"))
        url = _pick(rec, cols.get("url"))
        if not rid or rid in seen:
            continue
        if only and rid not in only:
            continue
        seen.add(rid)
        rows.append(Row(
            id=rid, url=url,
            name=_pick(rec, cols.get("name")), phone=_pick(rec, cols.get("phone")),
            address=_pick(rec, cols.get("address")), email=_pick(rec, cols.get("email")),
            extra={k: rec.get(k) for k in extras if k in rec},
        ))
        if cfg.input.limit and len(rows) >= cfg.input.limit and not only:
            break
    return rows
