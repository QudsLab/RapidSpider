# Facts

[Documentation home](README.md) · [Configuration](configuration.md#facts) · [Laya](laya.md) · [Output](output.md)

Facts are the useful, checkable statements about a business that a writer needs for a long description, a short description and bullet points. They are listed in `info.yml` and collected by Laya.

## The idea: ingredients

`info.yml` is a list of **ingredients**. Each one is a kind of fact: services, opening hours, years in business, accreditations, prices, and so on. For every ingredient RapidSpider asks the same thing of the page: *which sentence clearly states this?*

You decide what is collected by editing `info.yml`. Add an ingredient to collect a new kind of fact, delete one to stop.

## An ingredient

```yaml
- key: guarantees_warranty
  label: Guarantees and warranties
  bullet: "<N>-month warranty / guarantee"
  description: "guarantees, warranties or money-back promises that come with the product or service"
  patterns:
    - {re: '(?i)\b(\d{1,2}|one|two|three|six|twelve)[\s-]*(?:month|year)s?[\s-]*(?:warranty|guarantee)', value: 0}
  examples:
    - "All repairs are guaranteed for 12 months or 12,000 miles."
  max: 3
```

| Field | Required | Meaning |
| --- | --- | --- |
| `key` | yes | Unique id, lowercase letters and underscores |
| `label` | yes | Name shown in the web interface |
| `bullet` | yes | What a bullet point about it looks like. Used by whoever writes the final text |
| `description` | yes | Plain English. Laya reads it as: "does this text state *<description>*?" |
| `patterns` | no | Regexes that find the exact value. `value` is the capture group to keep, `0` is the whole match |
| `examples` | no | Solved example sentences Laya sees next to the question |
| `max` | no | Most facts kept for this ingredient (default `facts.max_per_category`, 4) |
| `special` | no | `emails`, `phones` or `name`. Found by code, Laya is not asked |
| `ask: false` | no | Regex only. Laya is not asked |

### Writing a good description

Laya reads the description literally, so be concrete and list what counts:

| Weak | Strong |
| --- | --- |
| `"services"` | `"which treatments it provides: check-ups, hygiene, fillings, crowns, whitening, implants"` |
| `"hours"` | `"opening days and times, evening or Saturday appointments"` |
| `"quality"` | `"accreditations and quality marks: regulator registration, trade body membership, ratings"` |

Add two or three `examples` for anything Laya gets wrong.

## How a fact is found

1. **Code finds the exact things first**, with no AI: email addresses, phone numbers, the business name seen on the page, and everything with `patterns`.
2. **The page is cut into groups of eight numbered sentences** (`facts.scan.chunk_sentences`). For each group, Laya answers one question per ingredient, all in one pass: which numbered sentence states it, or none? Up to `facts.max_blocks` sentences (96) are read per site.
3. **The picked sentence is the fact.** A pick needs at least `facts.scan.pick_min` (0.55) confidence. Its position is stored with it: zone (top, middle, bottom of the page) and sentence number.
4. **Laya checks its own pick.** Each candidate is re-asked `facts.verify.rounds` times (8), each worded differently, with the answer keys flipped to cancel position bias. At most `facts.verify.max_candidates` (20) candidates are verified per site.

## Accepted, rejected or unsure

| Result | Rule | Goes in the profile |
| --- | --- | --- |
| accepted | At least 67% of rounds agree (`accept_frac`) and the mean agreement is at least 0.6 | yes |
| rejected | At most 34% agree (`reject_frac`) | no |
| unsure | In between | no |

There is no manual review step. Decisions every round agreed on are written to `learned_auto.yml` and become examples for later questions (`verify.auto_teach`).

## What is stored

Each fact row has its ingredient (`category`), the sentence (`text`), the extracted `value` if a pattern matched, `confidence`, `votes`, `zone` and `loc` (where on the page), the `source` and the source page `url`.

Each site also gets one **profile**: a JSON document with the business name, URL, contacts and the accepted facts grouped by ingredient. `profiles.ready` is `1` when at least `facts.ready_min_categories` (4) ingredients have a fact, meaning there is enough to write from.

See [Output](output.md) for the tables and sample queries.

## Speed

Speed is the number of Laya passes per site. Each costs about 0.1 to 0.8 s on a GPU and more on CPU. To go faster:

- lower `facts.verify.rounds` or `facts.verify.max_candidates`
- lower `facts.max_blocks`
- remove ingredients you do not need from `info.yml`
- set `facts.enabled: false` to collect only text

## Third-party sites

Facts from a third-party page (a directory listing, a social page) are checked as being about that business, using the same identity proof described in [The pipeline](pipeline.md#third-party-pages).
