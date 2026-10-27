# VIP – BEA data (MacroCorpus)

Our part of MacroCorpus: pulling data from the BEA (Bureau of Economic Analysis) and turning it into one standard JSON format. All the code is in `macrocorpus-bea/`.

## How to run it

1. Get a free BEA API key here: https://apps.bea.gov/API/signup/ (they email it to you)
2. Set it up:
   ```bash
   cd macrocorpus-bea
   pip install -e .
   cp .env.example .env
   ```
   then open `.env` and paste your key in.
3. Run it:
   ```bash
   python -m bea_corpus
   ```
   That's the whole download (same as `python -m bea_corpus download`). Takes about 10 minutes the first time,
   seconds after that because responses are saved. You'll see a line for every table it pulls.
   `--only NIPA Regional` runs just some datasets, `--refresh` ignores saved responses.

### Metadata for our 5 table groups (TA task)

```bash
python -m bea_corpus metadata
```
Runs IIP, MNE, IntlServTrade, FixedAssets and NIUnderlyingDetail (set in `config/metadata_groups.yaml`, add a block there to do another table). Only pulls a couple of years per table. Output goes to `data/BEA/<Dataset>/` in the same layout as the TA's reference code: parameter list, every classification's values, a sample with dimensions/notes/observations, and a series metadata file. Every observation has a `series_id` that links it to its entry in the series metadata. `data/BEA/bea_metadata_summary.json` compares all 5.

## What it does

- Uses the BEA API key to call BEA's official API. No web scraping, it's just requests that return JSON.
- Checks BEA's list of datasets against `config/datasets.yaml`, so if BEA adds a dataset we'd see it in the issues.
- For each of the 12 datasets, saves the **dictionary**: every parameter and every value it can take (all tables, industries, countries, etc.).
- Picks a sample of tables from each dataset (5, or 2 for InputOutput and 3 for Regional, spread across the table list) and downloads **every series in them**, no cap. Regional only gives one line of a table per request, so it requests every line.
- Checks the download is complete: where BEA can list what a table should contain (Regional lines, ITA/IntlServTrade countries, GDPbyIndustry industries), it compares that list with what came back. Anything missing gets asked for on its own, and if it still has no data it goes in the issues.
- Stores every series as JSON: info about the series (dataset, title, frequency, units) plus a list of dates and a list of values.
- Errors never stop the run. They get logged and it moves on.

## Where stuff ends up

- `data/corpus/<Dataset>/dictionary.json` – the dataset's full dictionary
- `data/corpus/<Dataset>/series/<table>.json` – every series in that table (this is the main output)
- `data/raw/` – the original BEA responses, in case we need to re-convert
- `reports/coverage.md` – per dataset and per table: series, requests, MB, and whether it was checked against the dictionary
- `reports/issues.json` – everything that looked off, marked `error`, `warning` or `info`

`data/` isn't on GitHub since it's big (about 650 MB). Two examples are included so you can see the output without running anything: `macrocorpus-bea/data/corpus/NIPA/` (dictionary + one table) and the TA metadata output in `macrocorpus-bea/data/BEA/`.

If we change the JSON format, run `python -m bea_corpus standardize`. It redoes the JSON from the saved raw files without downloading again.

## Important facts

- **Last run: all 12 datasets worked, 0 errors, about 17,700 series** from 55 sampled tables. Details in `reports/coverage.md`.
- The issues are BEA's own gaps, not download failures. GDPbyIndustry's dictionary lists industries (like `622HO`) that never have data, and a few IIP/MNE series have every value hidden.
- Sizes: one NIUnderlyingDetail table (U20404) is about 100 MB on its own and one InputOutput table is about 35 MB. Most tables are under 3 MB.
- **All of BEA would be roughly 3 GB** (estimated from an earlier sample). Regional would probably be even bigger than that, since it has every state, county and metro.
- BEA limits you to 100 requests, 100 MB and 30 errors a minute. The code spaces out calls, waits when it gets near 100 MB, and pauses after errors.
- The API only gives the latest revised numbers, not what was originally published.
- Your API key stays in `.env`, which doesn't get pushed to GitHub.
