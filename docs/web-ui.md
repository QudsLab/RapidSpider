# Web interface

[Documentation home](README.md) · [Output](output.md) · [Laya](laya.md)

While a run is going, open **http://127.0.0.1:8800**. The address comes from `ui:` in `config.yml`.

```yaml
ui:
  enabled: true
  host: 127.0.0.1      # only this computer
  port: 8800
  keep_alive: true     # stay up after the queue is empty
```

With `keep_alive: true` the interface stays up when the queue is empty, so you can keep browsing the data. Press `Ctrl+C` in the terminal to quit.

> `host: 127.0.0.1` means only your own computer can open it. The interface has no login, so do not expose it to a network unless you know what that means.

## Dashboard

| Part | Shows |
| --- | --- |
| Header | Run state (running, paused), thread count with `-` and `+`, **Resume** / **Pause**, **Stop** |
| Big number | Sites finished of the sites in this run, with a progress bar |
| Stats | Sites per minute, time left, waiting, Laya questions asked, time per question |
| Outcomes | A coloured bar and a count and percentage for each status |
| Sites finished per minute | A chart for the last 20 minutes |
| Working now | Each site in progress, its stage and how many seconds it has been running |
| Laya | Laya version, device, loaded models and any errors |

### Controls

| Control | Effect |
| --- | --- |
| Pause / Resume | Workers finish their current site and wait, or carry on |
| `-` / `+` | Change how many sites run at once, live |
| Stop | Finish what is running and quit the run. Unfinished sites stay pending and are picked up next time |

## Data tables

The side menu lists every output table with its row count. Each table has its own page:

| Menu | Table | What to look for |
| --- | --- | --- |
| Facts | `facts` | Each accepted, rejected and unsure fact with confidence and location |
| Laya questions | `laya_calls` | Every question Laya was asked and its answer |
| Sites | `track` | One row per site with status and reason |
| Site text | `sites` | The cleaned text of each site |
| Pages | `pages` | Every fetched page |
| Profiles | `profiles` | One JSON profile per site, and whether it is ready |
| Steps | `incidents` | Every ladder step and review for a site |
| Site logs | `site_logs` | One readable log per site |
| Logos | `logos` | Logo candidates |
| Log | `run_log` | The run log |

On every table page:

- click a **column name** to sort
- use the **search box** to find text
- use the **drop-downs** to filter, for example facts by category or sites by status
- click a **row** to open its detail

## Reading a Laya question

Open any row in **Laya questions** to see:

- the text Laya judged
- its answer and its confidence
- the nearest solved example it was shown
- the other checks made on the same text

This is the fastest way to understand a wrong decision. If one is wrong, add a better example. See [Laya](laya.md#examples).

## A good first look after a run

1. **Dashboard**: check the outcome bar. Lots of `error` or `blocked` usually means the fetch ladder needs attention.
2. **Sites**: filter by `status` and read the `reason` column.
3. **Facts**: filter by `category` to see how well one ingredient was found.
4. **Profiles**: filter `ready` to see how many sites have enough to write from.
