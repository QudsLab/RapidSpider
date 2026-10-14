"""laya.py - the laya referee, running INSIDE this process. MANDATORY: no off switch, no heuristic fallback.

laya's Router is loaded once (models stay resident); questions run in a worker thread behind a lock so the
asyncio loop stays free. If laya cannot load, the run aborts. If a question keeps failing, the site is parked
as 'laya_down' (retryable) rather than being decided without laya.

Every question is (mostly) a 2..n option `choice` and carries the few solved examples nearest to the text being
judged (examples.py), so config.yml -> laya.examples / info.yml / learned_examples.yml steer it.
EVERY question and answer is written to laya_calls_*.db (see _log) and can be reviewed in the web UI.
"""
import asyncio
import contextvars
import json
import os
import re
import threading
import time

from .examples import ExampleBank
from .store import now

CUR_SITE = contextvars.ContextVar("cur_site", default="")      # engine sets it per site -> laya_calls.site_id


class LayaDown(Exception):
    pass


def choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def p_pick(ans, key):
    """probability that a choice answer == key (uses laya's confidence)."""
    if not isinstance(ans, dict):
        return 0.0
    c = ans.get("choice")
    conf = ans.get("confidence")
    conf = float(conf) if isinstance(conf, (int, float)) else 0.9
    if c is None:
        v = ans.get("noul")
        return float(v) if isinstance(v, (int, float)) else 0.0
    return conf if str(c) == key else 1.0 - conf


ANOMALIES = {
    "none": "a normal, working site page with real content",
    "domain_parked": "the page is advertising that this domain is for sale",
    "domain_expired": "the page says the domain registration expired",
    "hosting_placeholder": "the page is a hosting/under-construction placeholder with no business content",
    "registrar_landing": "the page is a domain registrar or search landing page",
    "error_page": "the page shows an error like 404 / site not found",
    "login_wall": "the page requires login to see anything",
    "holding_page": "the page is a 'please wait / verifying' shell with no real content",
    "third_party_platform": ("the page belongs to a social network, marketplace, directory, listing portal or "
                             "multi-branch corporate group - NOT the business's own standalone website"),
}
DEAD_ANOMALIES = {"domain_parked", "domain_expired", "hosting_placeholder", "registrar_landing", "error_page"}


def answer_label(ans):
    """-> (label, confidence) from a laya answer (choice or noul)."""
    if not isinstance(ans, dict):
        return None, None
    conf = ans.get("confidence")
    if ans.get("choice") is not None:
        return str(ans["choice"]), float(conf) if isinstance(conf, (int, float)) else None
    v = ans.get("noul")
    if isinstance(v, (int, float)):
        return ("yes" if v > 0.5 else "no"), max(float(v), 1.0 - float(v))
    return None, None


class Laya:
    def __init__(self, cfg, log, store=None):
        self.cfg = cfg.laya
        self.log = log
        self.store = store
        self.bank = ExampleBank(cfg)
        self.router = None
        self.lock = threading.Lock()
        self.calls = 0
        self.total_ms = 0
        self.last_error = ""
        self.info = {}

    # ---------------------------------------------------------------- lifecycle
    def _load(self):
        if os.environ.get("LAYA_DEBUG") != "1":
            import warnings
            warnings.filterwarnings("ignore", message=".*invalid temperatures.*")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            import laya
            from laya import Router
        except Exception as e:
            raise SystemExit(f"laya is mandatory but its python package cannot be imported ({type(e).__name__}: {e}).\n"
                             f"  pip install -r requirements.txt")
        device = self.cfg.device
        torch = None
        try:
            import torch
            torch.set_num_threads(max(1, int(self.cfg.torch_threads)))
        except Exception:
            pass
        if device == "auto":
            device = "cuda" if (torch is not None and torch.cuda.is_available()) else "cpu"
            if device == "cpu":
                self.log.info("laya: CUDA not available, running on CPU")
        names = ([m.strip() for m in str(self.cfg.models).split(",") if m.strip()] if self.cfg.models != "auto"
                 else (["english"] if device == "cpu" else list(laya.DEFAULT_MODELS)))
        specs = {n: laya.DEFAULT_MODELS[n] for n in names if n in laya.DEFAULT_MODELS}
        self.log.info("laya: loading %s on %s (first run downloads the checkpoint)...", list(specs), device)
        self.router = Router(device=device, preload=True, max_loaded=len(specs), models=specs)
        self.info = {"version": getattr(laya, "__version__", "?"), "device": device, "models": list(specs)}
        threading.Thread(target=self._ram_guard, daemon=True).start()

    def _ram_guard(self):
        try:
            import psutil
        except ImportError:
            return
        proc = psutil.Process()
        while True:
            time.sleep(30)
            try:
                mb = proc.memory_info().rss / 1e6
                loaded = self.router.loaded
                if mb > self.cfg.max_ram_mb and len(loaded) > 1:
                    self.router.unload(loaded[0])
                    self.log.warning("laya: RAM %.0fMB > %dMB - unloaded '%s'", mb, self.cfg.max_ram_mb, loaded[0])
            except Exception:
                continue

    async def ensure_running(self):
        await asyncio.to_thread(self._load)
        self.log.info("laya: ready (embedded) %s", self.info)

    async def close(self):
        self.bank.flush()

    # ---------------------------------------------------------------- transport + logging
    def _predict(self, state, qs):
        with self.lock:
            return self.router.predict(state, qs)

    async def ask(self, state, questions, meta=None):
        """meta: {qkey: {task, text, near:(sim,label)|None, shots: 'a,b'}} - only used for the laya_calls log."""
        qs = dict(list(questions.items())[:80])
        last = ""
        for attempt in range(1, self.cfg.retries + 2):
            t0 = time.time()
            try:
                res = await asyncio.to_thread(self._predict, state, qs)
                answers = (res or {}).get("answers") or {}
                ms = int((time.time() - t0) * 1000)
                self.calls += len(qs)
                self.total_ms += ms
                self._log(state, qs, answers, meta or {}, ms)
                return answers
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                self.last_error = last
                self.log.warning("laya question failed (%d/%d): %s", attempt, self.cfg.retries + 1, last[:200])
                await asyncio.sleep(0.5 * attempt)
        self._log(state, qs, {"error": last}, meta or {}, 0, error=last)
        raise LayaDown(last)

    def _log(self, state, qs, answers, meta, ms, error=None):
        lg = self.cfg.log
        if not (lg.enabled and self.store):
            return
        site, rows = CUR_SITE.get(), []
        for k, q in qs.items():
            m = meta.get(k) or {}
            task = m.get("task") or k
            ans = answers.get(k) if isinstance(answers, dict) else None
            label, conf = answer_label(ans)
            near = m.get("near")
            text = m.get("text")
            if text is None:
                text = (state or {}).get("body", "")
            full = task in lg.full_prompt_tasks
            rows.append((now(), site, task, k, str(text)[: m.get("chars") or lg.text_chars], m.get("shots", ""),
                         near[0] if near else None, near[1] if near else None, label if not error else "ERROR", conf,
                         json.dumps(ans if not error else {"error": error}, ensure_ascii=False, default=str)[:600],
                         (q.get("instructions") or "")[:1800] if full else None, ms, len(qs)))
        try:
            self.store.laya_calls(rows)
        except Exception as e:      # logging never breaks the run
            self.log.warning("laya_calls log failed: %s", e)

    def stats(self):
        return {"calls": self.calls, "avg_ms": int(self.total_ms / max(1, self.calls)), "last_error": self.last_error,
                **self.info}

    # ---------------------------------------------------------------- questions
    async def wait_referee(self, text):
        """-> 'content' | 'wait' | 'giveup'"""
        t = (text or "")[:2500]
        near = self.bank.nearest("wait", t, 1)
        a = await self.ask({"body": t}, {
            "verdict": choice("This is a page the scraper is waiting on. Classify it." + self.bank.fewshot("wait", t), {
                "content": "real business content is now visible",
                "waiting_shell": "still a holding/verification shell - KEEP WAITING, server is processing",
                "challenge": "bot-challenge page - keep waiting briefly",
                "permanent_error": "permanent error/parked/placeholder - GIVE UP, waiting is pointless",
            })}, {"verdict": {"task": "wait", "text": t, "near": near[0][:2] if near else None}})
        c = str((a.get("verdict") or {}).get("choice", "")).lower()
        if c == "content":
            return "content"
        if c == "permanent_error":
            return "giveup"
        return "wait"

    async def page_review(self, text, title):
        """-> (real_content: bool|None, anomaly: str, nearest_example: (sim,label)|None)"""
        t = (text or "")[:3000]
        near = self.bank.nearest("page_kind", f"{title}\n{t}", 1)
        a = await self.ask({"body": t, "title": (title or "")[:200]}, {
            # `noul` + this exact wording is what the old JS referee used on thousands of sites; a yes/no `choice`
            # made laya answer "no" for almost every page. Its veto is also size-guarded in engine.py.
            "real_content": {
                "type": "noul",
                "instructions": ("Answer yes only if this page shows real website content of an actual business or "
                                 "organisation (services, contact details, about info). Answer no ONLY for: "
                                 "cookie/consent panels, error pages, parking pages, 'under construction', or "
                                 "navigation menus with no content. A business website with services and contact "
                                 "info is YES.")},
            "anomaly": choice(
                "Read this page text. Which statement is TRUE? A real business website (even a small one) is 'none'. "
                "Only pick an anomaly if it clearly matches." + self.bank.fewshot("page_kind", f"{title}\n{t}"),
                ANOMALIES),
        }, {"real_content": {"task": "page_real", "text": t},
            "anomaly": {"task": "page_kind", "text": f"{title}\n{t}", "near": near[0][:2] if near else None}})
        rc = a.get("real_content")
        real = (p_pick(rc, "yes") > 0.5) if rc else None     # p_pick reads `noul` when no `choice` is present
        an = str((a.get("anomaly") or {}).get("choice") or "none")
        return real, an, (near[0][0], near[0][1]) if near else None

    async def rank_links(self, cands, hint):
        """cands: [(url, text)] -> same tuples, most promising first."""
        if len(cands) < 2:
            return cands
        sl = cands[:40]
        a = await self.ask(
            {"body": json.dumps({"hint": hint, "links": [{"i": i, "url": u, "text": t} for i, (u, t) in enumerate(sl)]})},
            {"ranking": choice(
                f"Which links are most likely to hold business info ({hint})? Return keys in priority order.",
                {str(i): f"{t} {u}"[:140] for i, (u, t) in enumerate(sl)})},
            {"ranking": {"task": "rank", "text": hint}})
        nums = [int(n) for n in re.findall(r"\d+", str((a.get("ranking") or {}).get("choice", "")))]
        out, used = [], set()
        for n in nums:
            if 0 <= n < len(sl) and n not in used:
                used.add(n)
                out.append(sl[n])
        out += [c for i, c in enumerate(sl) if i not in used]
        return out + cands[40:]

    async def logo_verdicts(self, items, site_name):
        """items: [{src, alt, source, where}] -> [float 0..1 = P(logo)]"""
        qs, meta = {}, {}
        for i, it in enumerate(items):
            desc = (f"file: {str(it.get('src'))[-120:]} | alt: {str(it.get('alt'))[:80]} | "
                    f"found as: {it.get('source')} | placement: {it.get('where')}")
            qs[f"l{i}"] = choice(
                f"Is this image the brand LOGO of the business '{site_name}'? A logo is the brand mark shown in the "
                f"site header; photos, banners, favicons and decorations are not logos." + self.bank.fewshot("logo", desc, 2)
                + f"\nImage: {desc}",
                {"logo": "the brand logo", "not_logo": "photo, banner, icon or decoration"})
            nr = self.bank.nearest("logo", desc, 2)
            meta[f"l{i}"] = {"task": "logo", "text": desc, "near": nr[0][:2] if nr else None,
                             "shots": ",".join(x[1] for x in nr)}
        a = await self.ask({"body": f"Logo check for {site_name}"}, qs, meta)
        return [p_pick(a.get(f"l{i}"), "logo") for i in range(len(items))]

    async def block_verdicts(self, blocks):
        """blocks: [str] -> [(keep: bool, confidence)] - the garbage filter."""
        qs, meta = {}, {}
        for i, b in enumerate(blocks):
            snip = re.sub(r"\s+", " ", b)[:220]
            qs[f"b{i}"] = choice(
                "Is this text block real, useful content about the business (what it does or sells, services, "
                "location, opening hours, contact, terms) or boilerplate noise (cookie notice, newsletter box, "
                "menu or navigation, footer or copyright, forms, review-widget text, pagination)?"
                + self.bank.fewshot("block", snip, 2) + f"\nBlock: {snip}",
                {"keep": "real content about the business", "drop": "boilerplate or noise"})
            nr = self.bank.nearest("block", snip, 2)
            meta[f"b{i}"] = {"task": "block", "text": snip, "near": nr[0][:2] if nr else None,
                             "shots": ",".join(x[1] for x in nr)}
        a = await self.ask({"body": "Decide for each block: keep or drop."}, qs, meta)
        out = []
        for i in range(len(blocks)):
            ans = a.get(f"b{i}")
            if not ans:
                out.append((True, 0.0))         # no answer -> keep (never lose content on a miss)
                continue
            keep = p_pick(ans, "keep")
            out.append((keep >= 0.5, max(keep, 1.0 - keep)))
        return out

    async def summary(self, text):
        a = await self.ask({"body": (text or "")[:1500]}, {
            "summary": choice("Summarize what kind of business this is in one short line.", {"a": "one line summary"})},
            {"summary": {"task": "summary", "text": (text or "")[:300]}})
        return str((a.get("summary") or {}).get("choice") or "")

    async def fact_verdicts(self, blocks, cats):
        """blocks: [(text, [candidate category keys])] -> [(category|None, confidence)].
        cats: {key: description}. Each block is a SMALL choice (its keyword-matched categories + 'none')."""
        qs, meta = {}, {}
        for i, (text, cand) in enumerate(blocks):
            snip = re.sub(r"\s+", " ", text)[:260]
            crit = {k: cats[k] for k in cand}
            crit["none"] = "no concrete fact of these kinds (generic marketing, navigation, opinion, unrelated)"
            nr = self.bank.nearest("fact", snip, 3, labels=set(crit))
            fs = ("\nSimilar solved examples:\n" + "\n".join(f'- "{t[:self.bank.max_chars]}" => {l}' for _s, l, t in nr)) if nr else ""
            qs[f"f{i}"] = choice(
                "Which kind of fact about the business does this text state? Answer 'none' unless it clearly "
                "states a concrete fact of one of these kinds." + fs + f"\nText: {snip}", crit)
            meta[f"f{i}"] = {"task": "fact", "text": snip, "near": nr[0][:2] if nr else None,
                             "shots": ",".join(x[1] for x in nr)}
        a = await self.ask({"body": "Which kind of business fact is each text?"}, qs, meta)
        out = []
        for i, (_t, cand) in enumerate(blocks):
            label, conf = answer_label(a.get(f"f{i}"))
            out.append((label if label in cand else None, conf or 0.0))
        return out

    async def about_trader(self, blocks, name):
        """lines of a third-party page -> [P(line is about the business called `name` itself)]"""
        out = []
        for a in range(0, len(blocks), 40):
            part = blocks[a:a + 40]
            qs, aff, meta = {}, {}, {}
            for j, b in enumerate(part):
                snip = re.sub(r"\s+", " ", b)[:260]
                flip = (a + j) % 2 == 1
                yes, no = f"about {name} itself", "about the website, other businesses, or generic page text"
                crit = {"A": no, "B": yes} if flip else {"A": yes, "B": no}
                k = f"t{j}"
                aff[k] = "B" if flip else "A"
                qs[k] = choice(f"This line is from a page that is not {name}'s own website. Is it about the business called "
                               f"'{name}' (its own services, stock, place, contact, history) or about something else?\nLine: {snip}", crit)
                meta[k] = {"task": "trader_only", "text": snip, "shots": name[:40] + ("*" if flip else "")}
            ans = await self.ask({"body": f"Keep only what is about {name}."}, qs, meta)
            for j in range(len(part)):
                lab, conf = answer_label(ans.get(f"t{j}"))
                if lab is None:
                    out.append(0.0)         # no answer on a third-party page: leave the line out (strict)
                    continue
                conf = 0.7 if conf is None else conf
                out.append(conf if lab == aff[f"t{j}"] else 1.0 - conf)
        return out

    async def pick(self, sents, cats, per_pass=24):
        """a few numbered sentences, one question per ingredient: WHICH sentence clearly states it, or none.
        Presence and place in one answer; all questions share one state, so one forward pass per `per_pass`.
        -> {key: (sentence index | None, confidence)}"""
        sents = [re.sub(r"\s+", " ", s)[:240] for s in sents]
        state = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sents))[:1500]
        crit = {str(i + 1): f"sentence {i + 1}" for i in range(len(sents))}
        crit["none"] = "no sentence clearly states it"
        keys = list(cats)
        out = {}
        for g in range(0, len(keys), per_pass):
            qs, meta, order = {}, {}, {}
            for j, k in enumerate(keys[g:g + per_pass]):
                nr = self.bank.nearest("fact", state, 1, labels={k})
                fs = f'\nExample of a sentence that states it: "{nr[0][2][:self.bank.max_chars]}"' if nr else ""
                qk = f"s{j}"
                order[qk] = k
                qs[qk] = choice(f"Which numbered sentence clearly states a concrete fact about: {cats[k]}? "
                                f"Answer none unless one sentence clearly does." + fs, crit)
                meta[qk] = {"task": "scan", "text": state[:260], "shots": k}
            a = await self.ask({"body": state}, qs, meta)
            for qk, k in order.items():
                lab, conf = answer_label(a.get(qk))
                idx = int(lab) - 1 if lab and lab.isdigit() and 0 < int(lab) <= len(sents) else None
                out[k] = (idx, 0.7 if conf is None else conf)
        return out

    # ================================================================= self-verification (no human review)
    # Laya is over-confident and position-biased, so every decision that matters is re-asked several times with
    # different wording, and the A/B answer keys are flipped between rounds. Agreement across rounds is the real
    # confidence. Each round is one cheap question; they all share one state, so one forward pass answers them all.
    async def _votes(self, state_text, task, templates, rounds, fmt, label="", chars=200):
        """templates: [(name, instruction, affirmative_desc, negative_desc)] -> [P(affirmative) per round]"""
        qs, aff, meta = {}, {}, {}
        n = len(templates)
        for i in range(rounds):
            name, instr, yes, no = templates[i % n]
            flip = ((i // n) + i) % 2 == 1
            crit = {"A": no, "B": yes} if flip else {"A": yes, "B": no}
            k = f"v{i}"
            aff[k] = "B" if flip else "A"
            qs[k] = choice(instr.format(**fmt), crit)
            meta[k] = {"task": task, "text": label or state_text, "shots": name + ("*" if flip else ""), "chars": chars}
        a = await self.ask({"body": (state_text or "")[:1500]}, qs, meta)
        ps = []
        for k, key in aff.items():
            lab, conf = answer_label(a.get(k))
            if lab is None:
                continue
            conf = 0.7 if conf is None else conf
            ps.append(conf if lab == key else 1.0 - conf)
        return ps

    async def fact_votes(self, text, desc, rounds):
        return await self._votes(text, "fact_verify", FACT_CHECKS, rounds, {"desc": desc})

    async def confirm(self, page_text, claim, rounds=None):
        """-> (agree_fraction, [p]) that `claim` really describes this page. rounds=0 -> (1.0, [])."""
        rounds = self.cfg.confirm.rounds if rounds is None else rounds
        if rounds <= 0:
            return 1.0, []
        ps = await self._votes((page_text or "")[:1800], "confirm", PAGE_CHECKS, rounds, {"claim": claim},
                               label=claim, chars=160)
        return (sum(p > 0.5 for p in ps) / len(ps) if ps else 1.0), ps

    async def block_recheck(self, blocks, rounds):
        """blocks the filter wants to drop -> [P(keep)] after `rounds` re-questions each."""
        out = []
        for b in blocks:
            ps = await self._votes(re.sub(r"\s+", " ", b)[:260], "block_recheck", BLOCK_CHECKS, rounds, {})
            out.append(sum(ps) / len(ps) if ps else 0.5)
        return out


FACT_CHECKS = [
    ("states_fact", "Does this text state a concrete fact about the business itself?",
     "yes, it states a concrete fact about the business", "no, it is vague, generic or about something else"),
    ("specific", "Is this text specific to one business, rather than generic wording that fits any business website?",
     "specific to this business", "generic wording"),
    ("category", "Is this text about: {desc}?",
     "yes, that is what the text states", "no, the text is about something else"),
    ("own_claim", "Is the business itself saying this about itself today (not a customer quote, a past event or a third party)?",
     "yes, the business states it about itself", "no, it is a quote, history or about someone else"),
    ("complete", "Is this a complete, understandable statement (not a menu, heading, price list or fragment)?",
     "complete statement", "fragment or list"),
    ("useful", "Would this help a new customer understand the business?",
     "useful to a customer", "not useful"),
    ("clean", "Is this text free of cookie, legal, navigation or form boilerplate?",
     "clean content", "boilerplate"),
]
PAGE_CHECKS = [
    ("true", "Is it true that {claim}?", "true", "false"),
    ("matches", "Does this page clearly match this description: {claim}?", "matches", "does not match"),
    ("visitor", "Would a visitor looking at this page agree that {claim}?", "agrees", "disagrees"),
    ("evidence", "Is there clear evidence in the text that {claim}?", "clear evidence", "no clear evidence"),
    ("plain", "Reading the text plainly: {claim}. Is that right?", "right", "wrong"),
]
BLOCK_CHECKS = [
    ("useful", "Is this text useful content about the business (what it does, services, location, hours, contact, terms)?",
     "useful content", "boilerplate or noise"),
    ("not_ui", "Is this text real sentences about the business, rather than menu, cookie, form or footer text?",
     "real content", "menu, cookie, form or footer text"),
]
