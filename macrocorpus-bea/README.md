# MacroCorpus: BEA module (v1)

Pulls core U.S. macro series from the Bureau of Economic Analysis (BEA) API, converts them into one standard format with metadata, and profiles their statistical properties (length, missingness, persistence, volatility, seasonality, breaks).

This is the BEA slice of **MacroCorpus and Forecasting Benchmarks** (Georgia Tech VIP). Background on BEA itself is in [`docs/bea_overview.md`](docs/bea_overview.md).

## Setup

```bash
git clone <repo-url> && cd macrocorpus-bea
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # then paste your key
```

Get a free API key at <https://apps.bea.gov/API/signup/>.

## Usage

```bash
python -m bea_corpus fetch                  # download + standardize everything in config/series.yaml
python -m bea_corpus profile                # write reports/profile.csv
python -m bea_corpus datasets               # list BEA datasets
python -m bea_corpus params NIPA            # a dataset's parameters
python -m bea_corpus params NIPA TableName  # every NIPA table
python -m bea_corpus fetch --only us_pce_price_index --refresh
python -m bea_corpus download              # every dataset's dictionary + all series in sampled tables -> data/corpus/
python -m bea_corpus standardize           # rebuild the JSON from saved raw files
```

Raw responses are cached in `data/raw/cache/`, so re-running `fetch` doesn't hit the API again. Use `--refresh` after a BEA release.

## What v1 collects

| id | Series | Dataset / table | Freq | Profiled as |
|---|---|---|---|---|
| `us_real_gdp_growth` | Real GDP % change (annualized) | NIPA T10101 | Q | as published |
| `us_real_gdp_level` | Real GDP, chained $ | NIPA T10106 | Q | log diff |
| `us_pce_price_index` | PCE price index | NIPA T20804 | M | log diff (inflation) |
| `us_core_pce_price_index` | PCE ex food & energy | NIPA T20804 | M | log diff |
| `us_personal_income` | Personal income | NIPA T20600 | M | log diff |
| `state_real_gdp.<FIPS>` | Real GDP by state (all states + DC + regions) | Regional SQGDP9 | Q | log diff |
| `us_goods_balance_sa` / `_nsa` | Balance on goods, SA and NSA | ITA BalGds | Q | diff |

To add a series, add an entry to `config/series.yaml`. Use `params` to find table names, and either `select: {line: N}` or `select: {match: "text"}` to pick the row.

## Output format

`data/processed/observations.csv`: one row per data point.

| series_id | date | value |
|---|---|---|

Dates are period starts: 2024Q3 becomes 2024-07-01, and 2024M7 also becomes 2024-07-01.

`data/processed/series_metadata.csv`: one row per series, with these columns:

- `source`, `country`, `geo`, `geo_code`
- `dataset`, `table`, `line`, `bea_series_code`, `description`, `category`
- `frequency`, `unit`, `unit_mult`, `seasonal_adjustment`, `transform`
- `first_date`, `last_date`, `n_obs`, `retrieved`

`value` is exactly as BEA publishes it; multiply by `10**unit_mult` for base units.

`reports/profile.csv`: one row per series. All stats are on the transformed series.

| Column | Meaning |
|---|---|
| `n_obs`, `years`, `start`, `end` | Length |
| `missing_frac` | Share of periods missing inside the observed span |
| `std` | Volatility |
| `acf1`, `half_life_periods` | Persistence |
| `acf_seasonal`, `seasonality_strength` | Seasonality (share of variance explained by month/quarter; about 0 if seasonally adjusted) |
| `max_mean_shift_t`, `max_mean_shift_date` | Break screen: largest mean-shift t-stat. Above about 5 is worth a look; this is not a formal test |

## Things to know

- **Latest data only.** The API returns the latest revised numbers, not what was known in real time. For real-time vintages use ALFRED or the Philadelphia Fed Real-Time Data Set.
- **SAAR.** Quarterly NIPA values are seasonally adjusted annual rates; don't adjust them again.
- **Rate limits.** BEA allows 100 requests/min. The client spaces calls about 0.75 s apart and backs off on HTTP 429.
- **Key safety.** Your API key stays in `.env`, which is git-ignored, and is stripped from cached responses.

## Layout

```
config/series.yaml         series to collect
src/bea_corpus/
  client.py                API client (rate limiting, retries, cache)
  standardize.py           BEA response -> observations + metadata
  profile.py               series statistics
  cli.py                   command line
tests/                     offline tests (network stubbed)
docs/bea_overview.md       what BEA is and what's in it
```

## Tests

```bash
pytest
```

## Next steps

- Add more NIPA tables and state income (SQINC1).
- Pull matching series from other countries' sources into the same format.
- Add forecasting baselines (random walk, AR) and relative RMSE / MASE.
