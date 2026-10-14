"""logos.py - hunt logo candidates on the home page, let laya pick the real ones, save those.

Port of fatcher2's logoExtractor: candidates come from <link rel=icon>, JSON-LD
Organization.logo, og:image, <img> inside links (esp. home link / header) and inline <svg>.
Heuristic score ranks them; laya (logos.laya_filter) decides which are logos; only
those are downloaded to  <logos_dir>/<site id>/image_NN.<ext>.
"""
import json
import os
import re
from urllib.parse import urljoin, urlparse

from .parse import host_of, soup_of

IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif")
LOGO_RE = re.compile(r"logo|brand", re.I)
HDR_WORDS = ("header", "navbar", "site-branding", "site-logo", "main-nav", "primary-nav", "masthead", "topbar")


def in_header(el):
    """inside <header>/<nav> or an ancestor whose class/id says header/nav."""
    for p in el.parents:
        if getattr(p, "name", None) in ("header", "nav"):
            return True
        attrs = " ".join((p.get("class") or []) + [p.get("id") or ""]).lower() if hasattr(p, "get") else ""
        if any(w in attrs for w in HDR_WORDS):
            return True
    return False
CT_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
          "image/svg+xml": ".svg", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico", "image/avif": ".avif"}


def _is_img(url):
    p = urlparse(url).path.lower()
    return p.endswith(IMG_EXTS) or "logo" in p


def _is_home_href(href, home):
    h = (href or "").strip()
    if h in ("", "/", "#"):
        return True
    r = urljoin(home, h)
    return r.rstrip("/") == home.rstrip("/") or urlparse(r).path in ("", "/")


def candidates(html, home, cfg):
    s = soup_of(html)
    out, seen = [], set()

    def add(src, source, score, alt="", where="", svg=None):
        key = src or (svg or "")[:80]
        if key in seen:
            return
        seen.add(key)
        out.append({"src": src, "source": source, "score": score, "alt": alt[:200], "where": where, "svg": svg})

    for l in s.select("link[rel~=icon], link[rel='shortcut icon']"):
        u = urljoin(home, (l.get("href") or "").strip())
        if u and _is_img(u):
            add(u, "favicon", 30, where="head")

    for sc in s.select("script[type='application/ld+json']"):
        try:
            data = json.loads(sc.string or "{}")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            lg = item.get("logo")
            u = lg if isinstance(lg, str) else (lg.get("url") if isinstance(lg, dict) else None)
            if u:
                u = urljoin(home, u)
                if _is_img(u):
                    add(u, "jsonld", 80, where="schema.org")

    og = s.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        u = urljoin(home, og["content"].strip())
        if _is_img(u):
            add(u, "og:image", 20, where="meta")

    for img in s.find_all("img"):
        raw = (img.get("src") or img.get("data-src") or "").strip()
        if not raw or raw.startswith("data:"):
            continue
        u = urljoin(home, raw)
        if not _is_img(u):
            continue
        sc, in_hdr, home_link = 0, in_header(img), False
        a = img.find_parent("a")
        if a is not None:
            href = (a.get("href") or "").strip()
            if _is_home_href(href, home):
                sc += 90
                home_link = True
            elif host_of(urljoin(home, href)) != host_of(home):
                continue
        cls = " ".join(img.get("class") or [])
        if LOGO_RE.search(cls):
            sc += 40
        if LOGO_RE.search(img.get("id") or ""):
            sc += 30
        alt = (img.get("alt") or "").strip()
        if LOGO_RE.search(alt):
            sc += 20
        if LOGO_RE.search(raw):
            sc += 15
        try:
            w, h = int(img.get("width") or 0), int(img.get("height") or 0)
            if w and h and (w < 40 or h < 40):
                continue
        except ValueError:
            pass
        if in_hdr:
            sc += 25
        if sc > 0:
            add(u, "img", sc, alt, "header link" if home_link and in_hdr else ("home link" if home_link else
                                                                                 ("header" if in_hdr else "body")))

    for svg in s.find_all("svg"):
        a = svg.find_parent("a")
        if a is None:
            continue
        href = (a.get("href") or "").strip()
        is_home = _is_home_href(href, home)
        is_logo = bool(LOGO_RE.search(" ".join(a.get("class") or []) + " " + (a.get("id") or "")))
        if not (is_home or is_logo):
            continue
        body = str(svg)
        if len(body) < 200:
            continue
        sc = 70 + (30 if is_home else 0) + (40 if is_logo else 0) + (25 if in_header(svg) else 0)
        add("", "inline-svg", sc, where="header link" if in_header(svg) else "link", svg=body)

    out = [c for c in out if c["score"] >= cfg.logos.min_score]
    out.sort(key=lambda c: -c["score"])
    return out[: cfg.logos.max_candidates]


def _rel(p, cfg):
    return os.path.relpath(p, cfg.output.root).replace(os.sep, "/")


def _ext(url, ctype):
    p = urlparse(url).path.lower()
    for e in IMG_EXTS:
        if p.endswith(e):
            return e
    for ct, e in CT_EXT.items():
        if ct in ctype:
            return e
    return ".png"


async def hunt(site_id, site_name, home_url, html, cfg, fetcher, laya, tor=False):
    """-> list of dict rows for the logos table."""
    cands = candidates(html, home_url, cfg)
    if not cands:
        return []
    if cfg.logos.laya_filter:
        verdicts = await laya.logo_verdicts(cands, site_name or host_of(home_url))
    else:
        verdicts = [1.0] * len(cands)

    d = os.path.join(cfg.output.root, "logos", str(site_id))
    rows, n = [], 0
    for c, v in zip(cands, verdicts):
        row = {"site_id": site_id, "url": c["src"], "source": c["source"], "score": c["score"],
               "laya_score": round(v, 3), "is_logo": int(v > 0.5), "alt": c["alt"], "placement": c["where"],
               "local_path": "", "size": 0, "content_type": "", "error": ""}
        if v > 0.5:
            os.makedirs(d, exist_ok=True)
            if c["svg"]:
                p = os.path.join(d, f"image_{n:02d}.svg")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(c["svg"])
                row.update(local_path=_rel(p, cfg), size=os.path.getsize(p), content_type="image/svg+xml")
                n += 1
            else:
                data, ct, err = await fetcher.download(c["src"], cfg.logos.max_logo_bytes,
                                                       cfg.logos.download_timeout_sec, tor=tor)
                if data is None:
                    row["error"] = err
                else:
                    p = os.path.join(d, f"image_{n:02d}{_ext(c['src'], ct)}")
                    with open(p, "wb") as f:
                        f.write(data)
                    row.update(local_path=_rel(p, cfg), size=len(data), content_type=ct)
                    n += 1
        rows.append(row)
    return rows
