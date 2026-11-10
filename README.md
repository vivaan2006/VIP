# VIP – BEA data (MacroCorpus)

Our part of MacroCorpus: pulling data from the BEA (Bureau of Economic Analysis) and turning it into one standard JSON format. All the code is in `macrocorpus-bea/`.

## How to run it

1. Get a free BEA API key here: https://apps.bea.gov/API/signup/ (they email it to you)
2. From the repo folder, run:
   ```bash
   python run.py
   ```
   That's it. It installs the packages, asks for your key the first time (saved to `macrocorpus-bea/.env`), checks the key works, then downloads every table of all 12 BEA datasets.

   - The full run is long, plan on leaving it overnight, and needs 10+ GB of free disk. Regional now pulls every county, metro and state for every line, MNE tries 25 request settings per series, and BEA only allows 100 requests a minute.
   - If it stops (laptop sleeps, Wi-Fi drops), run `python run.py` again. Every finished request is saved, so it skips straight to where it left off.
   - `python run.py --sample-size 5` is a quick test run (5 tables per dataset, about 10 minutes).
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

What to look for:

- **Start with the table at the top of `reports/coverage.md`.** Every dataset should have a status of `ok` or `issues` and a series count above 0. `failed` means nothing came back for that dataset.
- **In `reports/issues.json`, `error` is the one that matters.** It means a request got nothing back. A handful are normal, since some BEA tables reject the "everything" request. Lots of errors in one dataset, or errors saying `Network problem`, mean something actually went wrong, so just rerun.
- **`warning` and `info` are mostly BEA's own gaps, not our bugs.** Known ones are GDPbyIndustry listing industry `622HO` that never has data, some IIP and MNE series where every value is hidden (`(D)` = suppressed for privacy), and MNE series that only exist for some directions of investment.
- **"Requests with data" is lower than total requests on purpose.** Regional tries every geography level and MNE tries every request setting, and the ones BEA doesn't publish come back empty. What matters per table is `Series` above 0 and the `Missing` column.
- **If the run stopped early,** the last lines of `reports/run_log.txt` show where. Rerun and it continues from there.

### Metadata for our 5 table groups (TA task)

```bash
python -m bea_corpus metadata
```
Runs IIP, MNE, IntlServTrade, FixedAssets and NIUnderlyingDetail (set in `config/metadata_groups.yaml`, add a block there to do another table). Only pulls a couple of years per table. Output goes to `data/BEA/<Dataset>/` in the same layout as the TA's reference code: parameter list, every classification's values, a sample with dimensions/notes/observations, and a series metadata file. Every observation has a `series_id` that links it to its entry in the series metadata. `data/BEA/bea_metadata_summary.json` compares all 5.

## What it does

- Uses the BEA API key to call BEA's official API. No web scraping, it's just requests that return JSON.
- Checks BEA's list of datasets against `config/datasets.yaml`, so if BEA adds a dataset we'd see it in the issues.
- For each of the 12 datasets, saves the **dictionary**: every parameter and every value it can take (all tables, industries, countries, etc.).
- `python run.py` downloads **every table** of every dataset (sample size 1000, bigger than any dataset's table count) and **every series in each table**, no cap. `--sample-size 5` takes 5 tables spread across each dataset's list instead.
- **Regional**: one line per request, so it requests every line, at **every geography level the table has** (county, state, metro, micropolitan, metro division, combined area, metro/nonmetro portion). It keeps all the levels that return data instead of stopping at the first one.
- **MNE**: each series exists for some directions (outward, inward, parent, state), classifications (by country, industry, country x industry, ...) and ownership levels and not others. It tries all 25 combinations in `config/datasets.yaml` and keeps **every one with data**. Direction, classification and ownership level go in the series id.
- **IntlServSTA**: BEA won't send every country at once, so it's one request per country.
- **Empty tables**: if a whole-table request comes back empty or refused, it asks again one frequency at a time, then ten years at a time. A table with no series is `FAIL`, never `ok`.
- Checks the download is complete: where BEA can list what a table should contain (Regional lines, ITA/IntlServTrade countries, GDPbyIndustry industries), it compares that list with what came back. Anything missing gets asked for on its own, and if it still has no data it goes in the issues.
- Stores every series as JSON: info about the series (dataset, title, frequency, units) plus a list of dates and a list of values.
- **A series is decided by its codes only** (SeriesCode, RowCode, GeoFips, Industry, ...), never by names or display order. So when BEA renames something (East Timor became Timor-Leste in 2009) it stays one unbroken history, and every name it had is kept in `label_history`. The series id is built from the same codes, so it's the same every run.
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
