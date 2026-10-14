# Troubleshooting

[Documentation home](README.md) · [Configuration](configuration.md) · [Getting started](getting-started.md)

## The run will not start

| Message | Cause and fix |
| --- | --- |
| `config not found` | The path to the config is wrong. Run `python main.py config.yml` from the project folder |
| `input.path missing or not a file` | Create the file, or fix `input.path`. It is relative to the config file |
| `facts.file (info.yml) not found` | Put `info.yml` next to the config, fix `facts.file`, or set `facts.enabled: false` |
| `cannot guess input format` | Use a `.csv`, `.json`, `.sqlite` or `.db` file, or set `input.format` |
| `input.table is required for sqlite input` | Set `input.table` |
| `no input rows` | The file is empty, ids are missing, or `input.where` / `input.limit` filtered everything |
| Laya will not load | Check that PyTorch installed (`python -c "import torch"`), and that you had internet on the first run to download the models |

## Slow

- On CPU every Laya pass is slow. See [Getting started, CPU or GPU](getting-started.md#cpu-or-gpu).
- Lower `facts.verify.rounds` (8) and `facts.verify.max_candidates` (20).
- Lower `facts.max_blocks` (96), or set `facts.enabled: false` to collect only text.
- Raise `run.threads` if your machine and the sites can take it.
- Remove steps you do not need from `fetch.ladder`. Each extra step costs time on a site that will not load.

## Out of memory

- Set `laya.models: english` to load one model.
- Lower `laya.max_ram_mb` so the least-used model is unloaded sooner.
- Lower `run.threads`.

## Lots of `error`, `timeout` or `blocked`

| Status | Likely cause | Try |
| --- | --- | --- |
| `error` | Network failure, DNS failure with no Tor, or a site that refuses the connection | Read `reason` in the Sites table. Rerun, errors are retried |
| `timeout` | A slow site used up `run.site_budget_sec` | Raise `run.site_budget_sec` |
| `blocked` | Login wall, or blocked on every step | Add `browser_headed` or Tor to the ladder |
| `laya_down` | Laya failed during the run | Check the Laya line on the dashboard. Rerun |

Bot checks are the usual reason. Put the heavier steps in the ladder: `[http, browser, browser_headed]`, and add `browser_tor` with a running Tor (`network.tor_socks: socks5://127.0.0.1:9050`).

Some sites will always refuse. That is expected, not a bug.

## Lots of `no_content`

The page was reached but there was no real business text. Common causes:

- A JavaScript-only site that `http` could not read. Make sure `browser` is in the ladder and that `playwright install chromium` was run.
- A third-party page that could not prove it is the business. Give name, phone, address and email in the input, and see [Pipeline, third-party pages](pipeline.md#third-party-pages).
- A thin site. `crawl.min_site_words` (60) decides what is too thin.

After changing settings, run those sites again:

```bash
python main.py config.yml --requeue no_content
```

## A good site was marked `dead`

A domain is called dead only after Laya agrees several times, or after the DNS check fails and is confirmed through Tor. If you see a wrong `dead`:

1. Open **Laya questions** for that site and read what it judged.
2. Add the page as a solved example under `laya.examples.page_kind` with label `none`. See [Laya](laya.md#examples).
3. Requeue it: `python main.py config.yml --requeue dead`.

## Facts are wrong or missing

| Problem | Fix |
| --- | --- |
| A fact is missing | Make the ingredient `description` more concrete. Add `examples`. See [Facts](facts.md#writing-a-good-description) |
| A wrong sentence is accepted | Add a solved example with label `none` under `laya.examples.fact`. Raise `facts.verify.accept_frac` |
| Too few facts get accepted | Lower `facts.scan.pick_min` or `facts.verify.accept_frac` a little |
| Profiles are never `ready` | Lower `facts.ready_min_categories`, or add more ingredients |

## Boilerplate is left in the text, or real text is cut

- Left in: add a `drop` example under `laya.examples.block`, or add a pattern to `parse.drop_line_patterns`.
- Cut: add a `keep` example, or raise `parse.drop_confidence` so Laya must be surer before it drops a block.

## The web interface does not open

- Check the address in `ui:` and that `ui.enabled` is `true`.
- If the port is in use, change `ui.port`.
- `127.0.0.1` only works from the same computer.

## Browsers left running after the program stops

RapidSpider ties every process it starts to itself (`rapidspider/procguard.py`). Laya, the web interface and the memory watchdog run inside the Python process and stop with it. The browsers are children and are closed with it:

| System | When the children are closed |
| --- | --- |
| Windows | Always, however the program ends (Ctrl+C, closed window, task manager, crash). Windows kills the whole group |
| Linux and macOS | On normal exit, Ctrl+C, `SIGTERM` and `SIGHUP`. A hard `SIGKILL` cannot be caught and may leave a browser behind, close it by hand |

## Starting over

Delete the output folder (`output/<name>/`) to start from nothing. `learned_auto.yml` is separate and safe to delete too, it is rebuilt.
