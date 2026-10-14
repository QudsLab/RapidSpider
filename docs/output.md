# Output

[Documentation home](README.md) · [Facts](facts.md) · [Web interface](web-ui.md)

Each config writes to **one folder**, `output/<name>/` (`output.dir`). With `output.per_day: true` each day gets its own sub-folder, `output/<name>/YYYY-MM-DD/`.

Everything is SQLite, so you can open it with any SQLite tool, Python's `sqlite3`, or the [web interface](web-ui.md).

## Files

```text
output/dental/
├── track.db              one row per site
├── sites_0001.db         cleaned text and contacts
├── pages_0001.db         every fetched page
├── facts_0001.db         every fact
├── profiles_0001.db      one JSON profile per site
├── laya_calls_0001.db    every question Laya was asked
├── incidents_0001.db     every ladder step and review
├── site_logs_0001.db     one readable log per site
├── run_log_0001.db       the run log
├── logos_0001.db         logo candidates
├── logos/<id>/           saved logo images
└── logs/run_YYYY-MM-DD.log
```

Every table is split into numbered files. A file rolls over to `_0002.db` before it reaches `output.max_db_mb` (45), so none reaches 50 MB. When you query, read all files of a table (see below).

## track: one row per site

| Column | Meaning |
| --- | --- |
| `id`, `name`, `url`, `final_url` | From your input, and where the site ended up after redirects |
| `status` | `ok`, `third_party`, `dead`, `blocked`, `no_content`, `error`, `timeout`, `laya_down`, `pending` |
| `class` | The page class that decided it |
| `reason` | Why, in plain words |
| `done` | `0` running, `1` finished with data, `2` finished with nothing to take, `3` failed, retried next run |
| `attempts`, `rung` | Tries so far, and the ladder step that worked |
| `pages`, `words` | Pages kept and total words |
| `tor_used`, `elapsed_ms`, `started_at`, `finished_at`, `error` | Run details |
| `processed` | `1` when fully processed |

## sites: the text

| Column | Meaning |
| --- | --- |
| `id`, `attempt` | Site and which attempt wrote this row |
| `name`, `input_url`, `input_phone`, `input_address`, `input_email`, `extra` | Your input row |
| `final_url`, `status` | Result |
| `text` | The cleaned text of the whole site |
| `found_emails`, `found_phones` | Contacts found on the site |
| `summary` | Optional one-line summary |

## pages

One row per fetched page: `site_id`, `url`, `http_status`, `title`, `words`, `text`, `fetched_at`, and `raw`, the **full unfiltered text**, so a site can be reprocessed later without fetching again.

## facts

| Column | Meaning |
| --- | --- |
| `site_id`, `attempt` | Site |
| `category` | The ingredient key from `info.yml` |
| `text` | The sentence that states the fact |
| `value` | The extracted value if a pattern matched |
| `confidence`, `score`, `votes` | How sure, and how the verification rounds voted |
| `review` | `auto_good`, `auto_bad` or `auto_unsure` |
| `zone`, `loc` | Where on the page (top, middle, bottom, sentence number) |
| `source`, `url` | How it was found and the page it came from |

Only accepted facts go into the profile. Rejected and unsure ones are stored too (up to `facts.verify.max_rejected_stored`) so you can see them.

## profiles

One JSON document per site: business name, URL, contacts, and the accepted facts grouped by ingredient. `categories` is how many ingredients have a fact, and `ready` is `1` when that reaches `facts.ready_min_categories` (4). `ready = 1` means there is enough to write from.

## laya_calls

Every question: `task`, the judged `text`, the nearest example (`near_sim`, `near_label`), the answer `label` and `conf`, the `prompt` and `ms`. Use it to understand any decision.

## incidents and site_logs

`incidents` has one row per step (`step`, `outcome`, `detail`) for each site. `site_logs` has the same story as readable text, one per finished site.

## Querying across files

Tables are split, so attach the files or loop over them. In Python:

```python
import glob, sqlite3

def rows(folder, table, sql):
    for f in sorted(glob.glob(f"{folder}/{table}_*.db")):
        con = sqlite3.connect(f)
        yield from con.execute(sql)

for r in rows("output/dental", "facts", "SELECT site_id, category, text FROM facts WHERE category='opening_hours'"):
    print(r)
```

`track.db` is a single small file, so plain SQL works:

```sql
-- how did the run go?
SELECT status, COUNT(*) FROM track GROUP BY status ORDER BY 2 DESC;

-- why did sites fail?
SELECT reason, COUNT(*) FROM track WHERE done = 3 GROUP BY reason ORDER BY 2 DESC;

-- slowest sites
SELECT name, elapsed_ms FROM track ORDER BY elapsed_ms DESC LIMIT 10;
```

Run these inside the matching file:

```sql
-- facts_0001.db: which ingredients are found most?
SELECT category, COUNT(*) FROM facts WHERE review = 'auto_good' GROUP BY category ORDER BY 2 DESC;

-- profiles_0001.db: how many sites are ready to write from?
SELECT SUM(ready), COUNT(*) FROM profiles;
```

## Running again

- A normal run **resumes**: sites already finished are skipped (`run.resume`).
- `--requeue <status>` makes finished sites with that status run again. Their rows leave every table first, then they are processed fresh:

```bash
python main.py config.yml --requeue no_content,blocked
```

- Sites in `error`, `timeout` or `laya_down` are retried on the next run on their own, up to `run.max_attempts`.
