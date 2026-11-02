"""proxies.py - UK-only routing for the "tor" rungs.

    Tor (UK exits only)  +  a pool of free proxies built from downloaded lists  ->  one local gateway.

* Tor: if network.tor_exe is set, tor is started here with ExitNodes {gb} StrictNodes 1 and killed when we stop.
  An already running Tor is accepted only when its exit IPs are in the UK range list (network.tor_uk).
* Pool: network.proxy_pool.sources are downloaded every start, cut to UK IPs with an IP range list, then every
  proxy is tried `tries` times by `scan_threads` parallel workers (socks4, socks5, http / https-CONNECT).
  A proxy is kept only if it really works and its exit IP is in the UK range list.
* Gateway: a local http proxy (127.0.0.1:gateway_port). The fetcher (httpx and Chrome) is pointed at it, it sends
  every target host to one tor / pool exit (sticky for `sticky_sec`) and drops proxies that keep failing.
"""
import asyncio
import atexit
import bisect
import ipaddress
import os
import random
import re
import shutil
import platform
import socket
import struct
import tarfile
import time
from urllib.parse import urlparse

import httpx

IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
LINE_RE = re.compile(r"^\s*(?:(socks5h?|socks4a?|https?)://)?(?:[^@\s/]+@)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})")
SCHEME = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "socks4a": "socks4", "http": "http", "https": "http"}
AUTO = ["socks5", "socks4", "http"]


class ConnectFail(OSError):
    """could not even open the tcp connection to the proxy."""


class TargetFail(OSError):
    """the proxy works, the target site did not answer (not the proxy's fault)."""


# ------------------------------------------------------------------------- uk ip ranges
class Ranges:
    def __init__(self):
        self.starts, self.ends = [], []

    def load(self, text):
        rs = []
        for ln in text.splitlines():
            ln = ln.strip().split("#")[0].strip()
            if not ln:
                continue
            try:
                n = ipaddress.ip_network(ln.split()[0], strict=False)
            except ValueError:
                continue
            if n.version == 4:
                rs.append((int(n.network_address), int(n.broadcast_address)))
        rs.sort()
        for a, b in rs:
            if self.starts and a <= self.ends[-1] + 1:
                self.ends[-1] = max(self.ends[-1], b)
            else:
                self.starts.append(a)
                self.ends.append(b)
        return len(self.starts)

    def __contains__(self, ip):
        try:
            x = int(ipaddress.IPv4Address(ip))
        except ValueError:
            return False
        i = bisect.bisect_right(self.starts, x) - 1
        return i >= 0 and x <= self.ends[i]


def _client(timeout=40):
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})


async def load_ranges(pc, folder, log):
    """the UK range list: your own file (uk_ranges.file) or the downloaded one (cached in <output>/proxies)."""
    rc = pc.uk_ranges
    own = (rc.get("file") or "").strip()
    path = own or os.path.join(folder, "uk_ranges.txt")
    fresh = os.path.isfile(path) and (own or time.time() - os.path.getmtime(path) < float(rc.get("refresh_hours", 168)) * 3600)
    if not fresh:
        try:
            async with _client() as c:
                r = await c.get(rc.url)
                r.raise_for_status()
            with open(path, "w", encoding="utf-8") as f:
                f.write(r.text)
            log.info("uk ip ranges downloaded: %s", rc.url)
        except Exception as e:
            log.warning("uk ip ranges download failed (%s: %s)%s", type(e).__name__, e, " - using the old copy" if os.path.isfile(path) else "")
    if not os.path.isfile(path):
        raise RuntimeError("no UK ip range list - set network.proxy_pool.uk_ranges.url or .file")
    rg = Ranges()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        n = rg.load(f.read())
    if not n:
        raise RuntimeError(f"UK ip range list is empty: {path}")
    log.info("uk ip ranges: %d blocks", n)
    return rg


# ------------------------------------------------------------------------- proxy sources
def _guess_scheme(url):
    u = url.lower().rsplit("/", 1)[-1] + " " + url.lower()
    for k, v in (("socks5", "socks5"), ("socks4", "socks4"), ("https", "http"), ("http", "http")):
        if k in u:
            return v
    return "auto"


async def download_sources(pc, folder, log):
    """-> {(ip, port): scheme-or-auto}. A failed source falls back to its cached copy."""
    items = []
    for s in pc.sources:
        s = {"url": s} if isinstance(s, str) else dict(s)
        s["scheme"] = (s.get("scheme") or _guess_scheme(s["url"])).lower()
        items.append(s)

    async def one(c, i, s):
        cache = os.path.join(folder, f"source_{i}.txt")
        try:
            r = await c.get(s["url"])
            r.raise_for_status()
            with open(cache, "w", encoding="utf-8") as f:
                f.write(r.text)
            return r.text
        except Exception as e:
            old = os.path.isfile(cache)
            log.warning("proxy source failed %s (%s)%s", s["url"], type(e).__name__, " - old copy used" if old else "")
            if old:
                with open(cache, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            return ""

    async with _client() as c:
        texts = await asyncio.gather(*[one(c, i, s) for i, s in enumerate(items)])
    out = {}
    for s, t in zip(items, texts):
        for ln in t.splitlines():
            m = LINE_RE.match(ln)
            if not m:
                continue
            sch = SCHEME.get((m.group(1) or "").lower()) or s["scheme"]
            sch = SCHEME.get(sch, "auto")
            key = (m.group(2), int(m.group(3)))
            if key not in out or out[key] == "auto":
                out[key] = sch
    return out


# ------------------------------------------------------------------------- upstream protocols
async def _handshake(up, r, w, host, port):
    sch = up["scheme"]
    if sch == "socks5":
        user = up.get("user")
        w.write(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        await w.drain()
        ans = await r.readexactly(2)
        if ans[0] != 5:
            raise OSError("not socks5")
        if ans[1] == 2:
            if not user:
                raise OSError("socks5 wants a login")
            u, p = user.encode(), up.get("pw", "x").encode()
            w.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            await w.drain()
            if (await r.readexactly(2))[1] != 0:
                raise OSError("socks5 login refused")
        elif ans[1] != 0:
            raise OSError("socks5 method refused")
        h = host.encode()
        w.write(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
        await w.drain()
        hd = await r.readexactly(4)
        if hd[1] != 0:
            if hd[1] in (3, 4, 5, 6):
                raise TargetFail(f"socks5 reply {hd[1]}")
            raise OSError(f"socks5 reply {hd[1]}")
        if hd[3] == 3:
            n = (await r.readexactly(1))[0]
        elif hd[3] in (1, 4):
            n = 4 if hd[3] == 1 else 16
        else:
            raise OSError("socks5 bad reply")
        await r.readexactly(n + 2)
    elif sch == "socks4":
        try:
            ip = str(ipaddress.IPv4Address(host))
        except ValueError:
            ip = (await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET))[0][4][0]
        w.write(b"\x04\x01" + struct.pack(">H", port) + socket.inet_aton(ip) + b"\x00")
        await w.drain()
        ans = await r.readexactly(8)
        if ans[1] != 0x5A:
            raise OSError("socks4 refused")
    else:                                                    # http / https proxy: CONNECT
        w.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        await w.drain()
        head = await r.readuntil(b"\r\n\r\n")
        m = re.match(rb"HTTP/1\.[01] (\d{3})", head)
        if not m:
            raise OSError("not an http proxy")
        if m.group(1) != b"200":
            if m.group(1) in (b"502", b"503", b"504"):
                raise TargetFail("connect " + m.group(1).decode())
            raise OSError("connect " + m.group(1).decode())


async def open_via(up, host, port, timeout):
    """tcp connection to the target through upstream `up` -> (reader, writer)."""
    if up["scheme"] == "direct":                              # no proxy at all: this PC's own connection
        try:
            return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except socket.gaierror as e:
            raise TargetFail(f"dns {e.errno}")
        except (OSError, asyncio.TimeoutError) as e:
            raise TargetFail(str(e) or "timeout")
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(up["host"], up["port"]), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise ConnectFail(str(e) or "timeout")
    try:
        await asyncio.wait_for(_handshake(up, r, w, host, port), timeout)
    except BaseException:
        w.close()
        raise
    return r, w


async def probe(up, check, timeout):
    """one plain-http request through the proxy to the ip-check url -> (exit_ip, ms)."""
    t0 = time.time()
    u = urlparse(check)
    host, port = u.hostname, u.port or 80
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    r, w = await open_via(up, host, port, timeout)
    try:
        w.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: curl/8.0\r\nAccept: */*\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        data = b""
        while len(data) < 4096:
            ch = await asyncio.wait_for(r.read(4096), timeout)
            if not ch:
                break
            data += ch
        head, _, body = data.partition(b"\r\n\r\n")
        if not head.startswith(b"HTTP/1") or b" 200" not in head.split(b"\r\n")[0]:
            raise OSError("bad answer")
        m = IP_RE.search(body.decode("latin1"))
        if not m:
            raise OSError("no ip in answer")
        return m.group(1), int((time.time() - t0) * 1000)
    finally:
        w.close()


# ------------------------------------------------------------------------- scan
async def scan(cands, pc, ranges, log):
    """cands: {(ip, port): scheme|auto}. Every proxy gets `tries` attempts, scan_threads at a time. -> alive list."""
    threads = max(1, min(int(pc.get("scan_threads", 300)), 2000))
    tries = max(1, int(pc.get("tries", 3)))
    timeout = float(pc.get("timeout_sec", 6))
    check = pc.ip_check_url
    need_uk = bool(pc.get("require_uk_exit", True))
    q = asyncio.Queue()
    for (ip, port), sch in cands.items():
        q.put_nowait((ip, port, sch))
    total, done, alive = q.qsize(), [0], []

    async def worker():
        while True:
            try:
                ip, port, sch = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            schemes = AUTO if sch == "auto" else [sch]
            hit = None
            for _ in range(tries):
                for s in schemes:
                    up = {"scheme": s, "host": ip, "port": port}
                    try:
                        exit_ip, ms = await asyncio.wait_for(probe(up, check, timeout), timeout * 3)
                        hit = {"scheme": s, "host": ip, "port": port, "ms": ms, "exit": exit_ip, "key": f"{s}://{ip}:{port}"}
                        break
                    except ConnectFail:
                        break                              # port closed / silent: no other scheme will do better
                    except Exception:
                        continue
                if hit:
                    break
            done[0] += 1
            if hit and (not need_uk or hit["exit"] in ranges):
                alive.append(hit)

    async def progress():
        while True:
            await asyncio.sleep(5)
            log.info("proxy scan: %d/%d tried, %d alive", done[0], total, len(alive))

    log.info("proxy scan: %d proxies, %d parallel, %d tries each, %.0fs timeout", total, threads, tries, timeout)
    t0 = time.time()
    pt = asyncio.create_task(progress())
    try:
        await asyncio.gather(*[worker() for _ in range(min(threads, max(1, total)))])
    finally:
        pt.cancel()
    alive.sort(key=lambda p: p["ms"])
    by = {}
    for p in alive:
        by[p["scheme"]] = by.get(p["scheme"], 0) + 1
    log.info("proxy scan done in %.0fs: %d alive %s", time.time() - t0, len(alive), by or "")
    return alive


async def build_alive(cfg, log, ranges, folder):
    pc = cfg.network.proxy_pool
    cands = await download_sources(pc, folder, log)
    listed = len(cands)
    if pc.get("uk_only", True):
        cands = {k: v for k, v in cands.items() if k[0] in ranges}
    log.info("proxy lists: %d listed, %d with a UK ip", listed, len(cands))
    alive = await scan(cands, pc, ranges, log) if cands else []
    with open(os.path.join(folder, "alive.txt"), "w", encoding="utf-8") as f:
        for p in alive:
            f.write(f"{p['key']}\t{p['ms']}ms\texit {p['exit']}\n")
    return alive


# ------------------------------------------------------------------------- tor
def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


async def ensure_tor(cfg, log):
    """path of tor(.exe): tor_exe if it exists, tor on PATH, our own copy in <config folder>/tools/tor, else download the
    Tor Expert Bundle from torproject.org (tor_exe: auto)."""
    exe = (cfg.network.get("tor_exe") or "").strip()
    if exe and exe.lower() != "auto":
        p = exe if os.path.isfile(exe) else shutil.which(exe)
        if p:
            return p
        log.warning("tor_exe not found (%s) - using / downloading our own copy", exe)
    base = os.path.join(cfg["_base"], "tools", "tor")
    local = os.path.join(base, "tor", "tor.exe" if os.name == "nt" else "tor")
    if os.path.isfile(local):
        return local
    p = shutil.which("tor")
    if p:
        return p
    try:
        await download_tor(base, log)
    except Exception as e:
        log.error("tor download failed (%s: %s) - install the Tor Expert Bundle and set network.tor_exe", type(e).__name__, e)
        return None
    if os.path.isfile(local):
        if os.name != "nt":
            os.chmod(local, 0o755)
        return local
    log.error("tor download unpacked but %s is missing", local)
    return None


async def download_tor(base, log):
    sysname = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    mach = platform.machine().lower()
    arch = "x86_64" if mach in ("amd64", "x86_64") else "aarch64" if mach in ("arm64", "aarch64") else "i686"
    if not sysname:
        raise RuntimeError("no Tor Expert Bundle for " + platform.system())
    os.makedirs(base, exist_ok=True)
    arc = "https://archive.torproject.org/tor-package-archive/torbrowser/"
    async with _client(300) as c:
        vers = []
        try:
            vers.append((await c.get("https://aus1.torproject.org/torbrowser/update_3/release/downloads.json")).json()["version"])
        except Exception:
            pass
        try:
            idx = re.findall(r'href="(\d+\.\d+(?:\.\d+)?)/"', (await c.get(arc)).text)
            vers += sorted(set(idx), key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)[:4]
        except Exception:
            pass
        file, last = os.path.join(base, "bundle.tar.gz"), None
        for v in dict.fromkeys(vers):
            url = f"{arc}{v}/tor-expert-bundle-{sysname}-{arch}-{v}.tar.gz"
            try:
                log.info("tor: downloading %s", url)
                async with c.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(file, "wb") as f:
                        async for ch in r.aiter_bytes():
                            f.write(ch)
                break
            except Exception as e:
                last = e
        else:
            raise RuntimeError(f"no Tor Expert Bundle could be downloaded ({last})")
    root = os.path.realpath(base)
    with tarfile.open(file) as t:
        for m in t.getmembers():
            if not os.path.realpath(os.path.join(root, m.name)).startswith(root):
                raise RuntimeError("unsafe path in the tor archive")
        t.extractall(root)
    os.remove(file)
    log.info("tor: unpacked to %s", base)


async def start_tor(cfg, log, ranges, folder, uk=None, port=None, tag=""):
    """-> (upstream dict | None, process | None). Spawns tor when network.tor_exe is set; with uk the exits are UK only
    (uk=None: network.tor_uk). Several tors can run side by side: own port and own data folder (tag)."""
    n = cfg.network
    uk = bool(n.get("tor_uk")) if uk is None else uk
    exe = (n.get("tor_exe") or "").strip()
    sock = n.get("tor_socks")
    if not exe and not sock:
        return None, None
    port = int(port or n.get("tor_port") or 9050)
    proc = None
    if exe:
        if sock and not tag:
            port = int(urlparse(sock).port or port)
        if _port_open(port):
            log.info("tor: port %d already open - using that tor (not started again)", port)
        else:
            path = await ensure_tor(cfg, log)
            if not path:
                return None, None
            data = os.path.abspath(os.path.join(folder, "tor_data" + tag))
            os.makedirs(data, exist_ok=True)
            args = [path, "--SocksPort", f"127.0.0.1:{port}", "--DataDirectory", data, "--ClientOnly", "1",
                    "--Log", "notice stdout"]
            gd = os.path.join(os.path.dirname(os.path.dirname(path)), "data")        # tor needs geoip to know which exit is UK
            for opt, fn in (("--GeoIPFile", "geoip"), ("--GeoIPv6File", "geoip6")):
                if os.path.isfile(os.path.join(gd, fn)):
                    args += [opt, os.path.join(gd, fn)]
            if uk:
                args += ["--ExitNodes", "{gb}", "--StrictNodes", "1"]
            log.info("tor: starting %s on port %d%s", path, port, " (UK exits only)" if uk else " (any exit)")
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            atexit.register(lambda p=proc: p.returncode is None and p.terminate())
            ready = asyncio.Event()

            async def drain():
                while True:
                    ln = await proc.stdout.readline()
                    if not ln:
                        return
                    if b"Bootstrapped 100%" in ln:
                        ready.set()
            asyncio.create_task(drain())
            try:
                await asyncio.wait_for(ready.wait(), 240)
            except asyncio.TimeoutError:
                log.error("tor did not bootstrap in 240s")
                proc.terminate()
                return None, None
    elif sock:
        port = int(urlparse(sock).port or 9050)
    up = {"scheme": "socks5", "host": "127.0.0.1", "port": port, "key": "tor"}
    if uk and proc is None:                                   # a tor we did not start: prove it, every exit must be UK
        # (our own tor runs with ExitNodes {gb} StrictNodes 1 and tor's own geoip: that is the authority, range lists disagree)
        check = n.proxy_pool.ip_check_url
        for i in range(3):
            try:
                ip, _ = await asyncio.wait_for(probe(dict(up, user=f"chk{random.randint(0, 10**9)}", pw="x"), check, 20), 60)
            except Exception as e:
                log.error("tor check failed (%s) - tor not used", type(e).__name__)
                return None, proc
            if ip not in ranges:
                log.error("tor exit %s is not in the UK - tor not used. Put ExitNodes {gb} and StrictNodes 1 in torrc, or set network.tor_exe", ip)
                return None, proc
        log.info("tor: 3 circuits, all exits in the UK")
    return up, proc


# ------------------------------------------------------------------------- pool + gateway
class Pool:
    def __init__(self, pc, log, tor=None):
        self.pc, self.log, self.tor = pc, log, tor
        self.alive, self.fails, self.sticky = [], {}, {}
        self.mode = str(pc.get("mode", "both")).lower()
        self.levels, self.level = None, {}                    # levels: ordered upstreams, a host climbs one per rotate()

    def set_alive(self, lst):
        self.alive, self.fails = lst, {}

    def _choose(self, avoid):
        pool = [p for p in self.alive if p["key"] not in avoid]
        tor = None
        if self.tor and "tor" not in avoid and self.mode != "pool":
            tor = dict(self.tor, user=f"rs{random.randint(0, 10**9)}", pw="x")    # own circuit per sticky host
        if self.mode == "pool":
            tor = None
        elif self.mode == "tor":
            pool = []
        if pool and tor:
            return tor if random.random() < float(self.pc.get("tor_share", 0.5)) else random.choice(pool[:max(10, len(pool) // 2)])
        if pool:
            return random.choice(pool[:max(10, len(pool) // 2)])
        return tor

    @staticmethod
    def base(host):
        h = (host or "").lower()
        return h[4:] if h.startswith("www.") else h

    def pick(self, host, avoid=()):
        now = time.time()
        if self.levels:
            i = min(self.level.get(self.base(host), 0), len(self.levels) - 1)
            s = self.sticky.get(host)
            if s and s[0].get("lv") == i and now - s[1] < float(self.pc.get("sticky_sec", 300)):
                return s[0]
            up = dict(self.levels[i], lv=i)
            if up["scheme"] == "socks5":
                up["user"], up["pw"] = f"rs{random.randint(0, 10**9)}", "x"          # own tor circuit
            if len(self.sticky) > 5000:
                self.sticky.clear()
            self.sticky[host] = (up, now)
            return up
        s = self.sticky.get(host)
        if s and now - s[1] < float(self.pc.get("sticky_sec", 300)) and s[0]["key"] not in avoid and (
                s[0]["key"] == "tor" or any(p["key"] == s[0]["key"] for p in self.alive)):
            return s[0]
        up = self._choose(set(avoid))
        if up:
            if len(self.sticky) > 5000:
                self.sticky.clear()
            self.sticky[host] = (up, now)
        return up

    def bad(self, up, host):
        self.sticky.pop(host, None)
        if up["key"] == "tor":
            return
        self.fails[up["key"]] = self.fails.get(up["key"], 0) + 1
        if self.fails[up["key"]] >= 3:
            self.alive = [p for p in self.alive if p["key"] != up["key"]]
            self.log.info("proxy dropped %s (%d left)", up["key"], len(self.alive))

    def usable(self):
        if self.levels:
            return True
        return bool(self.alive and self.mode != "tor") or bool(self.tor and self.mode != "pool")

    def good(self, up):
        self.fails.pop(up["key"], None)


class Gateway:
    def __init__(self, pool, port, log):
        self.pool, self.port, self.log = pool, port, log
        self.server = None
        self.tunnels = {}                                     # host -> open writers, closed by rotate()

    def _forget(self, host, close=True):
        base = self.pool.base(host)
        if not base:
            return
        match = lambda k: self.pool.base(k) == base or k.endswith("." + base)
        for k in [k for k in self.pool.sticky if match(k)]:
            self.pool.sticky.pop(k, None)
        if close:
            for k in [k for k in self.tunnels if match(k)]:
                for w in list(self.tunnels.get(k, ())):
                    try:
                        w.close()
                    except Exception:
                        pass

    def rotate(self, host, climb=True):
        """the next connection to this host (and its www./sub hosts) gets another exit (a new tor circuit); with climb
        it also moves one level on (tor uk -> tor any -> direct). Open connections are closed so a browser with
        keep-alive connections cannot stay on the old exit."""
        base = self.pool.base(host)
        if climb and self.pool.levels and base:
            self.pool.level[base] = min(self.pool.level.get(base, 0) + 1, len(self.pool.levels) - 1)
        self._forget(host)

    def reset(self, host):
        """a site starts again: back to the first level."""
        self.pool.level.pop(self.pool.base(host), None)
        self._forget(host, close=False)

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", self.port)
        return f"http://127.0.0.1:{self.port}"

    async def close(self):
        if self.server:
            self.server.close()

    async def _open(self, host, port):
        if self.pool.levels:
            up = self.pool.pick(host.lower())
            try:
                return await open_via(up, host, port, float(self.pool.pc.get("connect_timeout_sec", 10)) + (20 if up["key"].startswith("tor") else 0))
            except TargetFail:
                raise
            except Exception:
                self.pool.sticky.pop(host.lower(), None)
                raise
        avoid = set()
        for _ in range(4):
            up = self.pool.pick(host.lower(), avoid)
            if not up:
                break
            try:
                r, w = await open_via(up, host, port, float(self.pool.pc.get("connect_timeout_sec", 10)))
                self.pool.good(up)
                return r, w
            except TargetFail:
                if up["key"].startswith("tor"):
                    raise TargetFail("target")                # tor itself says the host / name does not exist: final
                avoid.add(up["key"])
            except Exception:
                self.pool.bad(up, host.lower())
                avoid.add(up["key"])
        raise OSError("no working upstream")

    async def _client(self, cr, cw):
        w, host = None, None
        try:
            head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 30)
            line, _, rest = head.partition(b"\r\n")
            parts = line.decode("latin1").split()
            if len(parts) < 3:
                return
            method, target = parts[0].upper(), parts[1]
            first = None
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                host, port = host.strip("[]"), int(port or 443)
            else:
                u = urlparse(target)
                host, port = u.hostname, u.port or 80
                path = (u.path or "/") + ("?" + u.query if u.query else "")
                hdrs = [h for h in rest.split(b"\r\n") if h and not h.lower().startswith((b"proxy-", b"connection"))]
                first = f"{method} {path} {parts[2]}\r\n".encode() + b"\r\n".join(hdrs) + b"\r\nConnection: close\r\n\r\n"
            try:
                r, w = await self._open(host, port)
            except TargetFail:
                cw.write(b"HTTP/1.1 504 Target Unreachable\r\nContent-Length: 0\r\n\r\n")
                await cw.drain()
                return
            except Exception:
                cw.write(b"HTTP/1.1 502 No Route\r\nContent-Length: 0\r\n\r\n")
                await cw.drain()
                return
            reg = self.tunnels.setdefault(host.lower(), set())
            reg.add(w)
            if first is None:
                cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                w.write(first)
            await asyncio.gather(self._pipe(cr, w), self._pipe(r, cw))
        except Exception:
            pass
        finally:
            if w is not None and host:
                self.tunnels.get(host.lower(), set()).discard(w)
            for x in (cw, w):
                try:
                    if x:
                        x.close()
                except Exception:
                    pass

    @staticmethod
    async def _pipe(src, dst):
        try:
            while True:
                d = await src.read(65536)
                if not d:
                    break
                dst.write(d)
                await dst.drain()
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass


# ------------------------------------------------------------------------- entry point for main.py
class Routes:
    def __init__(self):
        self.url, self.gateway, self.procs, self.task = None, None, [], None

    def rotate(self, host, climb=True):
        if self.gateway:
            self.gateway.rotate(host, climb)

    def reset(self, host):
        if self.gateway:
            self.gateway.reset(host)

    def ok(self):
        return self.gateway is None or self.gateway.pool.usable()

    async def close(self):
        if self.task:
            self.task.cancel()
        if self.gateway:
            await self.gateway.close()
        for p in self.procs:
            if p and p.returncode is None:
                p.terminate()


async def prepare_levels(cfg, log, rt, levels, folder):
    """network.levels: ordered routes, e.g. [tor_uk, tor_world, direct]. A site's first rung uses the first, and every
    rotate() (the next rung, or the next try) moves it one on. tor_uk = our tor with UK exits only, tor_world = a
    second tor with any exit, direct = this PC's own connection."""
    n = cfg.network
    base = int(n.get("tor_port") or 9250)

    async def one(name):
        if name == "tor_uk":
            up, proc = await start_tor(cfg, log, None, folder, uk=True, port=base)
        elif name == "tor_world":
            up, proc = await start_tor(cfg, log, None, folder, uk=False, port=base + 1, tag="_world")
        elif name == "direct":
            up, proc = {"scheme": "direct", "key": "direct"}, None
        else:
            log.error("network.levels: unknown route '%s' (tor_uk | tor_world | direct)", name)
            return None, None
        if up is not None and name.startswith("tor"):
            up = dict(up, key=name)
        return up, proc

    res = await asyncio.gather(*[one(x) for x in levels])
    ups = []
    for name, (up, proc) in zip(levels, res):
        rt.procs.append(proc)
        if up is None:
            log.error("route %s is not available - left out", name)
        else:
            ups.append(up)
    if not ups:
        await rt.close()
        raise RuntimeError("no route is available (tor could not start)")
    pc = n.proxy_pool
    pool = Pool(pc, log, None)
    pool.levels = ups
    rt.gateway = Gateway(pool, int(pc.get("gateway_port", 8899)), log)
    rt.url = await rt.gateway.start()
    n["tor_rotate"] = False
    log.info("route: %s | levels: %s", rt.url, " -> ".join(u["key"] for u in ups))
    return rt


async def prepare(cfg, log):
    """before the first site: start tor (UK), download + scan the proxy lists, open the local gateway.
    Sets self.url (the proxy the fetcher must use) or leaves it None (plain network.tor_socks, as before)."""
    n = cfg.network
    pc = n.proxy_pool
    rt = Routes()
    folder = os.path.join(cfg.output.root, "proxies")
    os.makedirs(folder, exist_ok=True)
    levels = [str(x).lower() for x in (n.get("levels") or [])]
    if levels:
        return await prepare_levels(cfg, log, rt, levels, folder)
    wants_uk = bool(n.get("tor_uk")) or pc.enabled
    ranges = await load_ranges(pc, folder, log) if wants_uk else None
    wanted = bool(n.get("tor_exe") or n.get("tor_socks"))
    tor, proc = await start_tor(cfg, log, ranges, folder)
    rt.procs.append(proc)
    if not pc.enabled:
        n["tor_socks"] = f"socks5://127.0.0.1:{tor['port']}" if tor else None     # tor only (UK exits when tor_uk)
        if wanted and not tor:
            raise RuntimeError("tor is not usable - see the messages above")
        return rt
    pool = Pool(pc, log, tor)
    if pool.mode != "tor":
        pool.set_alive(await build_alive(cfg, log, ranges, folder))
    if not pool.alive and not (tor and pool.mode != "pool"):
        await rt.close()
        raise RuntimeError("no working UK route: no proxy passed the scan and tor is not available")
    rt.gateway = Gateway(pool, int(pc.get("gateway_port", 8899)), log)
    rt.url = await rt.gateway.start()
    n["tor_rotate"] = False                                   # the gateway rotates, not socks logins
    log.info("route: %s | mode %s | %d pool proxies%s", rt.url, pool.mode, len(pool.alive), " + tor" if tor else "")
    mins = float(pc.get("refresh_min", 0) or 0)
    if pool.mode != "tor":
        async def again():                                    # rescan on the timer, and at once when no route is left
            last = time.time()
            while True:
                await asyncio.sleep(20)
                due = mins > 0 and time.time() - last >= mins * 60
                if not (due or (not pool.usable() and time.time() - last >= 20)):
                    continue
                last = time.time()
                try:
                    pool.set_alive(await build_alive(cfg, log, ranges, folder))
                except Exception as e:
                    log.warning("proxy refresh failed: %s", e)
        rt.task = asyncio.create_task(again())
    return rt
