#!/usr/bin/env python3
"""RapidSpider - config-driven site scraper.

    python main.py config.yml
    python main.py config.yml --only 101,202      # just these ids (debug)
    python main.py config.yml --requeue no_content
    web interface: http://127.0.0.1:8800  (config.yml -> ui)
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from rapidspider import config as config_mod
from rapidspider import procguard
from rapidspider import ui as ui_mod
from rapidspider.engine import Engine
from rapidspider.facts import Facts
from rapidspider.fetch import Fetcher
from rapidspider.inputs import load_rows
from rapidspider.laya import Laya
from rapidspider.store import Store, db_log_handler


def setup_logging(cfg, store):
    log = logging.getLogger("rapidspider")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
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


async def amain(cfg, only, requeue=None):
    store = Store(cfg)
    log = setup_logging(cfg, store)
    if requeue:
        log.info("requeued %d finished sites (%s) - they run again", store.requeue(requeue), ",".join(requeue))
    log.info("config: %s | output dir: %s | threads: %d", cfg._path, store.path, cfg.run.threads)
    rows = load_rows(cfg, only)
    if not rows:
        log.error("no input rows")
        return 1
    laya = Laya(cfg, log, store)
    await laya.ensure_running()          # mandatory: loads laya in this process, aborts the run if it cannot
    facts = Facts(cfg, laya) if cfg.facts.enabled else None
    fetcher = Fetcher(cfg, log)
    engine = Engine(cfg, store, fetcher, laya, log, facts, loader=lambda: load_rows(cfg, only))
    srv = None
    if cfg.ui.enabled:
        # the engine's loop must exist before the ui thread can talk to it
        engine.loop = asyncio.get_running_loop()
        engine.q = asyncio.Queue()
        srv = ui_mod.start(cfg, store, engine, laya, facts, log)
    try:
        await engine.run(rows)
    finally:
        log.info("summary: %s | laya questions: %d", store.counts(), laya.calls)
        if srv:
            srv.shutdown()
        await fetcher.close()
        await laya.close()
        store.close()
    return 0


def main():
    procguard.install()           # every child process (browsers) dies with this one
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--only", help="comma separated input ids")
    ap.add_argument("--requeue", help="run finished sites again, by status: e.g. no_content,blocked")
    a = ap.parse_args()
    cfg = config_mod.load(a.config)
    only = set(x.strip() for x in a.only.split(",")) if a.only else None
    try:
        sys.exit(asyncio.run(amain(cfg, only, a.requeue.split(",") if a.requeue else None)))
    except KeyboardInterrupt:
        print("\ninterrupted - unfinished sites stay 'pending' and are retried next run")
        sys.exit(130)


if __name__ == "__main__":
    main()
