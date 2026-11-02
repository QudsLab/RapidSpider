"""fetch.py - getting pages: plain http and Playwright browser, direct or through Tor."""
import asyncio
import random
import re
import re
import socket
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .parse import all_text, soup_of, title_of

RUNGS = {
    "http": ("http", False, False),
    "http_tor": ("http", False, True),
    "browser": ("browser", False, False),
    "browser_headed": ("browser", True, False),
    "browser_tor": ("browser", False, True),
    "browser_headed_tor": ("browser", True, True),
}


@dataclass
class Page:
    url: str
    status: int = 0
    html: str = ""
    text: str = ""
    title: str = ""
    error: str = ""
    scripts: int = 0
    ctype: str = ""
    ms: int = 0


def make_page(url, status, html, error="", ctype="", ms=0):
    html = html or ""
    if not html:
        return Page(url=url, status=status, error=error, ctype=ctype, ms=ms)
    soup = soup_of(html)
    return Page(url=url, status=status, html=html, text=all_text(soup), title=title_of(soup),
                error=error, scripts=len(soup.find_all("script")), ctype=ctype, ms=ms)


async def resolves(host):
    """-> (ok, why). A temporary failure (try again / timeout) is asked again 3 times before it counts."""
    loop = asyncio.get_running_loop()
    why = ""
    for i in range(3):
        try:
            await asyncio.wait_for(loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), 8)
            return True, ""
        except socket.gaierror as e:
            why = f"dns:{e.errno}"
            if e.errno not in (11002, -3, 2):            # 11001 / NXDOMAIN style = the name does not exist: final
                return False, why
        except asyncio.TimeoutError:
            why = "dns:timeout"
        except Exception as e:
            return False, f"dns:{type(e).__name__}"
        await asyncio.sleep(2 + i)
    return False, why + " (temporary, not final)"


# ------------------------------------------------------------------------- sessions
class HttpSession:
    kind = "http"

    def __init__(self, client, cfg, own=False):
        self.client, self.cfg, self.own = client, cfg, own

    async def get(self, url, deadline=None):
        t0 = time.time()
        to = self.cfg.fetch.timeout_sec
        if deadline is not None:
            to = max(3, min(to, deadline - time.time()))
        try:
            async with self.client.stream("GET", url, timeout=to) as r:
                ctype = r.headers.get("content-type", "").lower()
                buf, size = [], 0
                async for chunk in r.aiter_bytes():
                    buf.append(chunk)
                    size += len(chunk)
                    if size >= self.cfg.crawl.max_page_bytes:
                        break
                raw = b"".join(buf)
                if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                    return Page(url=str(r.url), status=r.status_code, error="non_html", ctype=ctype,
                                ms=int((time.time() - t0) * 1000))
                enc = r.encoding or "utf-8"
                try:
                    html = raw.decode(enc, errors="replace")
                except LookupError:
                    html = raw.decode("utf-8", errors="replace")
                return make_page(str(r.url), r.status_code, html, ctype=ctype, ms=int((time.time() - t0) * 1000))
        except httpx.TimeoutException:
            return Page(url=url, error="timeout", ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return Page(url=url, error=f"{type(e).__name__}: {str(e)[:120]}", ms=int((time.time() - t0) * 1000))

    async def close(self):
        if self.own:
            await self.client.aclose()


class BrowserSession:
    kind = "browser"

    def __init__(self, ctx, cfg):
        self.ctx, self.cfg = ctx, cfg
        self.page = None
        self.mx, self.my = 200.0, 200.0         # where our "mouse" is, so every move starts from there
        self.clicks = 0

    async def _page(self):
        if self.page is None or self.page.is_closed():
            self.page = await self.ctx.new_page()
        return self.page

    async def get(self, url, deadline=None):
        t0 = time.time()
        to = self.cfg.fetch.timeout_sec * 1000
        if deadline is not None:
            to = max(4000, min(to, (deadline - time.time()) * 1000))
        try:
            pg = await self._page()
            resp = await pg.goto(url, wait_until="domcontentloaded", timeout=to)
            try:
                await pg.wait_for_load_state("load", timeout=min(8000, to))
            except Exception:
                pass
            await self._scroll(pg)
            status = resp.status if resp else 0
            return await self.snapshot(status, t0)
        except Exception as e:
            return Page(url=url, error=f"{type(e).__name__}: {str(e)[:120]}", ms=int((time.time() - t0) * 1000))

    async def _scroll(self, pg):
        try:
            await pg.evaluate("async()=>{for(let i=0;i<4;i++){window.scrollBy(0,innerHeight);"
                              "await new Promise(r=>setTimeout(r,150));}window.scrollTo(0,0);}")
        except Exception:
            pass

    async def snapshot(self, status=0, t0=None):
        pg = await self._page()
        html, err = None, None
        for i in range(5):                                   # a shield page navigates / redirects: content() fails while it does
            try:
                html = await pg.content()
                break
            except Exception as e:
                err = e
                try:
                    await pg.wait_for_load_state("domcontentloaded", timeout=4000)
                except Exception:
                    pass
                await asyncio.sleep(1.0 + i * 0.5)
        if html is None:
            return Page(url=pg.url, error=f"content:{type(err).__name__}")
        return make_page(pg.url, status, html[: self.cfg.crawl.max_page_bytes],
                         ms=int((time.time() - (t0 or time.time())) * 1000))

    async def poke(self):
        """look alive while waiting: mouse move + scroll."""
        pg = await self._page()
        try:
            await pg.mouse.move(120 + int(time.time() * 7) % 300, 160 + int(time.time() * 13) % 200)
            await pg.mouse.wheel(0, 200)
        except Exception:
            pass

    # ---- human-like interaction for challenge / shield pages ------------------------------------
    async def _move(self, pg, x, y, fast=False):
        """human mouse path: 1-3 curved segments with uneven speed, a small overshoot-and-correct, hand tremor."""
        pts = [(x, y)]
        far = abs(x - self.mx) + abs(y - self.my) > 250
        if far and random.random() < 0.6:                              # a person rarely goes in one clean arc
            pts.insert(0, (self.mx + (x - self.mx) * random.uniform(.4, .7) + random.uniform(-120, 120),
                           self.my + (y - self.my) * random.uniform(.4, .7) + random.uniform(-90, 90)))
        if not fast and random.random() < 0.35:                        # overshoot, then come back
            pts.insert(len(pts) - 1, (x + random.uniform(8, 26) * random.choice((-1, 1)), y + random.uniform(6, 18) * random.choice((-1, 1))))
        for tx, ty in pts:
            sx, sy = self.mx, self.my
            dist = ((tx - sx) ** 2 + (ty - sy) ** 2) ** .5
            cx = (sx + tx) / 2 + random.uniform(-.15, .15) * dist
            cy = (sy + ty) / 2 + random.uniform(-.15, .15) * dist
            steps = max(8, min(45, int(dist / 12) + random.randint(4, 10)))
            for i in range(1, steps + 1):
                u = i / steps
                u = u * u * (3 - 2 * u)                                # ease in / out
                px = (1 - u) ** 2 * sx + 2 * (1 - u) * u * cx + u * u * tx
                py = (1 - u) ** 2 * sy + 2 * (1 - u) * u * cy + u * u * ty
                tr = 0 if i == steps else 1.2
                await pg.mouse.move(px + random.uniform(-tr, tr), py + random.uniform(-tr, tr))
                await asyncio.sleep(random.uniform(0.004, 0.022) * (0.5 if fast else 1))
            self.mx, self.my = tx, ty
            if random.random() < 0.25:
                await asyncio.sleep(random.uniform(0.04, 0.2))

    async def _click(self, pg, x, y):
        await self._move(pg, x, y)
        for _ in range(random.randint(1, 3)):                          # hover: tiny settle movements on the target
            await self._move(pg, x + random.uniform(-2, 2), y + random.uniform(-2, 2), fast=True)
            await asyncio.sleep(random.uniform(0.08, 0.25))
        await asyncio.sleep(random.uniform(0.2, 0.6))
        await pg.mouse.down()
        await asyncio.sleep(random.uniform(0.06, 0.16))
        await pg.mouse.up()
        self.clicks += 1
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await self.wander(pg)                                          # and then leave the button, like a person does

    async def wander(self, pg=None):
        """idle behaviour: drift the mouse somewhere else, sometimes scroll a bit, sometimes just pause."""
        pg = pg or await self._page()
        try:
            vp = pg.viewport_size or {"width": 1200, "height": 800}
            if random.random() < 0.85:
                await self._move(pg, random.uniform(vp["width"] * .15, vp["width"] * .85),
                                 random.uniform(vp["height"] * .15, vp["height"] * .85))
            if random.random() < 0.4:
                await pg.mouse.wheel(0, random.choice((-1, 1)) * random.randint(40, 240))
            await asyncio.sleep(random.uniform(0.1, 0.6))
        except Exception:
            pass

    async def poke(self):
        await self.wander()

    _SHIELD_JS = """() => [...document.querySelectorAll(
        'input[type=checkbox],button,[role=button],[role=checkbox],input[type=submit],input[type=button],a.btn,.btn,label')]
        .map(e => {const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
          return {t: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 80), tag: e.tagName,
                  type: e.type || '', x: r.x, y: r.y, w: r.width, h: r.height,
                  vis: cs.visibility !== 'hidden' && cs.display !== 'none' && r.width > 8 && r.height > 8}})
        .filter(o => o.vis && o.y >= 0 && o.y < innerHeight && o.x >= 0 && o.x < innerWidth)"""
    _WANT = re.compile(r"verify|human|robot|continue|proceed|i am|i'm|click|check|confirm|submit|enter|allow", re.I)
    _SKIP = re.compile(r"cookie|accept all|reject|subscribe|newsletter|privacy|settings|preferences|decline", re.I)
    _FRAMES = ("challenges.cloudflare.com", "turnstile", "hcaptcha.com", "recaptcha")

    async def _challenge_box(self, pg):
        for fr in pg.frames:
            if any(k in (fr.url or "") for k in self._FRAMES):
                try:
                    box = await (await fr.frame_element()).bounding_box()
                except Exception:
                    continue
                if box and box["width"] > 40 and box["height"] > 25:
                    return box
        return None

    async def _buttons(self, pg):
        try:
            items = await pg.evaluate(self._SHIELD_JS)
        except Exception:
            return []
        return [o for o in items if not self._SKIP.search(o["t"]) and (o["type"] == "checkbox" or self._WANT.search(o["t"]))]

    async def probe(self):
        """what can be done on this page right now -> {'challenge': bool, 'buttons': [labels]}"""
        pg = await self._page()
        return {"challenge": bool(await self._challenge_box(pg)), "buttons": [o["t"] or o["type"] for o in await self._buttons(pg)][:5]}

    async def act(self, name):
        """one human-like action chosen by the decision engine: wander | scroll | click_challenge | click_button | reload | reset"""
        pg = await self._page()
        try:
            if name == "wander":
                await self.wander(pg)
            elif name == "scroll":
                await pg.mouse.wheel(0, random.randint(150, 500)); await asyncio.sleep(random.uniform(.3, .9))
                await pg.mouse.wheel(0, -random.randint(100, 400)); await self.wander(pg)
            elif name == "click_challenge":
                await self.wander(pg)                                  # arrive from somewhere, not from nowhere
                box = await self._challenge_box(pg)
                if box:
                    await self._click(pg, box["x"] + min(30.0, box["width"] * 0.12) + random.uniform(-3, 3),
                                      box["y"] + box["height"] / 2 + random.uniform(-4, 4))
            elif name == "click_button":
                c = await self._buttons(pg)
                if c:
                    o = sorted(c, key=lambda o: (o["type"] != "checkbox", o["y"]))[0]
                    await self._click(pg, o["x"] + o["w"] * random.uniform(0.3, 0.7), o["y"] + o["h"] * random.uniform(0.35, 0.65))
            elif name == "reload":
                await self.reload()
            elif name == "reset":
                await self.clear_cookies("all"); await self.reload()
        except Exception:
            pass

    async def clear_cookies(self, which):
        try:
            if which == "all" or which is True:
                await self.ctx.clear_cookies()
            else:
                for n in which or []:
                    await self.ctx.clear_cookies(name=n)
        except Exception:
            pass

    async def reload(self):
        pg = await self._page()
        try:
            await pg.reload(wait_until="domcontentloaded", timeout=self.cfg.fetch.timeout_sec * 1000)
        except Exception:
            pass

    async def close(self):
        try:
            if getattr(self, "shared", False):          # real chrome: close only our tab, the profile / cookies stay
                if self.page and not self.page.is_closed():
                    await self.page.close()
            else:
                await self.ctx.close()
        except Exception:
            pass


# ------------------------------------------------------------------------- factory
class Fetcher:
    def __init__(self, cfg, log, route=None):
        self.cfg, self.log = cfg, log
        self._pw = None
        self._browsers = {}
        self._procs = []
        self._lock = asyncio.Lock()
        self.tor = route or cfg.network.tor_socks            # route = local gateway url (proxies.py): tor UK + proxy pool
        self.rotate = bool(cfg.network.tor_rotate) and not route
        self.gateway = bool(route)    # the gateway rotates exits itself
        headers = {"User-Agent": cfg.fetch.user_agent, "Accept-Language": "en-GB,en;q=0.9",
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        kw = dict(follow_redirects=True, headers=headers, verify=False,
                  limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        self._kw = kw
        self.http_direct = httpx.AsyncClient(**kw)
        self.http_tor = None
        if self.tor:
            self.http_tor = httpx.AsyncClient(proxy=self.tor.replace("socks5://", "socks5h://"), **kw)

    rotate_host = None       # set by main.py: (host) -> None, the next connection to that host leaves from another exit
    route_ok = None          # set by main.py: () -> bool, False while no tor / proxy is usable

    async def wait_route(self):
        n = 0
        while self.route_ok and not self.route_ok():
            if n % 12 == 0:
                self.log.warning("no working UK route (tor / proxy) - waiting, no site is started")
            n += 1
            await asyncio.sleep(5)

    reset_host = None        # set by main.py: (host) -> None, a site starts again: first route level

    def new_exit(self, url, climb=True):
        """next request to this site: another exit, and (climb) one route level on."""
        if self.rotate_host:
            try:
                self.rotate_host(urlparse(url if "//" in url else "//" + url).hostname or "", climb)
            except Exception:
                pass

    def reset_exit(self, url):
        if self.reset_host:
            try:
                self.reset_host(urlparse(url if "//" in url else "//" + url).hostname or "")
            except Exception:
                pass

    def has_rung(self, name):
        kind, headed, tor = RUNGS[name]
        return not (tor and not self.tor)

    def _tor_proxy(self, key):
        """tor circuit per site: tor gives every distinct socks username its own circuit (and exit IP)."""
        if not (self.rotate and key is not None):
            return {"server": self.tor}
        u = f"rs{key}x{random.randint(0, 10**6)}"
        return {"server": self.tor, "username": u, "password": "x"}

    def _chrome_exe(self):
        import os, shutil
        c = (self.cfg.fetch.chrome_path or "").strip()
        if c and os.path.exists(c):
            return c
        pf = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for base in pf:
            q = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if base and os.path.exists(q):
                return q
        for n in ("google-chrome", "google-chrome-stable", "chrome", "chromium"):
            w = shutil.which(n)
            if w:
                return w
        if os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
            return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        raise RuntimeError("engine chrome: Chrome not found - set fetch.chrome_path")

    async def _real_chrome(self, tor):
        """the person's own Chrome, started normally (no automation switches, a persistent profile that keeps the
        cloudflare pass cookies) and only then connected to over the debugging port."""
        import os
        port = int(self.cfg.fetch.chrome_port) + (1 if tor else 0)
        prof = os.path.abspath(os.path.join(self.cfg.fetch.chrome_profile or os.path.join(self.cfg.output.root, "chrome_profile"),
                                            "tor" if tor else "direct"))
        url = f"http://127.0.0.1:{port}"
        async def up():
            try:
                r = await self.http_direct.get(url + "/json/version", timeout=2)
                return r.status_code == 200
            except Exception:
                return False
        if not await up():
            os.makedirs(prof, exist_ok=True)
            args = [self._chrome_exe(), f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "--no-first-run",
                    "--no-default-browser-check", "--window-size=1280,900", "--lang=en-GB"]
            args += list(self.cfg.fetch.get("chrome_args") or [])
            if tor:
                args.append(f"--proxy-server={self.tor}")
            self.log.info("starting your Chrome (%s) with a persistent profile: %s", "tor" if tor else "direct", prof)
            self._procs.append(await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
            for _ in range(40):
                await asyncio.sleep(0.5)
                if await up():
                    break
        try:
            from patchright.async_api import async_playwright
        except Exception:
            from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
        return await self._pw.chromium.connect_over_cdp(url)

    async def _browser(self, headed, tor):
        engine = (self.cfg.fetch.engine or "playwright").lower()
        key = (headed, tor and engine != "camoufox")             # camoufox sets the tor proxy per context, so one browser serves all
        if engine == "chrome":
            key = ("chrome", tor)
        async with self._lock:
            if key in self._browsers:
                return self._browsers[key]
            if engine == "chrome":
                b = await self._real_chrome(tor)
                self._browsers[key] = b
                return b
            if engine == "camoufox":
                from camoufox.async_api import AsyncCamoufox
                cm = AsyncCamoufox(headless=not headed, humanize=False, os=("windows", "macos"), geoip=False)
                b = await cm.__aenter__()
                b._cm = cm
                self._browsers[key] = b
                return b
            if self._pw is None:
                if engine == "patchright":
                    from patchright.async_api import async_playwright
                else:
                    from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            if engine == "patchright":                            # patchright hides the automation leaks itself; extra flags would add new ones
                args = ["--no-sandbox"]
            else:
                args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
            if headed:
                # off-screen window, but the page must still believe it is visible (Turnstile does not render otherwise)
                args += ["--window-position=-32000,-32000", "--window-size=1280,900",
                         "--disable-features=CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows",
                         "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]
            launch = dict(headless=not headed, args=args)
            if engine != "patchright":
                launch["ignore_default_args"] = ["--enable-automation"]
            if tor:
                launch["proxy"] = {"server": self.tor}        # chromium cannot send socks credentials: one shared circuit
            chan = (self.cfg.fetch.browser_channel or "").strip()
            if chan:                                   # the browser installed on this PC passes challenges far more often
                try:
                    b = await self._pw.chromium.launch(channel=chan, **launch)
                    self._browsers[key] = b
                    return b
                except Exception as e:
                    self.log.info("browser channel '%s' not usable (%s) - using the bundled chromium", chan, str(e)[:80])
            try:
                b = await self._pw.chromium.launch(**launch)
            except Exception as e:
                if "playwright install" not in str(e) and "Executable doesn't exist" not in str(e):
                    raise
                # first run on this PC: fetch the browser with the same python (the `playwright` command is
                # often not on PATH when pip installs into a custom target dir)
                mod = "patchright" if engine == "patchright" else "playwright"
                self.log.info("chromium missing - running: %s -m %s install chromium", sys.executable, mod)
                proc = await asyncio.create_subprocess_exec(sys.executable, "-m", mod, "install", "chromium")
                await proc.wait()
                b = await self._pw.chromium.launch(**launch)
            self._browsers[key] = b
            return b

    async def session(self, rung, key=None):
        kind, headed, tor = RUNGS[rung]
        if tor and not self.tor:
            raise RuntimeError("tor rung but network.tor_socks is not set")
        engine = (self.cfg.fetch.engine or "playwright").lower()
        if kind == "http":
            if tor and self.rotate and key is not None:
                px = self._tor_proxy(key)
                url = px["server"].replace("socks5://", "socks5h://").replace("://", f"://{px['username']}:x@", 1)
                c = httpx.AsyncClient(proxy=url, **self._kw)
                return HttpSession(c, self.cfg, own=True)
            return HttpSession(self.http_tor if tor else self.http_direct, self.cfg)
        b = await self._browser(headed, tor)
        if engine == "chrome":
            sess = BrowserSession(b.contexts[0], self.cfg)
            sess.shared = True
            return sess
        opts = dict(locale="en-GB", ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        if not headed and engine == "playwright":       # a visible browser keeps its own real user agent; patchright / camoufox must not fake one
            opts["user_agent"] = self.cfg.fetch.user_agent
        if tor and engine == "camoufox":
            opts["proxy"] = self._tor_proxy(key)
        ctx = await b.new_context(**opts)
        if engine == "playwright":
            await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        block = set(self.cfg.fetch.block_resources or []) if not headed else set()   # the visible rung loads everything
        if block:
            async def _route(route):
                if route.request.resource_type in block:
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", _route)
        return BrowserSession(ctx, self.cfg)

    async def aux_text(self, url, tor=False):
        """robots.txt / sitemap bodies. '' on any failure."""
        c = self.http_tor if (tor and self.http_tor) else self.http_direct
        try:
            r = await c.get(url, timeout=12)
            if r.status_code == 200:
                return r.text[:2_000_000]
        except Exception:
            pass
        return ""

    async def download(self, url, max_bytes, timeout, tor=False):
        """-> (bytes|None, content_type, error)"""
        c = self.http_tor if (tor and self.http_tor) else self.http_direct
        try:
            async with c.stream("GET", url, timeout=timeout) as r:
                if r.status_code != 200:
                    return None, "", f"http_{r.status_code}"
                buf, n = [], 0
                async for ch in r.aiter_bytes():
                    buf.append(ch)
                    n += len(ch)
                    if n > max_bytes:
                        return None, "", "too_big"
                return b"".join(buf), r.headers.get("content-type", "").lower(), ""
        except Exception as e:
            return None, "", f"{type(e).__name__}"

    async def close(self):
        await self.http_direct.aclose()
        if self.http_tor:
            await self.http_tor.aclose()
        for b in self._browsers.values():
            try:
                cm = getattr(b, "_cm", None)
                if cm:
                    await cm.__aexit__(None, None, None)
                else:
                    await b.close()
            except Exception:
                pass
        for pr in self._procs:
            try:
                pr.terminate()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
