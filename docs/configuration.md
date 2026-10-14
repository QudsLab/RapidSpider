# Configuration

[Documentation home](README.md) · [Getting started](getting-started.md) · [The pipeline](pipeline.md)

Everything is set in one YAML file, `config.yml`. It is merged over built-in defaults, so you only write what you want to change. A relative path is resolved from the folder that holds the config file.

Jump to: [input](#input) · [output](#output) · [run](#run) · [crawl](#crawl) · [fetch](#fetch) · [network](#network) · [laya](#laya) · [classes](#classes) · [parse](#parse) · [facts](#facts) · [ui](#ui) · [logos](#logos)

---

## name

```yaml
name: dental
```

A label for the config. It shows in the web interface and names the default output folder (`output/<name>`).

## input

Where the site list comes from.

```yaml
input:
  path: input/sites.csv
  format: auto
  columns:
    id: id
    url: website
    name: name
    address: [town]
  extra_columns: [town]
  limit: 0
```

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | none | The input file. Required |
| `format` | `auto` | `auto` guesses from the extension (`.csv`, `.tsv`, `.json`, `.sqlite`, `.db`). Or set `csv`, `json`, `sqlite` |
| `table` | none | SQLite only. The table to read |
| `where` | none | SQLite only. A raw SQL filter, for example `"done != 3"` |
| `columns` | `id, url, name, phone, address, email` | Maps RapidSpider's names to your column names. A list is joined with `, ` |
| `extra_columns` | `[]` | Columns copied into `sites.extra` as JSON, untouched |
| `limit` | `0` | Stop after this many rows. `0` means all |

Notes:

- `id` and `url` are required. A row without an id, or with a repeated id, is skipped.
- CSV delimiters (`,` `;` tab `|`) are detected automatically.
- JSON can be a list of objects, or an object whose first list value is used.

## output

```yaml
output:
  dir: output/dental
  per_day: false
  max_db_mb: 45
  store_pages: true
```

| Key | Default | Meaning |
| --- | --- | --- |
| `dir` | `output/<name>` | One folder per config |
| `per_day` | `false` | `true` puts each day in `output/<name>/YYYY-MM-DD/` with fresh databases |
| `max_db_mb` | `45` | A database file rolls over to `*_0002.db` before it reaches this size |
| `store_pages` | `true` | Keep one row per fetched page |
| `store_pages_text` | `false` | Also keep each page's cleaned text in `pages` (the full text is already in `sites`) |

## run

```yaml
run:
  threads: 3
  max_attempts: 3
  site_budget_sec: 240
  resume: true
  retry_statuses: [error, timeout, laya_down]
```

| Key | Default | Meaning |
| --- | --- | --- |
| `threads` | `3` | Sites processed at the same time. You can also change it live in the web interface |
| `max_attempts` | `3` | Total tries for a site that ends in a retry status |
| `site_budget_sec` | `240` | Time allowed for one site, across all ladder steps |
| `resume` | `true` | Skip sites already finished in this output folder |
| `retry_statuses` | `error, timeout, laya_down` | Statuses that are tried again on the next run |
| `pause_between_sites_sec` | `0.0` | Delay between sites, to be gentler |

## crawl

How much of each site is taken.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_pages` | `4` | Home page plus sub pages |
| `use_sitemap` | `true` | Read the sitemap to find pages |
| `max_page_bytes` | `1500000` | Largest page downloaded |
| `max_page_chars` | `40000` | Text kept per page |
| `max_site_chars` | `120000` | Text kept per site |
| `min_page_words` | `25` | Thinner pages are ignored |
| `min_site_words` | `60` | A site with less text in total becomes `no_content` |
| `prefer_paths` | contact, about, service, team, who-we, our-, location, find-us, fees, pricing | Links containing these are ranked higher |
| `skip_paths` | wp-admin, login, cart, checkout, account, tag, feed, privacy, cookie, terms, sitemap | Links containing these are never followed |
| `skip_ext` | images, pdf, office files, media, css, js, xml, json, txt | File types never fetched |

## fetch

```yaml
fetch:
  ladder: [http, browser, browser_headed, browser_tor]
  timeout_sec: 30
```

The ladder is tried in order until a page is accepted. Rungs:

| Rung | What it does |
| --- | --- |
| `http` | Plain HTTP request. Fast and light |
| `browser` | Headless Chromium through Playwright. Runs JavaScript |
| `browser_headed` | A visible browser window. Gets past some bot checks |
| `browser_tor` | Headless browser through Tor. Needs `network.tor_socks` |
| `http_tor` | Plain HTTP through Tor |
| `browser_headed_tor` | Visible browser through Tor |

Other keys: `user_agent`, and `block_resources` (default `image, media, font`, skipped in the browser to save time).

If you do not run Tor, remove `browser_tor` from the ladder.

## network

| Key | Default | Meaning |
| --- | --- | --- |
| `tor_socks` | `null` | For example `socks5://127.0.0.1:9050`. Tor must already be running |
| `dead_confirm_tries` | `3` | A domain that does not resolve is re-checked this many times through Tor before it is called `dead` |
| `accept_unconfirmed_dead` | `false` | When `true`, a domain is called `dead` even if Tor is not available to confirm |

## laya

The AI model that makes the judgement calls. See [Laya](laya.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `device` | `auto` | `auto`, `cpu` or `cuda` |
| `models` | `auto` | `auto` is English on CPU, all three on GPU. Or a list such as `english,multilingual,typed-decisions` |
| `torch_threads` | `8` | CPU threads for PyTorch |
| `max_ram_mb` | `10000` | Above this the least-used model is unloaded |
| `retries` | `2` | Retries on a failed call |
| `confirm.rounds` | `5` | A verdict that ends a site (dead, parked, third party, no content) is re-asked this many times |
| `confirm.min_agree` | `0.6` | Share of rounds that must agree. Otherwise the page is treated as normal |
| `log.enabled` | `true` | Store every question and answer in `laya_calls_*.db` |
| `log.text_chars` | `400` | Characters of judged text kept per row |
| `examples.*` | | Solved examples. See [Laya](laya.md#examples) |

## classes

What to do for each kind of page. `action` is one of `wait`, `escalate`, `stop`, `single_page`, `crawl`.

| Class | Default action | Notes |
| --- | --- | --- |
| `ok` | `crawl` | A real site |
| `holding` | `wait` | "One moment, please" shields. Waits up to `max_wait_sec` (45), clears cookies and reloads every `clear_every_sec` (12) |
| `cloudflare` | `wait` | Waits up to 25 s |
| `dead` | `stop` | Parked, expired, placeholder or suspended |
| `third_party` | `single_page` | A page on someone else's platform. See below |
| `error`, `js_shell`, `empty` | `escalate` | Try the next rung |

`extra_patterns` on `holding`, `cloudflare` and `dead` lets you add your own text patterns.

`third_party` settings:

| Key | Default | Meaning |
| --- | --- | --- |
| `status` | `third_party` | Status given to a kept third-party page |
| `identity_min` | `2` | Proofs needed that the page is this business (name plus phone, email or postcode) |
| `trader_min` | `0.6` | Laya confidence for a line to count as being about this business |
| `follow_max` | `3` | "More details" links followed from the page, each must prove identity |
| `signals`, `signals_min` | `[]`, `5` | Regex patterns that mark multi-branch or marketplace chrome, and how many must match |

## parse

How text is cleaned.

| Key | Default | Meaning |
| --- | --- | --- |
| `laya_block_filter` | `true` | Laya keeps or drops every text block that survived the basic rules |
| `drop_confidence` | `0.6` | A block is dropped only when Laya says drop at least this surely |
| `block_min_words` | `4` | Shorter lines are not judged, they are kept |
| `block_max_words` | `150` | Longer blocks are cut for judging |
| `block_batch` | `60` | Blocks per Laya call |
| `max_drop_ratio` | `0.8` | If Laya wants to drop more than this share of a page, its drops are ignored |
| `near_dup` | `0.7` | A paragraph this similar to one already kept on the site is dropped |
| `drop_selectors` | script, style, nav, footer, header, cookie and consent elements and more | HTML elements removed before anything else |
| `drop_line_patterns` | privacy, accept/reject all, cookie settings and more | Lines removed by pattern |

## facts

See [Facts](facts.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Set `false` to collect only text |
| `file` | `info.yml` | The list of ingredients |
| `max_per_category` | `4` | Facts kept per ingredient (an ingredient can set its own `max`) |
| `max_blocks` | `96` | Sentences read per site |
| `ready_min_categories` | `4` | A profile is `ready` when this many ingredients have a fact |
| `scan.chunk_sentences` | `8` | Numbered sentences Laya reads at once |
| `scan.pick_min` | `0.55` | Laya confidence for a picked sentence to become a candidate |
| `scan.questions_per_pass` | `24` | Ingredient questions sharing one pass |
| `verify.rounds` | `8` | Re-questions per candidate fact, worded differently with the answer keys flipped |
| `verify.max_candidates` | `20` | Candidates verified per site |
| `verify.accept_frac` | `0.67` | Share of rounds that must agree to accept |
| `verify.reject_frac` | `0.34` | At most this share agreeing means rejected. In between is "unsure" and is not put in the profile |
| `verify.auto_teach` | `true` | Unanimous decisions become new Laya examples |

Speed is the number of Laya passes per site. Lower `verify.rounds`, `verify.max_candidates` or `max_blocks` to go faster.

## ui

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Run the web interface |
| `host` | `127.0.0.1` | Only this computer. Change it only if you understand the exposure |
| `port` | `8800` | |
| `keep_alive` | `true` | Stay up after the queue is empty, so you can keep browsing the data |

## logos

Off by default.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Collect logo candidates |
| `laya_filter` | `true` | Only images Laya calls a logo are downloaded |
| `max_candidates` | `8` | Candidates considered per site |
| `min_score` | `20` | Minimum heuristic score |
| `max_logo_bytes` | `2000000` | Largest image saved |
