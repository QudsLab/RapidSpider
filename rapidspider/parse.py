"""parse.py - html -> title / text / links / contacts, plus url helpers."""
import re
from urllib.parse import urljoin, urlparse, urldefrag

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)   # sitemaps/feeds are read with the html parser on purpose

try:
    import lxml  # noqa: F401
    _PARSER = "lxml"
except Exception:  # pragma: no cover
    _PARSER = "html.parser"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\(?0)\d[\d\s()\-]{8,14}\d")
JUNK_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")


HARD_DROP = {"script", "style", "noscript", "svg", "template", "iframe", "img", "video", "audio", "canvas",
             "button", "input", "select", "textarea", "head"}


def soup_of(html):
    return BeautifulSoup(html or "", _PARSER)


def norm_url(u):
    """add scheme, strip fragment; None when not usable."""
    u = (u or "").strip()
    if not u or u.lower() in ("null", "none", "n/a", "-", "0"):
        return None
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", u, re.I):
        u = "https://" + u.lstrip("/")
    u, _ = urldefrag(u)
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname or ("." not in p.hostname and p.hostname != "localhost"):
        return None
    return u


def host_of(u):
    try:
        return (urlparse(u).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def same_site(a, b):
    return host_of(a) == host_of(b)


def title_of(soup):
    t = soup.title.string if soup.title and soup.title.string else ""
    return re.sub(r"\s+", " ", t).strip()[:200]


def all_text(soup):
    """everything visible except script/style - used for classification."""
    s = BeautifulSoup(str(soup), _PARSER)
    for t in s(["script", "style", "noscript", "template"]):
        t.decompose()
    txt = s.get_text("\n")
    return re.sub(r"[ \t\r\f\v]+", " ", txt)


def clean_text(html, cfg):
    """content text: drop furniture selectors, consent noise, empty lines."""
    s = soup_of(html)
    for sel in cfg.parse.drop_selectors:
        try:
            for t in s.select(sel):
                # a cookie/consent class on <body> or on a page-wide wrapper must not wipe the whole page
                if t.name in ("html", "body") or (sel not in HARD_DROP and words(t.get_text(" ")) > cfg.parse.furniture_max_words):
                    continue
                t.decompose()
        except Exception:
            continue
    noise = [re.compile(p, re.I) for p in cfg.parse.drop_line_patterns]
    out = []
    for ln in s.get_text("\n").split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln or any(n.search(ln) for n in noise):
            continue
        out.append(ln)
    return "\n".join(out)


def words(text):
    return len(re.findall(r"\w+", text or ""))


def links_of(soup, base):
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if not h or h.startswith(("mailto:", "tel:", "javascript:", "#", "sms:")):
            continue
        u, _ = urldefrag(urljoin(base, h))
        if u in seen:
            continue
        seen.add(u)
        out.append((u, re.sub(r"\s+", " ", a.get_text(" ")).strip()[:80]))
    return out


def contacts_of(html):
    s = soup_of(html)
    emails, phones = set(), set()
    for a in s.find_all("a", href=True):
        h = a["href"].strip()
        if h.lower().startswith("mailto:"):
            emails.add(h[7:].split("?")[0].strip().lower())
        elif h.lower().startswith("tel:"):
            phones.add(re.sub(r"[^\d+]", "", h[4:]))
    txt = s.get_text(" ")
    for m in EMAIL_RE.findall(txt):
        if not m.lower().endswith(JUNK_EMAIL_EXT):
            emails.add(m.lower())
    for m in PHONE_RE.findall(txt):
        d = re.sub(r"[^\d+]", "", m)
        if 10 <= len(d) <= 14:
            phones.add(d)
    return sorted(e for e in emails if e), sorted(p for p in phones if p)


def sitemap_urls(xml):
    """(page_urls, child_sitemaps) from a sitemap / sitemap index body."""
    locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", xml or "", re.I)
    if re.search(r"<sitemapindex", xml or "", re.I):
        return [], locs
    return locs, []


def robots_sitemaps(txt):
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt or "")


def shingles(text, n=4):
    w = re.findall(r"\w+", (text or "").lower())
    if len(w) < 8:
        return None
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def near_dup(sh, kept, threshold):
    """is this paragraph (shingle set) >= threshold similar to any paragraph already kept?"""
    for other in kept[-400:]:
        inter = len(sh & other)
        if inter and inter / len(sh | other) >= threshold:
            return True
    return False
