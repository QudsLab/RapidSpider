"""config.py - load config.yml, merge over built-in defaults, resolve paths."""
import copy
import os
import sys

import yaml

DEFAULTS = {
    "name": "rapidspider",
    "input": {
        "path": None,
        "format": "auto",            # auto | csv | sqlite | json
        "table": None,               # sqlite only
        "where": None,               # sqlite only: raw SQL filter
        "columns": {                 # canonical -> source column (str or list of str)
            "id": "id", "url": "url", "name": "name",
            "phone": "phone", "address": "address", "email": "email",
        },
        "extra_columns": [],         # carried into sites.extra as JSON
        "limit": 0,
    },
    "output": {
        "dir": None,                 # one folder per config; default: output/<name>
        "per_day": False,            # True -> <dir>/2026-10-13/ (fresh set of dbs every day)
        "max_db_mb": 45,             # every db file rolls to *_0002.db before it reaches this size
        "store_pages": True,         # pages table (one row per fetched page)
        "store_pages_text": False,   # also keep per-page text there (sites table already has all of it)
    },
    "run": {
        "threads": 3,
        "max_attempts": 3,
        "site_budget_sec": 240,
        "resume": True,
        "retry_statuses": ["error", "timeout", "laya_down"],
        "pause_between_sites_sec": 0.0,
    },
    "crawl": {
        "max_pages": 4,              # home + sub pages
        "use_sitemap": True,
        "max_page_bytes": 1500000,
        "max_page_chars": 40000,
        "max_site_chars": 120000,
        "min_page_words": 25,
        "min_site_words": 60,
        "prefer_paths": ["contact", "about", "service", "team", "who-we", "our-", "location", "find-us", "fees", "pricing"],
        "skip_ext": [".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".zip", ".doc", ".docx",
                     ".xls", ".xlsx", ".mp4", ".mp3", ".css", ".js", ".xml", ".json", ".txt"],
        "skip_paths": ["wp-admin", "wp-login", "/cart", "/basket", "/checkout", "/login", "/account", "/tag/",
                       "/feed", "/privacy", "/cookie", "/terms", "/sitemap"],
    },
    "fetch": {
        # escalation ladder, tried in order until a page is accepted.
        # rungs: http | browser | browser_headed | browser_tor | browser_headed_tor | http_tor
        "ladder": ["http", "browser", "browser_headed", "browser_tor"],
        "timeout_sec": 30,
        "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "block_resources": ["image", "media", "font"],   # browser: skip heavy resources
    },
    "network": {
        "tor_socks": None,           # e.g. socks5://127.0.0.1:9050 (Tor must already be running)
        "dead_confirm_tries": 3,     # DNS-dead domains are re-checked this many times via Tor
        "accept_unconfirmed_dead": False,
    },
    "laya": {                        # MANDATORY - runs INSIDE this app (no separate server), no off switch
        "device": "auto",            # auto | cpu | cuda
        "models": "auto",            # auto (cpu: english only, gpu: all) | english,multilingual,typed-decisions
        "torch_threads": 8,
        "max_ram_mb": 10000,         # RAM watchdog: unloads the least-used checkpoint above this
        "retries": 2,
        "summary": False,            # ask laya for a one-line site summary
        "confirm": {                 # a verdict that ENDS a site (dead / parked / third party / no content) must survive
            "rounds": 5,             # this many differently-worded, flipped re-questions
            "min_agree": 0.6,        # share of rounds that must agree, else the page is treated as a normal page
        },
        "log": {                     # EVERY laya question + answer goes to laya_calls_*.db
            "enabled": True,
            "text_chars": 400,       # judged text kept per row
            "full_prompt_tasks": ["page_real", "page_kind", "wait", "rank", "summary", "manual"],  # others keep only the few-shot labels
        },
        "examples": {                # few-shot: the examples nearest to each judged text are shown to laya
            "use_defaults": True,    # built-in generic examples (examples.py) + yours below
            "k": 3,                  # examples shown per question
            "max_chars": 170,
            "learned_file": "learned_examples.yml",   # your own corrections (hand-edited, next to config.yml)
            "auto_file": "learned_auto.yml",          # examples laya learns BY ITSELF from unanimous, verified decisions
            "auto_max_per_label": 120,
            "page_kind": [],         # [{label: domain_parked|hosting_placeholder|...|third_party_platform|none, text: ...}]
            "block": [],             # [{label: keep|drop, text: ...}]
            "logo": [],              # [{label: logo|not_logo, text: "file: ... | alt: ... | found as: ... | placement: ..."}]
            "wait": [],              # [{label: content|waiting_shell|challenge|permanent_error, text: ...}]
            "fact": [],              # [{label: <category key from info.yml> | none, text: ...}]
        },
    },
    "facts": {                       # business facts for descriptions / bullet points (categories live in info.yml)
        "enabled": True,
        "file": "info.yml",
        "max_per_category": 4,
        "scan": {
            "chunk_sentences": 8,    # numbered sentences laya reads at once: "which sentence states X?"
            "pick_min": 0.55,        # laya's confidence for a picked sentence to become a candidate
            "questions_per_pass": 24,  # ingredient questions sharing one forward pass
        },
        "max_blocks": 96,            # sentences read per site
        "block_min_words": 4,
        "block_max_words": 60,
        "ready_min_categories": 4,   # profile is "ready" for writing when this many categories have a fact
        "verify": {                  # every candidate fact is re-questioned by laya (no human review step)
            "rounds": 8,             # questions per fact, differently worded, answers flipped A/B to cancel position bias
            "max_candidates": 20,    # picked sentences verified per site (each is ONE laya pass of `rounds` questions)
            "accept_frac": 0.67,     # share of rounds that must agree -> auto_good
            "accept_mean": 0.6,      # and mean agreement probability
            "reject_frac": 0.34,     # at most this share agreeing -> auto_bad; in between -> auto_unsure (not in profile)
            "auto_teach": True,      # unanimous verdicts become laya examples (learned_auto.yml)
            "max_rejected_stored": 20,
        },
    },
    "ui": {                          # web interface: monitor, pause/resume, review data, laya Q&A
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8800,
        "keep_alive": True,          # stay up after the queue is empty (so you can review / requeue / rescan)
    },
    # What the engine does when it recognises each kind of page.
    # action: wait | escalate | stop | single_page | crawl
    "classes": {
        "ok":          {"action": "crawl"},
        "holding":     {"action": "wait", "max_wait_sec": 45, "poll_sec": 1.5,
                        "clear_cookies": "all", "clear_every_sec": 12, "extra_patterns": []},
        "cloudflare":  {"action": "wait", "max_wait_sec": 25, "poll_sec": 2.0,
                        "clear_cookies": [], "clear_every_sec": 0, "extra_patterns": []},
        "dead":        {"action": "stop", "status": "dead", "extra_patterns": []},
        "third_party": {"action": "single_page", "status": "third_party",
                        "signals": [], "signals_min": 5, "identity_min": 2, "follow_max": 3, "trader_min": 0.6},
        "error":       {"action": "escalate"},
        "js_shell":    {"action": "escalate"},
        "empty":       {"action": "escalate"},
    },
    "parse": {
        "drop_selectors": [
            "script", "style", "noscript", "svg", "template", "iframe", "img", "video", "audio", "canvas",
            "button", "input", "select", "textarea", "form", "nav", "footer", "header", "aside", "head",
            "[id*='cookie' i]", "[class*='cookie' i]", "[class*='cky' i]", "[class*='consent' i]",
            "[class*='hcaptcha' i]", "[aria-label*='cookie' i]", "[class*='breadcrumb' i]",
        ],
        "drop_line_patterns": [
            r"we value your privacy", r"reject all", r"accept all", r"cookie(s)? (policy|settings|preferences)",
            r"cookieyes", r"onetrust", r"manage consent",
        ],
        "keep_contacts_from_raw_html": True,
        "laya_block_filter": True,   # laya keep/drop on every text block that survived the selectors
        "block_min_words": 4,        # shorter lines are not judged (kept)
        "block_max_words": 150,
        "block_batch": 60,           # blocks per laya call
        "drop_confidence": 0.6,      # a block is only dropped when laya says drop with at least this confidence
        "max_drop_ratio": 0.8,       # if laya wants to drop more than this share of a page's words, ignore its drops
        "veto_min_words": 120,       # laya can only declare "no real content" for pages thinner than this
        "furniture_max_words": 250,  # a class-matched (cookie/consent/...) element bigger than this is content, kept
        "block_recheck_rounds": 2,   # laya "drop" calls that are not confident are re-asked this many times
        "near_dup": 0.7,             # drop paragraphs this similar (4-word shingles) to ones already kept on the site
    },
    "logos": {
        "enabled": False,
        "laya_filter": True,         # only candidates laya says "logo" are downloaded
        "max_candidates": 8,
        "min_score": 20,
        "max_logo_bytes": 2000000,
        "download_timeout_sec": 15,
    },
}


class Cfg(dict):
    """dict with attribute access (cfg.run.threads)."""
    def __getattr__(self, k):
        try:
            v = self[k]
        except KeyError:
            raise AttributeError(k)
        return v


def _wrap(o):
    if isinstance(o, dict):
        return Cfg({k: _wrap(v) for k, v in o.items()})
    return o


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        sys.exit(f"config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}
    cfg = _merge(copy.deepcopy(DEFAULTS), user)
    base = os.path.dirname(path)

    def rel(p):
        if not p:
            return p
        p = os.path.expandvars(os.path.expanduser(str(p)))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    for k in ("path",):
        cfg["input"][k] = rel(cfg["input"][k])
    out = cfg["output"]
    d = out.get("dir") or os.path.join("output", str(cfg.get("name") or "rapidspider"))
    d = rel(d)
    if out["per_day"]:
        from datetime import datetime
        d = os.path.join(d, datetime.now().strftime("%Y-%m-%d"))
    out["root"] = d
    cfg["facts"]["file"] = rel(cfg["facts"].get("file"))
    ex = cfg["laya"]["examples"]
    ex["learned_file"] = rel(ex.get("learned_file"))
    ex["auto_file"] = rel(ex.get("auto_file"))
    cfg["_base"] = base
    cfg["_path"] = path

    if cfg["facts"]["enabled"] and not (cfg["facts"]["file"] and os.path.isfile(cfg["facts"]["file"])):
        sys.exit(f"facts.file (info.yml) not found: {cfg['facts']['file']} - or set facts.enabled: false")
    if not cfg["input"]["path"] or not os.path.isfile(cfg["input"]["path"]):
        sys.exit(f"input.path missing or not a file: {cfg['input']['path']}")
    return _wrap(cfg)
