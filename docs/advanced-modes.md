# Advanced modes: retry, raw mode and UK routing

Three features for large or difficult lists. They are all optional; the default `config.yml` run is unchanged.

## Retrying failed sites

```bash
python main.py config.yml --retry                          # error, timeout, laya_down, blocked, dead, no_content: again and again
python main.py config.yml --retry error,dead --rounds 0 --retry-wait 120
python main.py config.yml --reset                          # every site again
```

Whenever the queue is empty, `--retry` puts the failed sites back, waits `--retry-wait` seconds and runs them again, until none is left or `--rounds` is reached (`0` = no limit).

To build a new input from an old run, use `make_retry_input.py`. It writes one table, `retry`, with every site that is not `ok` / `third_party`, with all directory columns plus the old `track_*` columns:

```bash
python make_retry_input.py backup/dental/track.db input/sites.sqlite input/retry.sqlite [directory_table]
```

## Raw mode (no Laya)

`config.raw.yml` sets `laya.enabled: false`. Sites are fetched and the full text and HTML are stored (`store_html: true`), with `track.processed = 0`. No model is needed on that machine, so the Laya packages in `requirements.txt` are not used. Challenge pages are handled with a fixed order of moves (click verify, scroll, reload ...).

Run Laya later, on a machine that has it, over the stored data:

```bash
python main.py config.raw.yml
python postprocess.py config.yml --from output/retry
python postprocess.py config.yml --from output/retry --only 12,34 --limit 200
```

`postprocess.py` takes every site with `processed = 0`, rebuilds its pages from the stored raw text and HTML, and runs the normal Laya steps. It can be stopped and started at any time.

## Challenge pages and real Chrome

New `fetch` options in `config.yml`:

| Key | Meaning |
| --- | --- |
| `engine` | `playwright`, `chrome` (your installed Chrome, best against Cloudflare), `patchright` or `camoufox` |
| `browser_channel`, `chrome_path`, `chrome_port` | which Chrome to use and its debugging port |
| `ladder` | adds `browser_headed_tor` after `browser_tor` |
| `shield_via_tor` | after a Cloudflare / "one moment" page, retry only through Tor, never from your own IP |
| `classes.<shield>.interact` | drift the mouse and click the verify checkbox like a person |
| `classes.<shield>.reload_every_sec` | re-open the page on a timer, cookies kept |
| `network.tor_rotate` | new Tor circuit per site |

## UK-only routing

`config.uk.yml` sends every request from a UK IP. `rapidspider/proxies.py` does the work:

* **Tor** with `ExitNodes {gb}`. With `tor_exe: auto` the Tor Expert Bundle is downloaded from torproject.org into `tools/tor` on first use (not part of the repository).
* **Proxy pool** (`network.proxy_pool`): free proxy lists are downloaded at every start, cut to UK IPs with a range list, tested, and kept only if they work and exit in the UK.
* **Gateway**: one local HTTP proxy (`gateway_port`) that the fetcher points at. It keeps one exit per target host for `sticky_sec` and drops proxies that keep failing.
* **`network.levels`**: one level per rung of a site, for example `[tor_uk, tor_world, direct]`. Remove `direct` to never use your own IP.

Free proxies are often dead or tamper with HTTPS; Tor-only (`mode: tor`) is the reliable setting.

[Back to the documentation](README.md)
