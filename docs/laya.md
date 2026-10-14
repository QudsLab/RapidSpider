# Laya

[Documentation home](README.md) · [Configuration](configuration.md#laya) · [Facts](facts.md)

Laya is the small decision model that runs **inside** RapidSpider. It makes the judgement calls a plain rule cannot: is this page parked, is this line boilerplate, does this sentence state an opening time.

- It is required. There is no separate server and no switch to turn it off.
- It needs no API key. The models download once on the first run, then work from the local cache.
- It answers short, structured questions, not free text, so it is fast and its answers are logged.

## What Laya decides

| Task | Question it answers |
| --- | --- |
| `page_kind` | Is this a normal site, a parked domain, an expired domain, a hosting placeholder, a registrar landing page, an error page, a login wall, a holding page, or a third-party platform? |
| `block` | Keep or drop this block of text? |
| `wait` | Is this still a "please wait" shell, a challenge, a permanent error, or real content? |
| `rank` | Which links on the site are most worth crawling? |
| `fact` | Which numbered sentence states this ingredient, or none? |
| `logo` | Is this image the business logo? |

## Models

| | CPU | GPU |
| --- | --- | --- |
| `laya.models: auto` | English only | English, multilingual and typed-decisions |

You can set `laya.models` to a list such as `english` or `english,multilingual`. Each loaded model costs about 1.7 GB on CPU. `laya.max_ram_mb` (10000) unloads the least-used model when memory goes over.

## Examples

Laya does not guess in a vacuum. For every question it is shown the **solved examples nearest to the text it is judging** (`laya.examples.k`, default 3). The better the examples, the better the answers. This is how you adapt RapidSpider to your niche.

Four places hold examples:

| Source | Written by | Use |
| --- | --- | --- |
| Built-in examples | the project | Generic starting set. Kept while `laya.examples.use_defaults` is `true` |
| `laya.examples` in `config.yml` | you | Examples for your own niche |
| `learned_examples.yml` | you, through the web interface or by hand | Corrections to answers that were wrong |
| `learned_auto.yml` | RapidSpider | Decisions every verification round agreed on |

### Add your own examples

Put them under `laya.examples` in `config.yml`, one list per task:

```yaml
laya:
  examples:
    use_defaults: true
    k: 3
    page_kind:                  # none | domain_parked | domain_expired | hosting_placeholder |
                                # registrar_landing | error_page | login_wall | holding_page | third_party_platform
      - {label: none, text: "Oakfield Dental is a family dentist in Leeds. We offer check-ups, hygiene and whitening."}
      - {label: third_party_platform, text: "Find a dentist near you. Search 4,000 dental practices across the UK."}
    block:                      # keep | drop
      - {label: keep, text: "Our dentists are registered with the General Dental Council."}
      - {label: drop, text: "Home About Us Treatments Fees Contact Privacy Policy"}
    logo:                       # logo | not_logo
      - {label: logo, text: "file: /uploads/oakfield-logo.png | alt: Oakfield Dental | found as: img | placement: header link"}
    wait:                       # content | waiting_shell | challenge | permanent_error
      - {label: waiting_shell, text: "One moment, please..."}
    fact:                       # a category key from info.yml, or none
      - {label: opening_hours, text: "Open Monday to Friday 8:30am to 5:30pm."}
```

Rules of thumb:

- A few **specific, typical** examples beat many vague ones.
- Cover the *boundary*: a real page that looks like boilerplate, boilerplate that looks like a real page.
- Texts are cut to `laya.examples.max_chars` (170).
- Keep labels exactly as listed above.

### learned_examples.yml (your corrections)

When a decision is wrong, the correct solved example goes in `learned_examples.yml`, next to `config.yml`. The file is written by the web interface review pages and you can edit it by hand. It holds a list of `label` and `text` per task:

```yaml
fact:
  - {label: opening_hours, text: "Surgery hours are 9 to 5 on weekdays."}
  - {label: none, text: "Opening times may change over bank holidays, see our news page."}
block:
  - {label: drop, text: "Website design by Example Digital."}
```

### learned_auto.yml (what Laya taught itself)

When all `facts.verify.rounds` agree on a decision, it is written here as a new example (`facts.verify.auto_teach`). The file header says it plainly: *safe to delete, it is rebuilt*.

- It holds real text from the sites you scraped, including company names and contact details. It is listed in `.gitignore`. Keep it out of a public repository.
- Automatic learning is not perfect. Now and then a sentence lands under the wrong label. If answers drift, review the file or delete it.
- `laya.examples.auto_max_per_label` (120) caps how many are kept per label.

## Seeing what Laya was asked

Every question and answer is stored in `laya_calls_*.db` (`laya.log.enabled`). The web interface shows it as **Laya questions**: the text judged, the answer and confidence, the nearest solved example and the other checks on the same text. This is the first place to look when a decision is wrong. See [Web interface](web-ui.md).

## When a decision ends a site

A verdict that would end a site (dead, parked, third party, no content) is not taken on one answer. It is re-asked `laya.confirm.rounds` times (5) with different wording, and must win at least `laya.confirm.min_agree` (60%) of them. Otherwise the page is treated as a normal page and the run carries on.
