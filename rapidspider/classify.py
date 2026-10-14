"""classify.py - the decision engine's eyes: what KIND of page did we get?

Classes: ok | holding | cloudflare | dead | error | js_shell | empty   (third_party is decided by laya + signals,
never from a hard-coded domain list)
What to DO about each class is in config.yml -> classes.<name>.action.
The signatures below are built-in defaults; config extends them via
classes.<name>.extra_patterns.
"""
import re
from dataclasses import dataclass

from .parse import host_of, words

# ---- cloudflare: strong html tokens or a challenge title -------------------------------
CF_HTML = ("__cf_chl_", "cf-challenge", "cf-turnstile", "cloudflare.rocks", "cf-chl-widget")
CF_TITLE = re.compile(r"just a moment|attention required|checking your browser|performing security verification", re.I)
CF_TEXT = re.compile(r"ray id|checking your browser before accessing|enable javascript and cookies to continue", re.I)

# ---- holding / hosting shield pages (Imunify360, SiteGround, cPanel WAF ...) -----------
HOLDING = [
    r"one moment,? please", r"please wait while your request", r"please wait\W*$", r"server verification",
    r"verif(y|ying) (that )?you are (a )?human", r"imunify360", r"sgcaptcha", r"sg-captcha",
    r"ddos protection by", r"bot protection", r"security check", r"checking your browser",
    r"your request is being processed", r"you are being redirected", r"redirecting\W*$",
    r"access denied.*imunify", r"this process is automatic",
]
HOLDING_MAX_WORDS = 90

# ---- dead: parked / expired / placeholder / suspended  (regex, max_words) --------------
DEAD = {
    "parked": [
        (r"(this )?domain (name )?(is|may be) for sale", 400), (r"buy this domain", 400), (r"domain for sale", 400),
        (r"hugedomains", 400), (r"sedoparking|sedo\.com", 400), (r"parkingcrew", 400), (r"afternic", 400),
        (r"domain parking", 300), (r"this (web ?page|site) is parked", 300), (r"sponsored listings", 150),
        (r"related searches", 120),
    ],
    "expired": [
        (r"domain (name )?(has )?expired", 300), (r"registration (has )?expired", 300), (r"renew this domain", 300),
    ],
    "placeholder": [
        (r"default web ?site page", 200), (r"welcome to nginx", 200), (r"apache2? (ubuntu )?default page", 200),
        (r"^index of /", 200), (r"future home of", 200), (r"coming soon", 60), (r"under construction", 80),
        (r"(this )?account (has been|is) suspended", 300), (r"site not found", 150),
        (r"no website (is )?configured", 200), (r"default parallels plesk", 200), (r"web server is working", 200),
        (r"domain is not configured", 200), (r"nothing here yet", 100),
    ],
}

@dataclass
class Verdict:
    cls: str
    kind: str = ""        # sub-type: parked/expired/placeholder, http_404, ...
    reason: str = ""


def _signals_hit(text, cfg):
    sig = cfg.classes.third_party.get("signals") or []
    n = 0
    for p in sig:
        try:
            if re.search(p, text, re.I):
                n += 1
        except re.error:
            continue
    return n


def name_in_text(text, name):
    """does the page speak about THIS business? (distinctive name tokens)"""
    norm = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    nn = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if not nn or not norm:
        return False
    if len(nn) >= 6 and nn in norm:
        return True
    stop = {"ltd", "limited", "plc", "llp", "company", "and", "the", "services", "solutions", "group", "uk"}
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 5 and t not in stop]
    if not toks:
        return False
    hits = sum(1 for t in toks if t in norm)
    return hits >= 2 or (len(toks) == 1 and hits == 1)


def classify(p, cfg, name=""):
    """p: fetch.Page (status, url, html, text, title, error, scripts, ctype)."""
    c = cfg.classes
    if p.error and not p.html:
        return Verdict("error", p.error[:60], p.error)

    html_l = (p.html or "")[:200000].lower()
    text_l = (p.text or "").lower()
    w = words(p.text)

    # cloudflare
    if any(t in html_l for t in CF_HTML) and w < 250:
        return Verdict("cloudflare", "token", "cloudflare challenge markup")
    if CF_TITLE.search(p.title or "") and w < 250:
        return Verdict("cloudflare", "title", p.title)
    if "cloudflare" in text_l and CF_TEXT.search(text_l) and w < 150:
        return Verdict("cloudflare", "text", "cloudflare block text")
    for x in c.cloudflare.get("extra_patterns") or []:
        if re.search(x, text_l, re.I) and w < 250:
            return Verdict("cloudflare", "extra", x)

    # holding / hosting shield
    if w < HOLDING_MAX_WORDS:
        for x in HOLDING + list(c.holding.get("extra_patterns") or []):
            if re.search(x, text_l, re.I | re.M):
                return Verdict("holding", "text", x)

    # dead
    extra = [(x, 300) for x in (c.dead.get("extra_patterns") or [])]
    for kind, pats in list(DEAD.items()) + [("custom", extra)]:
        for rx, maxw in pats:
            if w <= maxw and re.search(rx, text_l, re.I | re.M):
                return Verdict("dead", kind, rx)

    # http level
    st = p.status or 0
    if st in (404, 410) and w < 120:
        return Verdict("dead", f"http_{st}", f"HTTP {st}")
    if st >= 400:
        return Verdict("error", f"http_{st}", f"HTTP {st}")

    # empty shells
    if w < 15 and p.scripts >= 3:
        return Verdict("js_shell", "", f"{w} words, {p.scripts} scripts")
    if w < cfg.crawl.min_page_words:
        return Verdict("empty", "", f"{w} words")

    return Verdict("ok")


def third_party_extra(p, cfg, name=""):
    """post-fetch check on an otherwise ok page: portal chrome / not about this business."""
    t = cfg.classes.third_party
    text = (p.title or "") + "\n" + (p.text or "")
    sig_n = _signals_hit(text, cfg)
    if t.get("signals") and sig_n >= int(t.get("signals_min", 5)):
        return Verdict("third_party", "signals", f"{sig_n} portal signals")
    return None


_PC = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b", re.I)


def identity(p, row, need=2):
    """proofs that a page is about THIS trader (from the directory row): name, phone, email, postcode.
    The name is always required; `need` counts it. Proofs the row cannot offer lower the bar to what exists.
    -> (ok, [proofs found])"""
    text = (p.title or "") + "\n" + (p.text or "")
    html_l = (p.html or "").lower()
    proofs, possible = [], 1
    if name_in_text(text, row.name):
        proofs.append("name")
    ph = re.sub(r"\D", "", row.phone or "")[-9:]
    if len(ph) >= 9:
        possible += 1
        if ph in re.sub(r"\D", "", text + " " + " ".join(re.findall(r"tel:([^\"'>]+)", html_l))):
            proofs.append("phone")
    em = (row.email or "").strip().lower()
    if "@" in em:
        possible += 1
        if em in html_l:
            proofs.append("email")
    m = _PC.search(row.address or "")
    if m:
        possible += 1
        pc = (m.group(1) + m.group(2)).upper()
        if pc in re.sub(r"\s+", "", text).upper():
            proofs.append("postcode")
    ok = "name" in proofs and len(proofs) >= min(need, possible)
    return ok, proofs


def identity_hit(line, row):
    """does this single line carry the trader's own name, phone, email or postcode?"""
    if name_in_text(line, row.name) and len(line) < 400:
        return True
    ph = re.sub(r"\D", "", row.phone or "")[-9:]
    if len(ph) >= 9 and ph in re.sub(r"\D", "", line):
        return True
    em = (row.email or "").strip().lower()
    if "@" in em and em in line.lower():
        return True
    m = _PC.search(row.address or "")
    return bool(m and (m.group(1) + m.group(2)).upper() in re.sub(r"\s+", "", line).upper())
