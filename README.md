# VIP – BEA data (MacroCorpus)

Our part of MacroCorpus: pulling data from the BEA (Bureau of Economic Analysis) and turning it into one standard JSON format. All the code is in `macrocorpus-bea/`.

## How to run it

1. Get a free BEA API key here: https://apps.bea.gov/API/signup/ (they email it to you)
2. From the repo folder, run:
   ```bash
   python run.py
   ```
   That's it. It installs the packages, asks for your key the first time (saved to `macrocorpus-bea/.env`), checks the key works, then downloads every table of all 12 BEA datasets.

   - The full run is long, plan on leaving it overnight, and needs 10+ GB of free disk. Regional now pulls every county, metro and state for every line, MNE tries 25 to 82 request settings per series the first time (about 2,200 requests; reruns skip the ones BEA turned down), and BEA only allows 100 requests a minute.
   - If it stops (laptop sleeps, Wi-Fi drops), run `python run.py` again. Every finished request is saved, so it skips straight to where it left off.
   - `python run.py --sample` is a quick test run (a few tables per dataset, about 10 minutes). `--sample 10` takes 10 tables per dataset.
   - `--only NIPA MNE` runs just some datasets, and `--refresh` ignores saved responses.

   Needs Python 3.10+. If `pip install` complains, use a virtual environment: `python3 -m venv .venv && source .venv/bin/activate && python run.py`

## Checking the results

| What | Where (inside `macrocorpus-bea/`) |
|---|---|
| The data, one file per table | `data/corpus/<Dataset>/series/<table>.json` |
| Each dataset's full dictionary | `data/corpus/<Dataset>/dictionary.json` |
| Summary per dataset and per table | `reports/coverage.md` |
| Everything that looked off | `reports/issues.json` |
| Everything the run printed | `reports/run_log.txt` |
| Every MNE request setting tried, per series, and what BEA said | `reports/shapes_MNE.json` |

What to look for:

- **Start with the table at the top of `reports/coverage.md`.** Every dataset should have a status of `ok` or `issues` and a series count above 0. `failed` means nothing came back for that dataset.
- **In `reports/issues.json`, `error` is the one that matters.** It means a request got nothing back. A handful are normal, since some BEA tables reject the "everything" request. Lots of errors in one dataset, or any error or warning that says BEA didn't answer / `rerun to try again` (network, throttling, BEA server trouble), mean something actually went wrong, so just rerun. Those are never counted as "no data", and a table BEA didn't fully answer keeps its last good download.
- **`warning` and `info` are mostly BEA's own gaps, not our bugs.** Known ones are GDPbyIndustry listing industry `622HO` that never has data, some IIP and MNE series where every value is hidden (`(D)` = suppressed for privacy), MNE series that only exist for some directions of investment, and 9 MNE series BEA has discontinued (11, 13, 16, 19, 50, 51, 52, 55, 56), which show up as `info`.
- **"Requests with data" is lower than total requests on purpose.** Regional tries every geography level and MNE tries every request setting, and the ones BEA doesn't publish come back empty. What matters per table is `Series` above 0 and the `Missing` column.
- **If the run stopped early,** the last lines of `reports/run_log.txt` show where. Rerun and it continues from there.

### Metadata for our 5 table groups (TA task)

```bash
python -m bea_corpus metadata
```
Runs IIP, MNE, IntlServTrade, FixedAssets and NIUnderlyingDetail (set in `config/metadata_groups.yaml`, add a block there to do another table). Only pulls a couple of years per table. Output goes to `data/BEA/<Dataset>/` in the same layout as the TA's reference code: parameter list, every classification's values, a sample with dimensions/notes/observations, and a series metadata file. Every observation has a `series_id` that links it to its entry in the series metadata. `data/BEA/bea_metadata_summary.json` compares all 5.

## What it does

Diagrams of the whole flow, plus where to look when something breaks: [docs/workflow.md](docs/workflow.md).

- Uses the BEA API key to call BEA's official API. No web scraping, it's just requests that return JSON.
- Checks BEA's list of datasets against `config/datasets.yaml`, so if BEA adds a dataset we'd see it in the issues.
- For each of the 12 datasets, saves the **dictionary**: every parameter and every value it can take (all tables, industries, countries, etc.).
- `python run.py` (or `python -m bea_corpus download`) downloads **every table** of every dataset and **every series in each table**, no cap. Nothing to add. `--sample` is only for quick test runs.
- **Regional**: one line per request, so it requests every line, at **every geography level the table has** (county, state, metro, micropolitan, metro division, combined area, metro/nonmetro portion). It keeps all the levels that return data instead of stopping at the first one.
- **MNE**: each series exists for some directions (outward, inward, parent, state), classifications (by country, industry, country x industry, destination, ...) and ownership levels and not others. It tries every combination in `config/datasets.yaml` that applies to that series and keeps **every one with data**. The original 25 are tried first for every series; the extra ones only go to the series they apply to (BEA's own series list for directions, BEA's dictionary for classifications). If an extra one returns exactly the same numbers as an original one, the original keeps its series id. Direction, classification and ownership level go in the series id.
  - Older years of the activities-of-multinationals series need `NonbankAffiliatesOnly=1` (outward and parent up to 2008, inward up to 2006), newer years `0`. Both are tried and join into one history, and the series' `definition_history` says where it switches (before it, nonbank affiliates only).
  - Combinations BEA turns down are remembered for 60 days in `data/raw/cache/shapes_MNE.json` (saved every 20 tries and after every series, so an interrupted run keeps almost all of them) and skipped, so reruns are fast. `--refresh` tries them all again.
  - `reports/shapes_MNE.json` lists, for every series, each combination tried and what happened: data, no data (with BEA's reason), skipped, no answer, or error (a bug on our side, logged in issues.json).
- If one code carries several measures told apart only by name (GDPbyIndustry table 25 lists energy inputs, value added, ... under industry 11), those are split into separate series.
- If BEA sends two different numbers for the same series and date, the first is kept and it's logged as a warning. Exact repeats are merged quietly.
- Units: MNE only says "Millions of Dollars" in a text field, so that's read into `unit: USD` and `unit_multiplier: 6`.
- Errors never stop the run, network drops included (it retries first). They get logged and it moves on. Files are written to a temp name and then renamed, so a run that gets cut off never leaves half a file, and a table that comes back empty removes its old file.
- `--only` runs update just those datasets in the reports and keep everything else.

## Where stuff ends up

- `data/corpus/<Dataset>/dictionary.json` – the dataset's full dictionary
- `data/corpus/<Dataset>/series/<table>.json` – every series in that table (this is the main output)
- `data/raw/` – the original BEA responses, in case we need to re-convert
- `reports/coverage.md` – per dataset and per table: series, requests, MB, and whether it was checked against the dictionary
- `reports/issues.json` – everything that looked off, marked `error`, `warning` or `info`

`data/` isn't on GitHub since it's big (about 650 MB). Two examples are included so you can see the output without running anything: `macrocorpus-bea/data/corpus/NIPA/` (dictionary + one table) and the TA metadata output in `macrocorpus-bea/data/BEA/`.

If we change the JSON format, run `python -m bea_corpus standardize`. It redoes the JSON from the saved raw files without downloading again.

## Important facts

- **Last full run (Nov 4, before the audit fixes): about 320,000 series and 15 million numbers, 7 GB on disk** (6.3 GB of that is raw BEA responses). That run had no county data and only 5 MNE series, so the next one will be bigger.
- The issues are BEA's own gaps, not download failures. GDPbyIndustry's dictionary lists industries (like `622HO`) that never have data, and a few IIP/MNE series have every value hidden.
- Sizes: one NIUnderlyingDetail table (U20404) is about 100 MB on its own and one InputOutput table is about 35 MB. Most tables are under 3 MB.
- BEA limits you to 100 requests, 100 MB and 30 errors a minute. The code spaces out calls, waits when it gets near 100 MB, and pauses after errors.
- The API only gives the latest revised numbers, not what was originally published.
- Your API key stays in `.env`, which doesn't get pushed to GitHub.
