"""fetch.py - getting pages: plain http and Playwright browser, direct or through Tor."""
import asyncio
import re
import socket
import sys
import time
from dataclasses import dataclass

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
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), 8)
        return True, ""
    except socket.gaierror as e:
        return False, f"dns:{e.errno}"
    except asyncio.TimeoutError:
        return False, "dns:timeout"
    except Exception as e:
        return False, f"dns:{type(e).__name__}"


# ------------------------------------------------------------------------- sessions
class HttpSession:
    kind = "http"

    def __init__(self, client, cfg):
        self.client, self.cfg = client, cfg

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
        pass


class BrowserSession:
    kind = "browser"

    def __init__(self, ctx, cfg):
        self.ctx, self.cfg = ctx, cfg
        self.page = None

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
        try:
            html = await pg.content()
        except Exception as e:
            return Page(url=pg.url, error=f"content:{type(e).__name__}")
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
            await self.ctx.close()
        except Exception:
            pass


# ------------------------------------------------------------------------- factory
class Fetcher:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self._pw = None
        self._browsers = {}
        self._lock = asyncio.Lock()
        self.tor = cfg.network.tor_socks
        headers = {"User-Agent": cfg.fetch.user_agent, "Accept-Language": "en-GB,en;q=0.9",
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        kw = dict(follow_redirects=True, headers=headers, verify=False,
                  limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        self.http_direct = httpx.AsyncClient(**kw)
        self.http_tor = None
        if self.tor:
            self.http_tor = httpx.AsyncClient(proxy=self.tor.replace("socks5://", "socks5h://"), **kw)

    def has_rung(self, name):
        kind, headed, tor = RUNGS[name]
        return not (tor and not self.tor)

    async def _browser(self, headed, tor):
        key = (headed, tor)
        async with self._lock:
            if key in self._browsers:
                return self._browsers[key]
            if self._pw is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
            if headed:
                args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
            launch = dict(headless=not headed, args=args)
            if tor:
                launch["proxy"] = {"server": self.tor}
            try:
                b = await self._pw.chromium.launch(**launch)
            except Exception as e:
                if "playwright install" not in str(e) and "Executable doesn't exist" not in str(e):
                    raise
                # first run on this PC: fetch the browser with the same python (the `playwright` command is
                # often not on PATH when pip installs into a custom target dir)
                self.log.info("chromium missing - running: %s -m playwright install chromium", sys.executable)
                proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "playwright", "install", "chromium")
                await proc.wait()
                b = await self._pw.chromium.launch(**launch)
            self._browsers[key] = b
            return b

    async def session(self, rung):
        kind, headed, tor = RUNGS[rung]
        if tor and not self.tor:
            raise RuntimeError("tor rung but network.tor_socks is not set")
        if kind == "http":
            return HttpSession(self.http_tor if tor else self.http_direct, self.cfg)
        b = await self._browser(headed, tor)
        ctx = await b.new_context(
            user_agent=self.cfg.fetch.user_agent, locale="en-GB", ignore_https_errors=True,
            viewport={"width": 1280, "height": 900})
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        block = set(self.cfg.fetch.block_resources or [])
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
                await b.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
