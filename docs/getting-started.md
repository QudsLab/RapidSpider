# Getting started

[Documentation home](README.md) · [Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)

## 1. What you need

| | Needed |
| --- | --- |
| Python | 3.10 or newer |
| Disk | A few GB for the Laya models, plus room for the output |
| RAM | About 10 GB free is comfortable. On CPU with the English model alone, 4 GB can work for small runs |
| Internet | Needed on the first run to download the models. After that only the sites you scrape |
| GPU | Optional. Faster, and lets all three models load |

## 2. Install

Use a virtual environment so the packages stay separate from the rest of your system.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/macOS:  source .venv/bin/activate

pip install torch                      # CPU build. For a GPU use the CUDA build from pytorch.org instead
pip install -r requirements.txt
playwright install chromium            # one time
```

`laya` needs PyTorch, so install it first. `requirements.txt` pins `laya==0.3.9`.

## 3. Prepare the input

Create `input/sites.csv`. The `input/` folder is ignored by git, so your list stays private.

```csv
id,website,name,town
1,https://example-dental.co.uk,Example Dental,Leeds
2,https://another-practice.co.uk,Another Practice,Bristol
```

Only an id and a URL are required. The name, phone, address and email are used to prove that a third-party page (a Facebook page, a directory listing) really is this business. The more you give, the more third-party pages can be trusted.

The columns are mapped in `config.yml` under `input.columns`. See [Configuration, input](configuration.md#input).

## 4. Check the config

The repository ships a ready `config.yml` and `info.yml`. Before a big run look at these:

| Setting | Why |
| --- | --- |
| `input.path` | Points at your site list |
| `run.threads` | How many sites are fetched at the same time (default 3) |
| `laya.device` | `auto` uses CUDA if there is one, otherwise CPU |
| `fetch.ladder` | Remove `browser_tor` if you do not run Tor |
| `facts.file` | The `info.yml` that lists what to collect |

## 5. Run

```bash
python main.py config.yml
```

Open **http://127.0.0.1:8800** to watch progress. See [Web interface](web-ui.md).

Useful options:

```bash
python main.py config.yml --only 12,34           # only these input ids, good for testing one site
python main.py config.yml --requeue no_content   # run finished sites with that status again
```

Press `Ctrl+C` to stop. Unfinished sites stay pending and are picked up next time, because `run.resume` is on.

## 6. Read the results

Everything is written to `output/<name>/`. Open the `.db` files with any SQLite tool, or use the web interface tables. See [Output](output.md).

## CPU or GPU

| | CPU | GPU (CUDA) |
| --- | --- | --- |
| Models loaded | English only | English, multilingual, typed-decisions |
| Speed | Slower. Each Laya pass takes noticeably longer | About 0.1 to 0.8 s per pass |
| Memory | About 1.7 GB per loaded model | Uses VRAM |

To make CPU runs faster, lower `facts.verify.rounds` and `facts.verify.max_candidates`, or set `facts.enabled: false` to skip facts and only collect text.

## Next

- [The pipeline](pipeline.md): what happens to each site.
- [Facts](facts.md): change what is collected.
