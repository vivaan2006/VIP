# BEA coverage check

Run on 2026-11-09. For each dataset: the full dictionary, then every series in the chosen tables. `Checked` says how we know a table is complete. `Requests with data` counts every request made, including ones that tried a geography or setting BEA doesn't publish.

| Dataset | Status | Dictionary params listed | Tables in dataset | Downloaded | Requests with data | Series | Observations | MB |
|---|---|---|---|---|---|---|---|---|
| NIUnderlyingDetail | ok | 4/4 | 58 | 3 | 3/3 | 262 | 51556 | 11.6 |
| MNE | issues | 12/12 | 57 | 3 | 12/75 | 4217 | 49941 | 26.1 |
| Regional | ok | 4/4 | 105 | 3 | 110/127 | 15141 | 390864 | 59.3 |

**19620 series** downloaded in total.

## Tables

| Dataset | Table | Series | Requests with data | Checked | Missing |
|---|---|---|---|---|---|
| NIUnderlyingDetail | U001A | 128 | 1/1 | whole table in one call | - |
| NIUnderlyingDetail | U50205 | 56 | 1/1 | whole table in one call | - |
| NIUnderlyingDetail | U90100 | 78 | 1/1 | whole table in one call | - |
| MNE | 16 | 0 | 0/25 | whole table in one call | - |
| MNE | 53 | 1135 | 5/25 | whole table in one call | - |
| MNE | 6 | 3082 | 7/25 | whole table in one call | - |
| Regional | CAGDP1 | 9561 | 6/11 | every line in the dictionary, at COUNTY, STATE | 0 of 3 |
| Regional | SAGDP9 | 5520 | 92/98 | every line in the dictionary, at STATE | 0 of 92 |
| Regional | TASUMMARY2 | 60 | 12/18 | every line in the dictionary, at STATE | 0 of 12 |

## Issues

- **error** MNE / 16: No data with any of the 25 request shapes tried.
- **info** MNE / 53: 8 code(s) carry several measures told apart only by their labels; split into separate series.
- **info** MNE / 53: 37 of 1135 series have no values at all (every value suppressed or not available).
- **info** MNE / 6: 2 series changed names over time; each kept as one history with every name in label_history.
- **info** MNE / 6: 10 code(s) carry several measures told apart only by their labels; split into separate series.
- **info** MNE / 6: 92 of 3082 series have no values at all (every value suppressed or not available).
