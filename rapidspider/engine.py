"""engine.py - the per-site flow and the worker pool.

  DNS -> escalation ladder (http -> browser -> headed -> tor ...)
      each fetched page is CLASSIFIED (classify.py); config.classes.<class>.action says what to do:
        wait         browser keeps polling; clears cookies + reloads on a timer; laya referees
        escalate     try the next rung of fetch.ladder
        stop         final verdict (dead ...) - laya double-checks first
        single_page  third-party host: keep this one page, never crawl
        crawl        real site: laya reviews it, then sitemap/links -> a few pages
  -> logos (optional, laya picks) -> sqlite
"""
import asyncio
import copy
import re
from collections import deque
import time
import traceback
from urllib.parse import urlparse

from . import logos as logos_mod
from .store import now
from .classify import Verdict, classify, identity, identity_hit, third_party_extra
from .fetch import RUNGS, resolves
from .laya import ANOMALIES, CUR_SITE, DEAD_ANOMALIES, LayaDown
from .parse import (clean_text, near_dup, shingles, contacts_of, host_of, links_of, norm_url, robots_sitemaps, same_site,
                    sitemap_urls, soup_of, words)


class Site:
    def __init__(self, row):
        self.row = row
        self.status = "error"
        self.cls = ""
        self.reason = ""
        self.rung = ""
        self.tor = False
        self.final_url = ""
        self.pages = []          # [(Page, text)]
        self.emails, self.phones = set(), set()
        self.summary = ""
        self.home_html = ""
        self.error = ""
        self.log_text = None     # normally built from incidents
        self.incidents = []      # (at, step, outcome, detail) - flushed once in store.save
        self.logos = []
        self.facts = None        # list once facts were extracted (None = not run)
        self.profile = None
        self.stage = "starting"
        self.t0 = time.time()

    def note(self, step, outcome, detail=""):
        self.stage = f"{step}: {outcome}"[:60]
        self.incidents.append((now(), step, outcome, str(detail)[:300]))

    def set(self, status, cls="", reason=""):
        self.status, self.cls, self.reason = status, cls, reason


class Engine:
    MAX_WORKERS = 16

    def __init__(self, cfg, store, fetcher, laya, log, facts=None, loader=None):
        self.cfg, self.store, self.f, self.laya, self.log = cfg, store, fetcher, laya, log
        self.facts = facts
        self.loader = loader                  # () -> all input rows (UI "scan input")
        self.stop = asyncio.Event()           # laya kept failing
        self.laya_down_streak = 0
        self.done = 0
        self.total = 0
        # ---- control surface (the web UI flips these) ----
        self.paused = False
        self.threads = max(1, int(cfg.run.threads))
        self.quit = False
        self.active = {}                      # id -> Site (being processed now)
        self.q = None
        self.queued = set()
        self.shared = set()                   # hosts used by 2+ input rows (branches of one group site)
        self.host_cache = {}                  # host -> what the one fetch of that site gave
        self.fetching = set()
        self.loop = None
        self.finished = deque(maxlen=400)     # finish times -> sites/min

    @property
    def mode(self):
        if self.quit:
            return "stopping" if self.active else "stopped"
        if self.paused:
            return "paused" if not self.active else "pausing"
        return "running" if (self.active or (self.q is not None and not self.q.empty())) else "idle"

    def enqueue(self, rows):
        cnt = {}
        for r in rows:
            h = host_of(norm_url(r.url) or "")
            if h:
                cnt[h] = cnt.get(h, 0) + 1
        self.shared |= {h for h, n in cnt.items() if n > 1}
        return self._enqueue(rows)

    def _enqueue(self, rows):
        n = 0
        for r in rows:
            if r.id in self.queued or r.id in self.active or not self.store.should_run(r):
                continue
            self.queued.add(r.id)
            self.q.put_nowait(r)
            n += 1
        self.total += n
        return n

    def rescan(self):
        """UI: re-read the input and queue every site that still has to run (new rows, requeued sites)."""
        return self.enqueue(self.loader()) if self.loader else 0

    # ================================================================= pool
    async def run(self, rows):
        self.loop = asyncio.get_running_loop()
        self.q = self.q or asyncio.Queue()
        n = self.enqueue(rows)
        self.log.info("rows in input: %d | to run: %d | already final: %d", len(rows), n, len(rows) - n)
        keep = bool(self.cfg.ui.enabled and self.cfg.ui.keep_alive)

        async def worker(i):
            while not self.quit and not self.stop.is_set():
                if self.paused or i >= self.threads:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    row = self.q.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.5)
                    continue
                self.queued.discard(row.id)
                await self.process(row)
                if self.cfg.run.pause_between_sites_sec:
                    await asyncio.sleep(self.cfg.run.pause_between_sites_sec)

        ws = [asyncio.create_task(worker(i)) for i in range(self.MAX_WORKERS)]
        try:
            while not self.quit and not self.stop.is_set():
                await asyncio.sleep(0.5)
                if not keep and self.q.empty() and not self.active:
                    break
        finally:
            self.quit = True
            await asyncio.gather(*ws, return_exceptions=True)
        if self.stop.is_set():
            self.log.error("run stopped early: laya kept failing")

    # ================================================================= one site
    async def process(self, row):
        t0 = time.time()
        url = norm_url(row.url)
        host = host_of(url) if url else ""
        if host in self.shared and host not in self.host_cache:
            if host in self.fetching:                   # another branch of this site is being fetched: do something else first
                self.queued.add(row.id)
                self.q.put_nowait(row)
                await asyncio.sleep(0.3)
                return
            self.fetching.add(host)
        attempt = self.store.begin(row, url)
        s = Site(row)
        CUR_SITE.set(str(row.id))
        self.active[row.id] = s
        deadline = t0 + self.cfg.run.site_budget_sec
        sessions = []
        try:
            if not url:
                s.set("blocked", "error", "no usable url")
            else:
                s.final_url = url
                await asyncio.wait_for(self._site_shared(s, url, deadline, sessions, host), self.cfg.run.site_budget_sec + 45)
                if self.facts and s.status in ("ok", "third_party") and s.pages:
                    s.stage = "facts"
                    try:
                        await asyncio.wait_for(self.facts.extract(s), 180)
                    except asyncio.TimeoutError:
                        s.facts, s.profile = [], {"profile": {}, "categories": 0, "ready": False}
                        s.note("facts", "timeout", "180s")
            self.laya_down_streak = 0
        except asyncio.TimeoutError:
            s.set("timeout", s.cls, "site budget exhausted")
        except LayaDown as e:
            s.set("laya_down", s.cls, f"laya unreachable: {e}")
            self.laya_down_streak += 1
            if self.laya_down_streak >= 5:
                self.stop.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            s.set("error", s.cls, f"{type(e).__name__}: {e}")
            s.error = traceback.format_exc()[-800:]
        finally:
            self.fetching.discard(host)
            for ss in sessions:
                try:
                    await ss.close()
                except Exception:
                    pass
        self.active.pop(row.id, None)
        self._save(s, t0, attempt)

    def _save(self, s, t0, attempt):
        self.store.save(s, attempt, int((time.time() - t0) * 1000), store_text=s.status in ("ok", "third_party"))
        self.done += 1
        self.finished.append(time.time())
        self.log.info("[%d/%d] id=%s %s -> %s%s | pages=%d words=%d rung=%s %.1fs", self.done, self.total, s.row.id,
                      host_of(s.final_url or s.row.url) or (s.row.url or "?")[:40], s.status,
                      f" ({s.cls}:{s.reason[:50]})" if s.reason else "",
                      len(s.pages), sum(words(t) for _, t in s.pages), s.rung or "-", time.time() - t0)

    # ================================================================= group sites (one host, many input rows)
    async def _site_shared(self, s, url, deadline, sessions, host):
        """branches of a group site share ONE fetch. Every branch then keeps only the text that proves to be about it."""
        if host not in self.shared:
            return await self._site(s, url, deadline, sessions)
        c = self.host_cache.get(host)
        if c is None:
            await self._site(s, url, deadline, sessions)
            if s.status == "ok":
                c = ("ok", list(s.pages), s.final_url, s.rung, s.tor)
            elif s.cls != "third_party":
                c = ("fail", s.status, s.cls, s.reason)
            if c:
                self.host_cache[host] = c
        else:
            s.note("group-site", "cached", f"{host}: fetched once for all branches")
            if c[0] == "fail":
                s.set(c[1], c[2], c[3])
                return
            s.final_url, s.rung, s.tor = c[2], c[3], c[4]
        if c and c[0] == "ok":
            await self._branch(s, c[1])

    async def _branch(self, s, pages):
        tp = self.cfg.classes.third_party
        need = int(tp.get("identity_min", 2))
        good, proofs = [], set()
        for page, text in pages:
            ok, pr = identity(page, s.row, need)
            if ok:
                good.append((copy.copy(page), text))      # own copy: raw is set per branch
                proofs.update(pr)
        s.note("identity", "ok" if good else "failed",
               f"group site: {len(good)}/{len(pages)} pages prove this branch ({'+'.join(sorted(proofs)) or 'no proof'})")
        s.pages, s.emails, s.phones = [], set(), set()
        for page, text in good:
            await self._trader_lines(s, page, text)
        if not s.pages:
            s.set("no_content", "third_party", "group site: no text about this branch")
            return
        s.set(tp.get("status", "third_party"), "third_party", f"group site | proof {'+'.join(sorted(proofs))} | {len(s.pages)} page(s)"[:120])

    # ================================================================= flow
    async def _site(self, s, url, deadline, sessions):
        rid = s.row.id
        host = host_of(url)
        ladder = [r for r in self.cfg.fetch.ladder if r in RUNGS and self.f.has_rung(r)]

        ok, why = await resolves(host)
        if not ok and not host.startswith("www."):                # bare domain without DNS: try www.
            ok2, _ = await resolves("www." + host)
            if ok2:
                u = urlparse(url)
                url = u._replace(netloc="www." + u.netloc).geturl()
                s.final_url, ok = url, True
        if not ok:
            s.note("dns", "failed", why)
            verdict = await self._confirm_dead(s, url, why)
            if verdict == "dead" or verdict == "unconfirmed":
                return
            ladder = [r for r in ladder if RUNGS[r][2]] or ["http_tor"]   # reachable only via tor

        accepted = await self._ladder(s, url, ladder, deadline, sessions)
        if not accepted:
            return
        sess, page = accepted
        if s.status != "ok":            # third_party / no_content decided inside the ladder
            return
        await self._crawl(s, sess, page, deadline)
        if s.status != "ok":
            return
        if self.cfg.laya.summary and s.pages:
            s.summary = await self.laya.summary(s.pages[0][1])
        if self.cfg.logos.enabled and s.home_html:
            s.logos = await logos_mod.hunt(rid, s.row.name, s.final_url, s.home_html, self.cfg, self.f, self.laya, s.tor)

    # ----------------------------------------------------------------- DNS dead
    async def _confirm_dead(self, s, url, why):
        net = self.cfg.network
        rid = s.row.id
        if self.f.http_tor:
            for i in range(net.dead_confirm_tries):
                pg = await (await self.f.session("http_tor")).get(url)
                if pg.html or (pg.status and pg.status < 500):
                    s.note("dead-confirm", "reachable via tor", f"try {i + 1}")
                    s.tor = True
                    return "reachable"
                await asyncio.sleep(1.5)
            s.set("dead", "dead", f"dns dead (confirmed: direct + {net.dead_confirm_tries} tor tries)")
            return "dead"
        if net.accept_unconfirmed_dead:
            s.set("dead", "dead", f"dns dead ({why}), tor not configured")
            return "dead"
        s.set("error", "error", f"dns failed ({why}); tor not configured, not final - will retry")
        return "unconfirmed"

    # ----------------------------------------------------------------- ladder
    async def _ladder(self, s, url, ladder, deadline, sessions):
        rid = s.row.id
        cl = self.cfg.classes
        seen = []       # (rung, verdict) of every attempt
        for rung in ladder:
            if deadline - time.time() < 6:
                s.set("timeout", s.cls, "no time left for next rung")
                return None
            kind = RUNGS[rung][0]
            try:
                sess = await self.f.session(rung)
            except Exception as e:
                s.note(rung, "unavailable", f"{type(e).__name__}: {e}")
                continue
            sessions.append(sess)
            page = await sess.get(url, deadline)
            if page.error and not page.html and url.startswith("https://") and not s.row.url.lower().startswith("https://"):
                # input had no scheme and https failed (old sites are often http-only): try plain http once
                alt = "http://" + url[len("https://"):]
                s.note(rung, "https-failed", page.error[:100])
                page = await sess.get(alt, deadline)
                if page.html:
                    url = alt
            v = classify(page, self.cfg, s.row.name)
            s.note(rung, v.cls, f"{v.kind} {v.reason} http={page.status} {page.ms}ms {page.error}")

            if v.cls in ("holding", "cloudflare") and cl[v.cls].action == "wait":
                if kind == "browser":
                    page, v = await self._wait_loop(sess, page, v, deadline, rung)
                    s.note(rung, f"after-wait:{v.cls}", f"{v.kind} {v.reason}")

            seen.append((rung, v))
            if page.url:
                s.final_url = page.url

            # ---- decide by class ------------------------------------------------
            if v.cls == "third_party":
                await self._single_page(s, page, v, sess, deadline)
                s.rung, s.tor = rung, RUNGS[rung][2]
                return sess, page

            if v.cls == "dead":
                # 404/410 over plain http is not final: a bot wall can look like that. confirm in a browser.
                if v.kind.startswith("http_") and kind == "http" and any(RUNGS[r][0] == "browser" for r in ladder):
                    continue
                if v.kind != "laya_giveup":
                    real, an, near = await self.laya.page_review(page.text, page.title)
                    s.note("laya-review", an, f"real={real}{self._near(near, an)}")
                    if an == "none" and real is not False and not v.kind.startswith("http_"):
                        v = Verdict("ok", "laya-overrode-dead")      # pattern false positive
                if v.cls == "dead":
                    s.set("dead", "dead", f"{v.kind}")
                    s.rung, s.tor = rung, RUNGS[rung][2]
                    return None

            if v.cls == "ok":
                tp = third_party_extra(page, self.cfg, s.row.name)
                if tp:
                    await self._single_page(s, page, tp, sess, deadline)
                    s.rung, s.tor = rung, RUNGS[rung][2]
                    return sess, page
                real, an, near = await self.laya.page_review(page.text, page.title)
                s.note("laya-review", an, f"real={real}{self._near(near, an)}")
                # laya alone must not end a site: its verdict is re-asked (different wording, flipped keys) first
                if an != "none" and not await self._sure(s, page, an, ANOMALIES.get(an, an)):
                    an = "none"
                if an in DEAD_ANOMALIES:
                    s.set("dead", "dead", f"laya:{an}")
                    s.rung, s.tor = rung, RUNGS[rung][2]
                    return None
                if an == "third_party_platform":
                    await self._single_page(s, page, Verdict("third_party", "laya", "laya: not the business's own site"), sess, deadline)
                    s.rung, s.tor = rung, RUNGS[rung][2]
                    return sess, page
                if an == "login_wall":
                    s.set("blocked", "error", "login wall")
                    return None
                if an == "holding_page":
                    seen[-1] = (rung, Verdict("holding", "laya", "laya says holding page"))
                    continue
                if real is False:
                    # laya may only veto a THIN page, and only when the veto survives re-questioning
                    wc = words(page.text)
                    if wc >= self.cfg.parse.veto_min_words:
                        s.note("laya-review", "veto-overridden", f"laya said no real content but page has {wc} words")
                    elif await self._sure(s, page, "no_content", "this page has no real business content, only cookie "
                                          "notices, error text, parking text or navigation"):
                        s.set("no_content", "empty", "laya: no real business content")
                        return None
                s.set("ok", "ok", "")
                s.rung, s.tor = rung, RUNGS[rung][2]
                s.home_html = page.html
                return sess, page
            # everything else: escalate to the next rung

        # ladder exhausted
        classes = [v.cls for _, v in seen]
        last = seen[-1][1] if seen else Verdict("error", "", "no rung could run")
        if any(c in ("cloudflare", "holding") for c in classes):
            bad = next(v for _, v in seen if v.cls in ("cloudflare", "holding"))
            s.set("blocked", bad.cls, f"{bad.kind} {bad.reason}"[:80])
        elif any(c in ("js_shell", "empty") for c in classes):
            s.set("no_content", last.cls, last.reason)
        else:
            s.set("error", last.cls, f"{last.kind} {last.reason}"[:80])
        return None

    async def _sure(self, s, page, label, claim):
        frac, ps = await self.laya.confirm(page.text, claim)
        ok = frac >= self.cfg.laya.confirm.min_agree
        s.note("laya-confirm", f"{label}: {'confirmed' if ok else 'rejected'}", f"{frac:.2f} of {len(ps)} rounds agree")
        return ok

    @staticmethod
    def _near(near, an):
        """diagnostic: where laya disagrees with a very similar solved example, say so in incidents."""
        if near and near[0] >= 0.8 and near[1] != an:
            return f" | DISAGREES with near example ({near[0]:.2f}: {near[1]})"
        return ""

    # ----------------------------------------------------------------- garbage reduction
    async def _tidy(self, s, page, seen_lines, kept):
        """clean_text -> drop lines seen on earlier pages -> laya keep/drop per block -> near-duplicate paragraphs."""
        cfg, pc = self.cfg, self.cfg.parse
        raw = clean_text(page.html, cfg)
        page.raw = raw                       # the full text before any laya filtering; stored in pages.raw for later reprocessing
        lines = [ln for ln in raw.split("\n") if ln not in seen_lines]
        seen_lines.update(raw.split("\n"))
        n_laya = n_dup = 0
        if pc.laya_block_filter and lines:
            judge = [i for i, ln in enumerate(lines) if pc.block_min_words <= words(ln) <= pc.block_max_words]
            drop, shaky = set(), []
            for a in range(0, len(judge), pc.block_batch):
                chunk = judge[a:a + pc.block_batch]
                for i, (keep, conf) in zip(chunk, await self.laya.block_verdicts([lines[i] for i in chunk])):
                    if not keep and conf >= pc.drop_confidence:
                        drop.add(i)
                        if conf < 0.85:
                            shaky.append(i)
            if shaky and pc.block_recheck_rounds > 0:      # an unsure "drop" must survive a second opinion
                for i, p_keep in zip(shaky, await self.laya.block_recheck([lines[i] for i in shaky], pc.block_recheck_rounds)):
                    if p_keep >= 0.5:
                        drop.discard(i)
            tot = sum(words(lines[i]) for i in judge) or 1
            gone = sum(words(lines[i]) for i in drop)
            if drop and gone / tot > pc.max_drop_ratio:
                # laya wants to throw away nearly everything: it is mis-calibrated for this page, not the page bad
                s.note("tidy", "drop-guard", f"{page.url}: laya wanted to drop {gone}/{tot} words - ignored")
                drop = set()
            n_laya = len(drop)
            lines = [ln for i, ln in enumerate(lines) if i not in drop]
        out = []
        for ln in lines:
            sh = shingles(ln)
            if sh and near_dup(sh, kept, pc.near_dup):
                n_dup += 1
                continue
            if sh:
                kept.append(sh)
            out.append(ln)
        text = "\n".join(out)[: cfg.crawl.max_page_chars]
        s.note("tidy", "ok", f"{page.url}: {len(raw)}->{len(text)} chars | laya dropped {n_laya} | near-dup {n_dup}")
        return text

    # ----------------------------------------------------------------- waiting on shields
    async def _wait_loop(self, sess, page, v, deadline, rung):
        conf = self.cfg.classes[v.cls]
        t_start = time.time()
        max_wait = max(0.0, min(conf.max_wait_sec, deadline - t_start - 5))
        poll = conf.poll_sec
        ce = conf.get("clear_every_sec") or 0
        last_clear = t_start
        n = 0
        snap, v2 = page, v
        while time.time() - t_start < max_wait:
            await asyncio.sleep(poll)
            await sess.poke()
            snap = await sess.snapshot(page.status)
            v2 = classify(snap, self.cfg)
            if v2.cls not in ("holding", "cloudflare"):
                return snap, v2
            n += 1
            if n % 3 == 1:
                ruling = await self.laya.wait_referee(snap.text)
                # laya may only wave a page through when it is not a tiny shell
                if ruling == "content" and words(snap.text) >= self.cfg.crawl.min_page_words * 2:
                    return snap, Verdict("ok", "laya", "laya: content visible")
                if ruling == "giveup":
                    return snap, Verdict("dead", "laya_giveup", "laya: permanent error page")
            if ce and time.time() - last_clear >= ce:
                await sess.clear_cookies(conf.get("clear_cookies"))
                await sess.reload()
                last_clear = time.time()
        return snap, v2

    # ----------------------------------------------------------------- third party
    async def _single_page(self, s, page, v, sess=None, deadline=0):
        """a page that is not the trader's own site is used STRICTLY: it must prove it is this trader (name plus
        phone / email / postcode from the directory row). 'More details' links from it are followed the same
        way - a linked page is kept only if it proves it is the same trader too."""
        tp = self.cfg.classes.third_party
        need = int(tp.get("identity_min", 2))
        ok, proofs = identity(page, s.row, need)
        s.note("identity", "ok" if ok else "failed", f"{page.url}: {'+'.join(proofs) or 'no proof'}")
        if not ok:
            s.set("no_content", "third_party", f"third-party page not proven to be this trader ({'+'.join(proofs) or 'no proof'})")
            return
        seen_lines, kept = set(), []
        got = await self._trader_page(s, page, seen_lines, kept)
        if not got:
            s.set("no_content", "third_party", "third-party page has no text about this trader itself")
            return
        n = int(tp.get("follow_max", 0))
        if sess is not None and n > 0:
            base = page.url or s.final_url
            cands = [(u, t) for u, t in links_of(soup_of(page.html), base) if same_site(u, base) and self._key(u) != self._key(base)]
            if len(cands) > 1:
                cands = await self.laya.rank_links(cands, "more details about this same company: its profile, about, services, contact")
            for u, _t in cands[:n]:
                if deadline and deadline - time.time() < 8:
                    break
                p = await sess.get(u, deadline)
                if classify(p, self.cfg).cls != "ok":
                    continue
                ok2, pr2 = identity(p, s.row, need)
                s.note("identity", "ok" if ok2 else "skipped", f"{u}: {'+'.join(pr2) or 'no proof'}")
                if ok2:
                    await self._trader_page(s, p, seen_lines, kept)
        s.set(tp.get("status", "third_party"), "third_party", f"{v.kind}: {v.reason} | proof {'+'.join(proofs)} | {len(s.pages)} page(s)"[:120])

    async def _trader_page(self, s, page, seen_lines, kept):
        """third-party page: keep ONLY the lines about this trader, never the portal's own text or other listings.
        A line stays when it carries the trader's name/phone/email/postcode, or laya says it is about the trader."""
        return await self._trader_lines(s, page, await self._tidy(s, page, seen_lines, kept))

    async def _trader_lines(self, s, page, text):
        tp = self.cfg.classes.third_party
        lines = [ln for ln in text.split("\n") if ln.strip()]
        mine, rest = [], []
        for ln in lines:
            (mine if identity_hit(ln, s.row) else rest).append(ln)
        if rest:
            ps = await self.laya.about_trader(rest, s.row.name)
            mine += [ln for ln, p in zip(rest, ps) if p >= float(tp.get("trader_min", 0.6))]
        mine = [ln for ln in lines if ln in set(mine)]           # page order
        out = "\n".join(mine)
        page.raw = out                       # third-party: the portal's own text is never stored
        s.note("trader-only", "ok", f"{page.url}: kept {len(mine)}/{len(lines)} lines")
        if words(out) < self.cfg.crawl.min_page_words:
            return False
        s.pages.append((page, out))
        em, ph = contacts_of("<p>" + out.replace("\n", "</p><p>") + "</p>")       # contacts only from the trader's own text
        s.emails.update(em), s.phones.update(ph)
        return True

    # ================================================================= crawl
    async def _crawl(self, s, sess, home, deadline):
        cfg = self.cfg
        c = cfg.crawl
        seen_lines, kept = set(), []
        home_text = await self._tidy(s, home, seen_lines, kept)
        s.pages.append((home, home_text))
        em, ph = contacts_of(home.html)
        s.emails.update(em), s.phones.update(ph)
        budget = c.max_pages - 1

        if budget > 0:
            cands = await self._candidates(s, home, sess)
            if len(cands) > budget:
                cands = await self.laya.rank_links(cands, "contact, about, services, location, team")
            total_chars = len(home_text)
            for u, _t in cands:
                if budget <= 0 or deadline - time.time() < 8 or total_chars >= c.max_site_chars:
                    break
                p = await sess.get(u, deadline)
                v = classify(p, cfg)
                s.note("subpage", v.cls, f"{u} http={p.status}")
                if v.cls != "ok":
                    continue
                em, ph = contacts_of(p.html)
                s.emails.update(em), s.phones.update(ph)
                text = await self._tidy(s, p, seen_lines, kept)
                if words(text) < c.min_page_words:
                    continue
                s.pages.append((p, text))
                total_chars += len(text)
                budget -= 1

        total = sum(words(t) for _, t in s.pages)
        if total < c.min_site_words:
            s.set("no_content", "empty", f"only {total} words")
            s.pages = s.pages[:1] if total else []
            return
        s.set("ok", "ok", "")

    async def _candidates(self, s, home, sess):
        c = self.cfg.crawl
        base = home.url or s.final_url
        links = [(u, t) for u, t in links_of(soup_of(home.html), base) if same_site(u, base)]
        if c.use_sitemap:
            for u in await self._sitemap(base, s.tor):
                if same_site(u, base):
                    links.append((u, ""))
        out, seen = [], {self._key(base)}
        for u, t in links:
            k = self._key(u)
            lu = u.lower()
            if k in seen or urlparse(lu).path.endswith(tuple(c.skip_ext)) or any(x in lu for x in c.skip_paths):
                continue
            seen.add(k)
            out.append((u, t))

        def pref(item):
            lu = item[0].lower()
            for i, kw in enumerate(c.prefer_paths):
                if kw in lu or kw in item[1].lower():
                    return i
            return 999
        out.sort(key=pref)          # stable: nav order kept inside each preference bucket
        return out[:60]

    @staticmethod
    def _key(u):
        p = urlparse(u)
        return (p.hostname or "").removeprefix("www.") + p.path.rstrip("/") + ("?" + p.query if p.query else "")

    async def _sitemap(self, base, tor):
        p = urlparse(base)
        root = f"{p.scheme}://{p.netloc}"
        robots = await self.f.aux_text(root + "/robots.txt", tor)
        queue = robots_sitemaps(robots) or [root + "/sitemap.xml"]
        urls, done = [], 0
        while queue and done < 4:
            sm = queue.pop(0)
            done += 1
            body = await self.f.aux_text(sm, tor)
            pages, kids = sitemap_urls(body)
            urls += pages
            queue += kids[:3]
        return urls[:200]
