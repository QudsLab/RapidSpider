












"""examples.py - few-shot examples for laya, picked per question.

Laya is a small classifier with a tiny token budget, so it cannot be handed a whole example set.
Instead, for every question we pick the few examples most similar to the text being judged
(char-3gram cosine) - at most one per label first, so the options stay visible - and append them to the
question's instructions as   "text" => label .   Examples come from built-in defaults plus
config.yml -> laya.examples.<task>, info.yml (fact) and learned_examples.yml (written by the web UI review).
(task: page_kind | block | logo | wait | fact).
"""
import math
import os
import re
from collections import Counter

import yaml

DEFAULT_EXAMPLES = {
    "page_kind": [
        ("domain_parked", "This domain is for sale. Buy this domain today. Make an offer. Related searches."),
        ("domain_expired", "This domain name has expired. If you are the owner, renew your registration now."),
        ("hosting_placeholder", "Welcome to nginx! If you see this page, the web server is successfully installed."),
        ("hosting_placeholder", "This account has been suspended. Please contact your hosting provider."),
        ("hosting_placeholder", "Coming soon. Our new website is under construction. Check back shortly."),
        ("registrar_landing", "Your domain is registered with a registrar. Build your website today with our builder."),
        ("error_page", "404 Not Found. The page you are looking for does not exist. Return to the homepage."),
        ("login_wall", "Please log in to view this content. Username Password Forgot your password?"),
        ("holding_page", "One moment, please... Please wait while your request is being verified."),
        ("holding_page", "Checking your browser before accessing the site. This process is automatic."),
        ("third_party_platform", "Log in to Facebook to connect with this business. Create new account."),
        ("third_party_platform", "Find a branch near you. Our branches across the UK. Search thousands of listings."),
        ("none", "We are a family run business serving the local community for over twenty years. "
                 "Our team offers a wide range of services. Visit us or call to find out more."),
    ],
    "block": [
        ("keep", "We have been serving customers in the local area for over 20 years with honest, friendly service."),
        ("keep", "Our workshop offers inspections, servicing and repairs on all makes and models."),
        ("keep", "Unit 4, High Street, Leeds LS1 4AB. Open Monday to Saturday, 9am to 5:30pm."),
        ("drop", "We use cookies to improve your experience. By continuing you agree to our cookie policy."),
        ("drop", "Subscribe to our newsletter for the latest offers and news straight to your inbox."),
        ("drop", "Copyright 2024 Company Ltd. All rights reserved. Website design by Agency."),
        ("drop", "Home About Us Services Contact Privacy Policy Terms and Conditions Sitemap"),
        ("drop", "Read more. Collected on: Google reviews powered by Reviews.io"),
        ("drop", "Enter your name, email and phone number and we will call you back."),
    ],
    "logo": [
        ("logo", "file: /wp-content/uploads/logo.png | alt: Company logo | found as: img | placement: header link"),
        ("logo", "file: /assets/brand/site-logo.svg | alt: | found as: jsonld | placement: schema.org"),
        ("not_logo", "file: /images/hero-banner-2.jpg | alt: Showroom interior | found as: img | placement: body"),
        ("not_logo", "file: /favicon.ico | alt: | found as: favicon | placement: head"),
        ("not_logo", "file: /uploads/team-photo.jpg | alt: Our team | found as: og:image | placement: meta"),
    ],
    "wait": [
        ("waiting_shell", "One moment, please... Please wait while your request is being verified."),
        ("challenge", "Just a moment... Checking your browser before accessing the website."),
        ("permanent_error", "This domain is for sale. Account suspended. Domain expired."),
        ("content", "Welcome to our company. We offer a range of services. Contact us on 0113 123 4567."),
    ],
}


def _vec(t):
    t = re.sub(r"\s+", " ", (t or "").lower())[:1500]
    g = Counter(t[i:i + 3] for i in range(max(0, len(t) - 2)))
    return g, math.sqrt(sum(v * v for v in g.values())) or 1.0


def _cos(a, b):
    ga, na = a
    gb, nb = b
    if len(ga) > len(gb):
        ga, gb = gb, ga
    return sum(v * gb.get(k, 0) for k, v in ga.items()) / (na * nb)

DEFAULT_EXAMPLES["fact"] = [
    ("none", "Welcome to our website. We look forward to hearing from you."),
    ("none", "Click here to read more about our latest news and offers."),
]
TASKS = ("page_kind", "block", "logo", "wait", "fact")


class ExampleBank:
    def __init__(self, cfg):
        ex = cfg.laya.examples
        self.k = int(ex.get("k", 3))
        self.max_chars = int(ex.get("max_chars", 170))
        self.learned_file = ex.get("learned_file")
        self.auto_file = ex.get("auto_file")
        self.auto_max = int(ex.get("auto_max_per_label", 120))
        self.auto = {}
        self._auto_dirty = 0
        self.items = {}
        for task in TASKS:
            rows = list(DEFAULT_EXAMPLES[task]) if ex.get("use_defaults", True) else []
            for e in ex.get(task) or []:
                if isinstance(e, dict) and e.get("label") and e.get("text"):
                    rows.append((str(e["label"]), str(e["text"])))
            self.items[task] = [(lab, txt, _vec(txt)) for lab, txt in rows]
        self.learned = {}
        if self.learned_file and os.path.isfile(self.learned_file):
            try:
                with open(self.learned_file, "r", encoding="utf-8") as f:
                    self.learned = yaml.safe_load(f) or {}
            except Exception:
                self.learned = {}
            for task, rows in self.learned.items():
                for e in rows or []:
                    if task in self.items and e.get("label") and e.get("text"):
                        self.items[task].append((str(e["label"]), str(e["text"]), _vec(e["text"])))

        if self.auto_file and os.path.isfile(self.auto_file):
            try:
                with open(self.auto_file, "r", encoding="utf-8") as f:
                    self.auto = yaml.safe_load(f) or {}
            except Exception:
                self.auto = {}
            for task, labels in self.auto.items():
                for lab, texts in (labels or {}).items():
                    for t in texts or []:
                        if task in self.items:
                            self.items[task].append((str(lab), str(t), _vec(t)))

    def add_auto(self, task, label, text):
        """laya's own teaching: a decision every verification round agreed on becomes an example (capped per label,
        near-duplicates skipped so the pool stays diverse). Returns True when added."""
        text = re.sub(r"\s+", " ", text or "").strip()[:300]
        if task not in self.items or not label or len(text) < 12:
            return False
        have = self.auto.setdefault(task, {}).setdefault(label, [])
        if len(have) >= self.auto_max:
            return False
        v = _vec(text)
        for lab, _t, vec in self.items[task]:
            if lab == label and _cos(v, vec) >= 0.9:
                return False
        have.append(text)
        self.items[task].append((label, text, v))
        self._auto_dirty += 1
        if self._auto_dirty >= 25:
            self.flush()
        return True

    def flush(self):
        if not (self.auto_file and self._auto_dirty):
            return
        with open(self.auto_file, "w", encoding="utf-8") as f:
            f.write("# written by RapidSpider itself: examples laya learned from decisions every verification round agreed on.\n"
                    "# Safe to delete (it is rebuilt) or to edit. Your own corrections belong in learned_examples.yml.\n")
            yaml.safe_dump(self.auto, f, allow_unicode=True, sort_keys=False, width=200)
        self._auto_dirty = 0

    def extend(self, task, rows):
        """rows: [(label, text)] - e.g. the examples written in info.yml."""
        self.items.setdefault(task, [])
        self.items[task] += [(str(l), str(t), _vec(t)) for l, t in rows if l and t]

    def add(self, task, label, text):
        """learn one solved example now and keep it in learned_examples.yml (human review in the web UI)."""
        text = re.sub(r"\s+", " ", text or "").strip()[:400]
        if task not in self.items or not label or not text:
            return False
        self.items[task].append((label, text, _vec(text)))
        rows = self.learned.setdefault(task, [])
        rows[:] = [r for r in rows if r.get("text") != text]
        rows.append({"label": label, "text": text})
        if self.learned_file:
            with open(self.learned_file, "w", encoding="utf-8") as f:
                f.write("# written by the RapidSpider web UI (review pages). Edit freely: label + text per task.\n")
                yaml.safe_dump(self.learned, f, allow_unicode=True, sort_keys=False, width=200)
        return True

    def nearest(self, task, text, k=None, labels=None):
        """-> [(similarity, label, example_text)] most similar first, label-diverse. labels: only these labels."""
        pool = [p for p in (self.items.get(task) or []) if labels is None or p[0] in labels]
        if not pool or not (text or "").strip():
            return []
        v = _vec(text)
        scored = sorted(((_cos(v, vec), lab, txt) for lab, txt, vec in pool), key=lambda x: -x[0])
        k = k or self.k
        out, seen = [], set()
        for s in scored:                      # first pass: best example of each label
            if s[1] not in seen:
                seen.add(s[1])
                out.append(s)
            if len(out) >= k:
                return out
        return out

    def fewshot(self, task, text, k=None, labels=None):
        near = self.nearest(task, text, k, labels)
        if not near:
            return ""
        lines = [f'- "{txt[:self.max_chars]}" => {lab}' for _s, lab, txt in near]
        return "\nSimilar solved examples:\n" + "\n".join(lines)
