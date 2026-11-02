#!/usr/bin/env python3
"""make_retry_input.py - ONE-TIME helper. NOT part of the program: main.py never runs it.

    python make_retry_input.py backup/dental/track.db input/sites.sqlite input/retry.sqlite [directory_table]

Writes a new sqlite file with one table `retry`: every site of the old track.db whose status is NOT ok and NOT
third_party (error, no_content, dead, blocked, ...), with ALL columns of the directory row (as they are) plus ALL
columns of its track row, prefixed `track_` (track_status, track_reason, track_attempts ...).
Both source files are opened read-only.
"""
import os
import sqlite3
import sys
from pathlib import Path


def uri(p):
    return Path(p).resolve().as_uri() + "?mode=ro"


def main():
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    track_db, dir_db, out_db = sys.argv[1:4]
    table = sys.argv[4] if len(sys.argv) == 5 else "sites"
    for p in (track_db, dir_db):
        if not os.path.isfile(p):
            sys.exit(f"not found: {p}")
    if os.path.exists(out_db):
        os.remove(out_db)
    os.makedirs(os.path.dirname(os.path.abspath(out_db)), exist_ok=True)
    con = sqlite3.connect(Path(out_db).resolve().as_uri(), uri=True)
    con.execute("ATTACH DATABASE ? AS tr", (uri(track_db),))
    con.execute("ATTACH DATABASE ? AS dr", (uri(dir_db),))
    tcols = [r[1] for r in con.execute("PRAGMA tr.table_info(track)")]
    dcols = [r[1] for r in con.execute(f'PRAGMA dr.table_info("{table}")')]
    if not tcols or not dcols:
        sys.exit("could not read the columns (wrong table name?)")
    sel = ", ".join([f'd."{c}"' for c in dcols] + [f't."{c}" AS "track_{c}"' for c in tcols])
    con.execute(
        f'CREATE TABLE main.retry AS SELECT {sel} FROM tr.track t JOIN dr."{table}" d '
        f'ON CAST(d."id" AS TEXT) = t.id WHERE t.status NOT IN (\'ok\', \'third_party\')')
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM retry").fetchone()[0]
    wanted = con.execute("SELECT COUNT(*) FROM tr.track WHERE status NOT IN ('ok', 'third_party')").fetchone()[0]
    print(f"wrote {n} sites to {out_db}")
    for st, c in con.execute("SELECT track_status, COUNT(*) FROM retry GROUP BY track_status ORDER BY 2 DESC"):
        print(f"  {st:12s} {c}")
    if n != wanted:
        print(f"WARNING: track.db has {wanted} such sites but only {n} matched a row in {table} (by id)")
    con.close()


if __name__ == "__main__":
    main()
