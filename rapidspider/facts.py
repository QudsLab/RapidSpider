"""facts.py - business facts per site, ingredient first.

info.yml lists the INGREDIENTS a long/short description and bullet points need (name, year founded, services,
email, hours ...). For every site:
  1. exact things are found by code, laya is not asked: emails and phones (contacts), the directory name seen on
     the page, and every category `patterns` regex (year, hours, company no., VAT ...)
  2. the page text is cut into small groups of numbered sentences; for each group laya answers one question PER
     ingredient: "which numbered sentence states X, or none?" - existence and place in one answer, all
     ingredients in one forward pass
  3. the picked sentence is the fact; its place (top/middle/bottom of the page, sentence no.) and laya's
     confidence are stored with it
  4. each picked sentence is re-questioned (differently worded, A/B flipped) - no human review
-> s.facts [{category,text,value,confidence,source,url,zone,loc,...}] and s.profile {profile, categories, ready}
"""
import re

import yaml

from .parse import near_dup, shingles, words

SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9£\"'(])")


class Category:
    def __init__(self, d, default_max):
        self.key = str(d["key"]).strip()
        self.label = d.get("label") or self.key
        self.bullet = d.get("bullet") or ""
        self.description = d.get("description") or self.label
        self.special = d.get("special") or ""        # emails | phones | name : found by code, never asked of laya
        self.ask = not self.special and d.get("ask", True)      # false = regex only (patterns), no laya question
        self.patterns = [(re.compile(p["re"]), int(p.get("value", 0))) for p in d.get("patterns") or []]
        self.examples = [str(e) for e in d.get("examples") or []]
        self.max = int(d.get("max") or default_max)


def load_categories(cfg):
    with open(cfg.facts.file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cats = [Category(c, cfg.facts.max_per_category) for c in data.get("categories") or []]
    keys = [c.key for c in cats]
    if not cats:
        raise SystemExit(f"{cfg.facts.file}: no categories")
    if len(set(keys)) != len(keys) or "none" in keys:
        raise SystemExit(f"{cfg.facts.file}: category keys must be unique (and none called 'none')")
    return cats


class Facts:
    def __init__(self, cfg, laya):
        self.cfg, self.laya = cfg, laya
        self.fc = cfg.facts
        self.cats = load_categories(cfg)
        self.by = {c.key: c for c in self.cats}
        self.desc = {c.key: c.description for c in self.cats}
        laya.bank.extend("fact", [(c.key, e) for c in self.cats for e in c.examples])

    # ------------------------------------------------------------------ blocks
    def blocks(self, pages):
        """pages [(Page, text)] -> [{url, text, zone, pos}] sentence-sized, de-duplicated, capped.
        zone = top / middle / bottom of its page (position in the text, deterministic)."""
        fc, seen, out = self.fc, set(), []
        for page, text in pages:
            part_list = []
            for ln in (text or "").split("\n"):
                for part in SENT.split(ln.strip()):
                    part = part.strip()
                    n = words(part)
                    k = part.lower()
                    if not (fc.block_min_words <= n <= fc.block_max_words) or k in seen:
                        continue
                    seen.add(k)
                    part_list.append(part)
            for i, part in enumerate(part_list):
                f = i / max(len(part_list), 1)
                out.append({"url": page.url, "text": part, "zone": "top" if f < 1 / 3 else "middle" if f < 2 / 3 else "bottom",
                            "pos": f"{i + 1}/{len(part_list)}"})
        return out[: fc.max_blocks]

    def chunks(self, blocks):
        """consecutive sentences of one page, `scan.chunk_sentences` at a time -> [[block index]]"""
        out, cur, url = [], [], None
        for i, b in enumerate(blocks):
            if cur and (b["url"] != url or len(cur) >= self.fc.scan.chunk_sentences):
                out.append(cur)
                cur = []
            cur.append(i)
            url = b["url"]
        if cur:
            out.append(cur)
        return out

    def _about(self, s, cat):
        d = self.by[cat].description
        # on a third-party page the sentence must be about THIS trader, not another listing on the page
        return f"{d}, said about the business called {s.row.name}" if s.status == "third_party" and s.row.name else d

    # ------------------------------------------------------------------ extract
    async def extract(self, s):
        fc = self.fc
        q0 = self.laya.calls
        blocks = self.blocks(s.pages)
        found = []        # dict: category,text,value,conf,source,url,zone,loc
        def add(cat, text, value, conf, src, url="", zone="", loc=""):
            found.append({"category": cat, "text": text[:300], "value": value, "conf": conf, "source": src,
                          "url": url, "zone": zone, "loc": loc})

        # 1. exact things, no laya ------------------------------------------------------------------
        page_txt = "\n".join((p.title or "") + "\n" + t for p, t in s.pages)
        for c in self.cats:
            if c.special == "emails":
                for e in sorted(s.emails)[:3]:
                    add(c.key, f"Email: {e}", e, 1.0, "contacts")
            elif c.special == "phones":
                for ph in sorted(s.phones)[:3]:
                    add(c.key, f"Phone: {ph}", ph, 1.0, "contacts")
            elif c.special == "name" and s.row.name:
                from .classify import name_in_text
                if name_in_text(page_txt, s.row.name):
                    add(c.key, f"Trading name: {s.row.name}", s.row.name, 1.0, "row+page")
            for b in blocks:
                for rx, g in c.patterns:
                    m = rx.search(b["text"])
                    if m:
                        val = (m.group(g) if g <= (m.re.groups or 0) else m.group(0)).strip()[:120]
                        add(c.key, b["text"], val, 1.0, "pattern", b["url"], b["zone"], b["pos"])

        # 2+3. per group of sentences: which sentence states each ingredient (or none) ----------------
        ask = {c.key: c.description for c in self.cats if c.ask}
        n_chunks = 0
        for chunk in self.chunks(blocks):
            n_chunks += 1
            got = await self.laya.pick([blocks[i]["text"] for i in chunk], ask, fc.scan.questions_per_pass)
            for k, (j, conf) in got.items():
                if j is None or conf < fc.scan.pick_min:
                    continue
                b = blocks[chunk[j]]
                add(k, b["text"], None, round(conf, 3), "laya", b["url"], b["zone"], b["pos"])

        # 4. verify: every located sentence is re-questioned by laya (differently worded, A/B flipped) --
        v = fc.verify
        found.sort(key=lambda f: (-f["conf"], f["source"] == "laya"))
        checked, seen_txt = [], set()
        for f in found:
            k = (f["category"], f["text"].lower(), f["value"])
            if k in seen_txt:
                continue
            seen_txt.add(k)
            checked.append(f)
        sure = [f for f in checked if f["source"] != "laya"]
        checked = sure + [f for f in checked if f["source"] == "laya"][: v.max_candidates]
        out_all = []
        for f in checked:
            cat, src, conf = f["category"], f["source"], f["conf"]
            if src != "laya":          # exact values need no vote
                out_all.append({**f, "confidence": 1.0, "score": 1.0, "votes": "exact", "review": "auto_good"})
                continue
            ps = await self.laya.fact_votes(f["text"], self._about(s, cat), v.rounds) if v.rounds > 0 else []
            n = len(ps)
            agree = sum(p > 0.5 for p in ps)
            mean = sum(ps) / n if n else 1.0
            frac = agree / n if n else 1.0
            if frac >= v.accept_frac and mean >= v.accept_mean:
                review = "auto_good"
            elif frac <= v.reject_frac:
                review = "auto_bad"
            else:
                review = "auto_unsure"
            if v.auto_teach and n and conf >= 0.8:
                if review == "auto_good" and agree == n and mean >= 0.8:
                    self.laya.bank.add_auto("fact", cat, f["text"])
                elif review == "auto_bad" and agree == 0:
                    self.laya.bank.add_auto("fact", "none", f["text"])
            out_all.append({**f, "confidence": round((conf + mean) / 2, 3), "score": round(mean, 3),
                            "votes": f"{agree}/{n}", "review": review})
        # 5. merge the accepted: best first, no near-duplicates, cap per category; rejected ones stay for audit
        good = sorted((f for f in out_all if f["review"] == "auto_good"), key=lambda f: -f["confidence"])
        kept, shs, per = [], {}, {}
        for f in good:
            sh = shingles(f["text"])
            cat = f["category"]
            if per.get(cat, 0) >= self.by[cat].max or (sh and near_dup(sh, shs.setdefault(cat, []), 0.6)):
                f["review"] = "auto_duplicate"
                continue
            if sh:
                shs[cat].append(sh)
            per[cat] = per.get(cat, 0) + 1
            kept.append(f)
        others = [f for f in out_all if f["review"] != "auto_good"][: v.max_rejected_stored]
        profile, values = {}, {}
        for f in kept:
            profile.setdefault(f["category"], []).append(f["text"])
            if f["value"]:
                values.setdefault(f["category"], []).append(f["value"])
        contact = {"emails": sorted(s.emails)[:5], "phones": sorted(s.phones)[:5]}
        s.facts = kept + others
        s.profile = {"profile": {"name": s.row.name, "url": s.final_url, "contact": contact, "facts": profile, "values": values},
                     "categories": len(profile), "ready": len(profile) >= fc.ready_min_categories}
        n_q = self.laya.calls - q0
        s.note("facts", "ok", f"{len(kept)} accepted, {len(others)} rejected/unsure, {len(profile)}/{len(self.cats)} "
                              f"ingredients found | {len(blocks)} sentences in {n_chunks} groups | {n_q} laya questions")
