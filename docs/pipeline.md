# The pipeline

[Documentation home](README.md) · [Configuration](configuration.md) · [Facts](facts.md)

What RapidSpider does for every site in your list, step by step.

```text
 DNS check ──► fetch ladder ──► classify ──► crawl ──► clean ──► facts ──► logos ──► save
```

## 1. DNS check

The domain is looked up first. A domain that does not resolve is not trusted straight away: it is re-checked through Tor (`network.dead_confirm_tries`, default 3) before the site is called `dead`. If Tor is not configured the failure is treated as temporary (`error`) and tried again on a later run, unless `network.accept_unconfirmed_dead` is `true`.

## 2. The fetch ladder

The home page is requested up the ladder in `fetch.ladder` until a page is accepted:

| Step | Method | When it helps |
| --- | --- | --- |
| `http` | Plain request | Most sites. Fast and light |
| `browser` | Headless Chromium | Sites that need JavaScript to show text |
| `browser_headed` | Visible browser window | Some bot checks |
| `browser_tor` | Headless browser through Tor | Sites that block your address |

The site has a time budget (`run.site_budget_sec`). If it runs out before the next step, the site ends as `timeout`.

## 3. Classify the page

Every fetched page is recognised as one of these kinds. `classes.<name>.action` in the config says what to do.

| Class | What it is | Default action |
| --- | --- | --- |
| `ok` | A real site | `crawl` |
| `holding` | A "one moment, please" or bot-check shield from a host | `wait` |
| `cloudflare` | A Cloudflare challenge | `wait` |
| `dead` | Parked, expired, hosting placeholder, registrar landing, error page | `stop` |
| `third_party` | A page on someone else's platform | `single_page` |
| `error`, `js_shell`, `empty` | Failed, or an empty JavaScript shell | `escalate` to the next step |

Laya referees the uncertain cases. A verdict that would end a site (dead, parked, third party, no content) is re-asked five times and must win at least 60% of rounds (`laya.confirm`), otherwise the page is treated as normal.

### Waiting

For `holding` and `cloudflare` pages the browser keeps polling. For holding pages it also clears cookies and reloads on a timer (`clear_every_sec`), which gets past many host shields. If the page never clears within `max_wait_sec`, the ladder moves on.

### Third-party pages

A social page, directory listing or multi-branch group site is not thrown away, but it is used strictly:

1. The page must **prove it is this business**: the name from your input plus at least one more match, a phone, email or postcode from the same input row (`identity_min`, default 2).
2. "More details" links on it are followed (`follow_max`, default 3) and a linked page is kept only if it proves the same.
3. Only **lines about the business** are kept. A line stays if it carries the name, phone, email or postcode, or Laya says it is about this business (`trader_min`).
4. The portal's own text and other listings are dropped, and contacts are taken from the kept lines only.

If the proof fails the site ends as `no_content` with the reason recorded.

This is why name, phone, address and email in your input matter.

## 4. Crawl

For a real site, more pages are taken from the sitemap (`crawl.use_sitemap`) and from links. Links are ranked by Laya and by `crawl.prefer_paths` (contact, about, service, team and so on). Links matching `crawl.skip_paths` and file types in `crawl.skip_ext` are never followed. At most `crawl.max_pages` pages are taken, home page included.

## 5. Clean the text

In this order:

1. HTML elements in `parse.drop_selectors` are removed (scripts, navigation, footers, cookie and consent banners).
2. Lines matching `parse.drop_line_patterns` are removed.
3. Laya keeps or drops each remaining text block (`parse.laya_block_filter`). A block is dropped only when Laya says drop with at least `drop_confidence` (0.6). If Laya wants to drop more than 80% of a page, its drops are ignored as a safety.
4. Paragraphs that are 70% or more similar to one already kept on the same site are dropped (`near_dup`).

The full unfiltered text is still stored in `pages.raw`, so a site can be reprocessed later.

If the cleaned text of the whole site is under `crawl.min_site_words` (60), the site ends as `no_content`.

## 6. Facts

Laya finds and verifies the facts listed in `info.yml`. See [Facts](facts.md).

## 7. Logos

Off by default (`logos.enabled`). When on, image candidates are scored, Laya picks the real logos, and the files are saved under `logos/<id>/`.

## 8. Save and statuses

Everything is written to the output folder. See [Output](output.md).

| Status | Meaning | `done` code |
| --- | --- | --- |
| `pending` | Not finished | 0 |
| `ok` | Real site, text collected | 1 |
| `third_party` | Third-party page proven to be this business | 1 |
| `dead` | Domain gone or parked, confirmed | 2 |
| `blocked` | Login wall or blocked on every step | 2 |
| `no_content` | Reached, but no real business text | 2 |
| `error`, `timeout`, `laya_down` | Failed, tried again on the next run | 3 |

A site in a retry status is attempted up to `run.max_attempts` times in total.
