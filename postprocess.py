#!/usr/bin/env python3
"""postprocess.py - run laya over data that was stored in raw mode (laya.enabled: false).

    python postprocess.py config.yml --from output/retry
    python postprocess.py config.yml --from backup/dental --limit 200
    python postprocess.py config.yml --from output/retry --only 12,34

Takes every site with track.processed = 0 (status ok / third_party / no_content), rebuilds its pages from the stored
raw text and html, and runs the normal laya steps: page review, third-party trader-only filter, block cleaning,
facts and profiles. The site's rows are rewritten, the final status is set and processed becomes 1.
Stop and start it any time: only sites with processed = 0 are taken. Use a config with laya.enabled true
(config.yml); copy the output folder to the machine that has laya and point --from at the copy.
"""
import argparse
import asyncio
import glob
import json
import os
import sqlite3
import sys
import time
import zlib

from main import setup_logging
from rapidspider import config as config_mod
from rapidspider.classify import identity
from rapidspider.engine import Engine, Site
from rapidspider.facts import Facts
from rapidspider.fetch import Page, make_page
from rapidspider.inputs import Row
from rapidspider.laya import ANOMALIES, CUR_SITE, DEAD_ANOMALIES, Laya, LayaDown
from rapidspider.parse import contacts_of, words
from rapidspider.store import Store

TODO = "('ok', 'third_party', 'no_content')"


class Reader:
    """read-only handles on every shard of a table."""

    def __init__(self, root):
        self.root, self.cons = root, {}

    def rows(self, name, where, params=()):
        if name not in self.cons:
            self.cons[name] = []
            for f in sorted(glob.glob(os.path.join(self.root, f"{name}_*.db"))):
                c = sqlite3.connect(f, timeout=30, check_same_thread=False)
                c.row_factory = sqlite3.Row
                self.cons[name].append(c)
        out = []
        for c in self.cons[name]:
            try:
                out += [dict(r) for r in c.execute(f"SELECT * FROM {name} WHERE {where}", params)]
            except sqlite3.Error:
                pass
        return out

    def close(self):
        for cs in self.cons.values():
            for c in cs:
                c.close()
        self.cons = {}


def load_site(rd, sid):
    """-> (Row, [Page], attempt, old_log_text) from the stored rows of the newest attempt."""
    srows = rd.rows("sites", "id=?", (sid,))
    if not srows:
        raise ValueError("no sites row")
    srow = max(srows, key=lambda r: r["attempt"] or 0)
    att = srow["attempt"]
    row = Row(id=sid, url=srow["input_url"] or "", name=srow["name"] or "", phone=srow["input_phone"] or "",
              address=srow["input_address"] or "", email=srow["input_email"] or "",
              extra=json.loads(srow["extra"] or "{}"))
    pages = []
    for r in rd.rows("pages", "site_id=? AND attempt=?", (sid, att)):
        html = zlib.decompress(r["html_z"]).decode("utf-8", "replace") if r.get("html_z") else ""
        raw = r["raw"] or r["text"] or ""
        if html:
            p = make_page(r["url"], r["http_status"] or 200, html)
        else:
            p = Page(url=r["url"], status=r["http_status"] or 200, text=raw, title=r["title"] or "")
        p.stored_raw = raw
        p.raw = raw
        pages.append(p)
    if not pages and srow["text"]:                      # no pages table rows: fall back to the one joined text
        p = Page(url=srow["final_url"] or row.url, status=200, text=srow["text"])
        p.stored_raw = p.raw = srow["text"]
        pages.append(p)
    logs = rd.rows("site_logs", "site_id=? AND attempt=?", (sid, att))
    return row, pages, att, (logs[0]["log"] if logs else "")


def raw_of(p):
    return p.stored_raw if p.stored_raw else None


async def judge(eng, s, pages):
    """the laya decisions the live run makes, on stored pages. Sets s.status / s.pages / s.emails / s.phones."""
    cfg, laya = eng.cfg, eng.laya
    home = pages[0]
    real, an, near = await laya.page_review(home.text, home.title)
    s.note("laya-review", an, f"real={real}{eng._near(near, an)}")
    if an != "none" and not await eng._sure(s, home, an, ANOMALIES.get(an, an)):
        an = "none"
    if an in DEAD_ANOMALIES:
        s.set("dead", "dead", f"laya:{an}")
        return
    if an == "login_wall":
        s.set("blocked", "error", "login wall")
        return
    if an == "holding_page":
        s.set("blocked", "holding", "laya says holding page")
        return
    if an == "third_party_platform":
        await third_party(eng, s, pages, "laya: not the business's own site")
        return
    if real is False:
        wc = words(home.text)
        if wc >= cfg.parse.veto_min_words:
            s.note("laya-review", "veto-overridden", f"laya said no real content but page has {wc} words")
        elif await eng._sure(s, home, "no_content", "this page has no real business content, only cookie notices, "
                             "error text, parking text or navigation"):
            s.set("no_content", "empty", "laya: no real business content")
            return
    await own_site(eng, s, pages)


async def own_site(eng, s, pages):
    c = eng.cfg.crawl
    seen_lines, kept = set(), []
    s.pages, s.emails, s.phones = [], set(), set()
    for i, p in enumerate(pages):
        text = await eng._tidy(s, p, seen_lines, kept, raw=raw_of(p))
        em, ph = contacts_of(p.html or "<p>" + (p.stored_raw or "").replace("\n", "</p><p>") + "</p>")
        s.emails.update(em), s.phones.update(ph)
        if i > 0 and words(text) < c.min_page_words:
            continue
        s.pages.append((p, text))
    total = sum(words(t) for _, t in s.pages)
    if total < c.min_site_words:
        s.set("no_content", "empty", f"only {total} words")
        s.pages = s.pages[:1] if total else []
        return
    s.set("ok", "ok", "")


async def third_party(eng, s, pages, why):
    """strict, like the live run: a page is kept only if it proves it is this trader, and only the trader's own lines."""
    tp = eng.cfg.classes.third_party
    need = int(tp.get("identity_min", 2))
    seen_lines, kept = set(), []
    s.pages, s.emails, s.phones = [], set(), set()
    proofs_all = set()
    for p in pages:
        ok, proofs = identity(p, s.row, need)
        s.note("identity", "ok" if ok else "failed", f"{p.url}: {'+'.join(proofs) or 'no proof'}")
        if ok:
            proofs_all.update(proofs)
            await eng._trader_page(s, p, seen_lines, kept, raw=raw_of(p))
    if not s.pages:
        s.set("no_content", "third_party", f"third-party page has no proven text about this trader ({why})"[:120])
        return
    s.set(tp.get("status", "third_party"), "third_party",
          f"{why} | proof {'+'.join(sorted(proofs_all))} | {len(s.pages)} page(s)"[:120])


async def amain(cfg, only, limit):
    root = cfg.output.root
    tp = os.path.join(root, "track.db")
    if not os.path.isfile(tp):
        sys.exit(f"no track.db in {root}")
    store = Store(cfg)
    log = setup_logging(cfg, store)
    con = sqlite3.connect(tp)
    ids = [r[0] for r in con.execute(f"SELECT id FROM track WHERE processed=0 AND status IN {TODO} ORDER BY id")]
    con.close()
    if only:
        ids = [i for i in ids if i in only]
    if limit:
        ids = ids[:limit]
    log.info("postprocess %s: %d sites with processed=0", root, len(ids))
    if not ids:
        store.close()
        return 0
    laya = Laya(cfg, log, store)
    await laya.ensure_running()
    facts = Facts(cfg, laya) if cfg.facts.enabled else None
    eng = Engine(cfg, store, None, laya, log, facts)
    rd = Reader(root)
    done, bad, streak = 0, 0, 0
    t0 = time.time()
    try:
        for n, sid in enumerate(ids, 1):
            t1 = time.time()
            try:
                row, pages, attempt, old_log = load_site(rd, sid)
                if not pages:
                    raise ValueError("no stored pages")
                tr = store.track.execute("SELECT status, final_url, rung, tor_used FROM track WHERE id=?", (sid,)).fetchone()
                old_status = tr[0]
                s = Site(row)
                s.final_url, s.rung, s.tor = tr[1] or row.url, tr[2] or "", bool(tr[3])
                s.stage = "postprocess"
                CUR_SITE.set(str(sid))
                await judge(eng, s, pages)
                if facts and s.status in ("ok", "third_party") and s.pages:
                    try:
                        await asyncio.wait_for(facts.extract(s), 180)
                    except asyncio.TimeoutError:
                        s.facts, s.profile = [], {"profile": {}, "categories": 0, "ready": False}
                        s.note("facts", "timeout", "180s")
                s.note("postprocess", f"{old_status}->{s.status}", "raw data judged by laya")
                if not s.pages:                      # dead / blocked / nothing kept: keep the raw pages anyway
                    s.pages = [(p, "") for p in pages]
                s.log_text = (old_log + "\n--- postprocess ---\n" if old_log else "") + "\n".join(
                    f"[{at}] {step}: {out} {det}".rstrip() for at, step, out, det in s.incidents)
                store.purge([sid], ["sites", "pages", "facts", "profiles", "site_logs"])
                store.save(s, attempt, int((time.time() - t1) * 1000), store_text=s.status in ("ok", "third_party"))
                done += 1
                streak = 0
                log.info("[%d/%d] id=%s %s -> %s%s | pages=%d words=%d %.1fs", n, len(ids), sid, old_status, s.status,
                         f" ({s.reason[:50]})" if s.reason else "", len(s.pages),
                         sum(words(t) for _, t in s.pages), time.time() - t1)
            except LayaDown as e:
                bad += 1
                streak += 1
                log.error("id=%s laya unreachable: %s (left processed=0)", sid, e)
                if streak >= 5:
                    log.error("laya kept failing - stopped")
                    break
            except Exception as e:
                bad += 1
                log.error("id=%s %s: %s (left processed=0)", sid, type(e).__name__, e)
    except KeyboardInterrupt:
        print("\ninterrupted - sites not finished stay processed=0")
    finally:
        log.info("postprocess finished: %d done, %d failed, %.0f min", done, bad, (time.time() - t0) / 60)
        rd.close()
        await laya.close()
        store.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--from", dest="src", required=True, help="output folder to post-process (holds track.db)")
    ap.add_argument("--only", help="comma separated ids")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many sites")
    a = ap.parse_args()
    cfg = config_mod.load(a.config, need_input=False)
    if not cfg.laya.enabled:
        sys.exit("this config has laya.enabled: false - use a config with laya on (config.yml)")
    cfg.output["root"] = os.path.abspath(a.src)
    cfg.output["store_html"] = True                     # the rewrite must keep the stored html
    only = set(x.strip() for x in a.only.split(",")) if a.only else None
    try:
        sys.exit(asyncio.run(amain(cfg, only, a.limit)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
