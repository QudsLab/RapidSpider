#!/usr/bin/env python3
"""RapidSpider - config-driven site scraper.

    python main.py config.yml
    python main.py config.yml --only 101,202      # just these ids (debug)
    python main.py config.yml --requeue no_content
    python main.py config.yml --retry               # failed sites (error, timeout, dead, blocked, no_content ...) again and again
    python main.py config.yml --retry error,dead --rounds 0 --retry-wait 120
    python main.py config.yml --reset               # every site again
    web interface: http://127.0.0.1:8800  (config.yml -> ui)
"""
import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime

from rapidspider import config as config_mod
from rapidspider import procguard
from rapidspider import proxies
from rapidspider import ui as ui_mod
from rapidspider.engine import Engine
from rapidspider.facts import Facts
from rapidspider.fetch import Fetcher
from rapidspider.inputs import load_rows
from rapidspider.laya import Laya, NoLaya
from rapidspider.store import Store, db_log_handler


class ColorFmt(logging.Formatter):
    """console only: a finished site is green when it is ok / third_party, yellow when it has nothing to take or can
    be retried, red when it failed or is dead. Warnings yellow, errors red."""
    G, Y, R, C = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
    OK = ("ok", "third_party")
    WARN = ("no_content", "blocked", "timeout", "laya_down", "pending")

    def format(self, rec):
        out = super().format(rec)
        m = re.search(r" -> (\w+)", out)
        if rec.levelno >= logging.ERROR:
            col = self.R
        elif rec.levelno == logging.WARNING:
            col = self.Y
        elif m and re.search(r"\[\d+/\d+\] id=", out):
            col = self.G if m.group(1) in self.OK else self.Y if m.group(1) in self.WARN else self.R
        else:
            return out
        return col + out + self.C


def setup_logging(cfg, store):
    log = logging.getLogger("rapidspider")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        if os.name == "nt":
            os.system("")                                  # turns on ansi colors in the windows console
        ch.setFormatter(ColorFmt("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    else:
        ch.setFormatter(fmt)
    log.addHandler(ch)
    logs_dir = os.path.join(cfg.output.root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(logs_dir, f"run_{datetime.now():%Y-%m-%d}.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(db_log_handler(store))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log


FAILED = ["error", "timeout", "laya_down", "blocked", "dead", "no_content"]


async def retry_loop(engine, store, log, statuses, rounds, wait):
    """--retry: whenever the queue is empty, put every failed site back and run it again, until none is left."""
    n = 0
    await asyncio.sleep(3)
    while not engine.quit:
        while engine.q is None or not engine.q.empty() or engine.active or engine.busy:
            await asyncio.sleep(2)
            if engine.quit:
                return
        c = store.counts()
        failed = sum(c.get(s, 0) for s in statuses)
        if not failed:
            log.info("retry: no failed site left - finished")
            break
        if rounds and n >= rounds:
            log.info("retry: %d rounds done, %d sites still failed (%s)", n, failed, {s: c[s] for s in statuses if c.get(s)})
            break
        n += 1
        log.info("retry round %d%s: %d failed sites %s - waiting %ds", n, f"/{rounds}" if rounds else "", failed,
                 {s: c[s] for s in statuses if c.get(s)}, wait)
        await asyncio.sleep(wait)
        store.requeue(statuses)
        engine.rescan()
    engine.quit = True


def _quiet(loop, ctx):
    """windows: closing a socket twice prints a harmless traceback from the proactor loop"""
    e = ctx.get("exception")
    if isinstance(e, OSError) and getattr(e, "winerror", 0) in (10022, 10053, 10054, 995):
        return
    loop.default_exception_handler(ctx)


async def amain(cfg, only, requeue=None, reset=False, retry=None, rounds=0, wait=60):
    asyncio.get_running_loop().set_exception_handler(_quiet)
    store = Store(cfg)
    log = setup_logging(cfg, store)
    if requeue:
        log.info("requeued %d finished sites (%s) - they run again", store.requeue(requeue), ",".join(requeue))
    if reset:
        log.info("reset: %d sites (all statuses) run again", store.requeue(list(store.counts())))
    log.info("config: %s | output dir: %s | threads: %d", cfg._path, store.path, cfg.run.threads)
    rows = load_rows(cfg, only)
    if not rows:
        log.error("no input rows")
        return 1
    try:
        routes = await proxies.prepare(cfg, log)     # tor (UK) + proxy lists scanned + local gateway, before anything runs
    except RuntimeError as e:
        log.error("%s", e)
        store.close()
        return 1
    laya = Laya(cfg, log, store) if cfg.laya.enabled else NoLaya(cfg, log, store)
    await laya.ensure_running()          # laya on: loads it in this process, aborts the run if it cannot. off: raw mode
    facts = Facts(cfg, laya) if cfg.facts.enabled else None
    fetcher = Fetcher(cfg, log, route=routes.url)
    fetcher.route_ok = routes.ok
    fetcher.rotate_host = routes.rotate
    fetcher.reset_host = routes.reset
    engine = Engine(cfg, store, fetcher, laya, log, facts, loader=lambda: load_rows(cfg, only))
    srv = None
    if retry:
        engine.hold = True
        engine.loop = asyncio.get_running_loop()
        engine.q = asyncio.Queue()
    if cfg.ui.enabled:
        # the engine's loop must exist before the ui thread can talk to it
        engine.loop = asyncio.get_running_loop()
        engine.q = asyncio.Queue()
        srv = ui_mod.start(cfg, store, engine, laya, facts, log)
    rt = asyncio.create_task(retry_loop(engine, store, log, retry, rounds, wait)) if retry else None
    try:
        await engine.run(rows)
    finally:
        if rt:
            rt.cancel()
        log.info("summary: %s | laya questions: %d", store.counts(), laya.calls)
        if srv:
            srv.shutdown()
        await fetcher.close()
        await routes.close()
        await laya.close()
        store.close()
    return 0


def main():
    procguard.install()           # every child process (browsers) dies with this one
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--only", help="comma separated input ids")
    ap.add_argument("--requeue", help="run finished sites again, by status: e.g. no_content,blocked")
    ap.add_argument("--reset", action="store_true", help="run EVERY site again (clears the finished state of all sites)")
    ap.add_argument("--retry", nargs="?", const=",".join(FAILED), metavar="STATUSES",
                    help="run the failed sites again and again (default statuses: " + ",".join(FAILED) + ")")
    ap.add_argument("--rounds", type=int, default=5, help="with --retry: at most this many rounds (0 = until none fails, or Ctrl+C)")
    ap.add_argument("--retry-wait", type=int, default=60, help="with --retry: seconds between rounds")
    a = ap.parse_args()
    cfg = config_mod.load(a.config)
    only = set(x.strip() for x in a.only.split(",")) if a.only else None
    try:
        sys.exit(asyncio.run(amain(cfg, only, a.requeue.split(",") if a.requeue else None, a.reset,
                                  a.retry.split(",") if a.retry else None, a.rounds, a.retry_wait)))
    except KeyboardInterrupt:
        print("\ninterrupted - unfinished sites stay 'pending' and are retried next run")
        sys.exit(130)


if __name__ == "__main__":
    main()
