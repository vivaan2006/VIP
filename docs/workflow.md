# How the BEA download works

What `python run.py` does from start to finish, and where to look when something goes wrong. Function names are in `macrocorpus-bea/src/bea_corpus/`; most of it is `download.py`.

## 1. The whole run

```mermaid
flowchart TD
    A["python run.py"] --> B["installs packages"]
    B --> C["BEA key in macrocorpus-bea/.env<br/>(asks once if missing)"]
    C --> D["preflight: one quick call<br/>bad key or no internet stops here"]
    D --> E["python -m bea_corpus download<br/>(cli.py), output also saved to reports/run_log.txt"]
    E --> F["check_dataset_coverage<br/>BEA's dataset list vs config/datasets.yaml"]
    F --> G["for each of the 12 datasets"]
    G --> H["download_dictionary<br/>every parameter and all its values<br/>data/corpus/DATASET/dictionary.json"]
    H --> I["pick tables<br/>all of them, or --sample N spread across the list"]
    I --> J["download_table<br/>(section 2)"]
    J --> K["table_to_series<br/>BEA rows into series JSON (section 5)"]
    K --> L["write files<br/>data/raw/DATASET/TABLE.json<br/>data/corpus/DATASET/series/TABLE.json"]
    L --> I
    G --> M["write_reports<br/>reports/coverage.md, coverage.json, issues.json"]
```

Every request goes through `client.py` (`BEAClient`). It keeps under BEA's limits (100 requests, 100 MB and 30 errors a minute), saves every answered response in `data/raw/cache/`, and retries network trouble before giving up.

## 2. One table: which route it takes

The route depends on the dataset's block in `config/datasets.yaml`.

```mermaid
flowchart TD
    A["download_table"] --> B{"which dataset?"}
    B -->|"Regional"| R["_regional_table<br/>list the table's lines, find every geography level<br/>that has data, then ask for every line x level"]
    B -->|"config has split_by<br/>(IntlServSTA)"| S["_split<br/>one request per country"]
    B -->|"config has request_shapes<br/>(MNE)"| M["_all_shapes<br/>every request shape that applies (section 3)"]
    B -->|"everything else"| W["_whole_table<br/>the whole table in one call"]
    W -->|"empty or no answer"| P["_pieces<br/>one frequency at a time,<br/>then ten years at a time"]
    R --> C{"config has check?<br/>(GDPbyIndustry, UnderlyingGDPbyIndustry,<br/>ITA, IntlServTrade)"}
    S --> C
    M --> C
    W --> C
    P --> C
    C -->|"yes"| D["_check_against_dictionary<br/>compare with BEA's list of what the table has,<br/>ask for anything missing on its own"]
    C -->|"no"| E["table_to_series"]
    D --> E
```

## 3. MNE: the request shapes

Each MNE series only exists for some directions (outward, inward, parent, state), classifications and ownership levels. So every shape that applies is tried and every one with data is kept.

```mermaid
flowchart TD
    A["_all_shapes for one series"] --> B["expand_shapes<br/>the original 25 (request_shapes), then the extra ones<br/>(extra_request_shapes) whose series list includes it"]
    B --> C{"for each shape:<br/>BEA turned it down in the last 60 days?<br/>(data/raw/cache/shapes_MNE.json)"}
    C -->|"yes"| SK["skip it"]
    C -->|"no, or --refresh"| Q["ask BEA"]
    Q --> R{"what came back?"}
    R -->|"rows"| K["keep it"]
    R -->|"BEA said no / no rows"| N["remember it, skip next run"]
    R -->|"no answer<br/>(network, throttling, BEA down)"| U["NOT remembered<br/>warning, tried again next run"]
    R -->|"bug on our side"| X["error in issues.json<br/>NOT remembered"]
    K --> V["every 20 shapes and at the end:<br/>save shapes_MNE.json<br/>and reports/shapes_MNE.json"]
    N --> V
    U --> V
    X --> V
    SK --> V
```

The 9 series BEA lists as discontinued (11, 13, 16, 19, 50, 51, 52, 55, 56) are logged as `info`, not `error`, when nothing comes back.

## 4. What happens to every request

This is the part that matters most for debugging. "BEA said no" and "BEA didn't answer" are handled differently on purpose.

```mermaid
flowchart LR
    A["a request"] --> B{"result"}
    B -->|"rows"| D["data"]
    B -->|"BEAError<br/>BEA says the combination or table doesn't exist"| N["no data"]
    B -->|"BEATransientError / is_transient<br/>network, 403, 408, 429, 5xx, not JSON,<br/>BEA throttle message (code 7)"| U["no answer<br/>counted in unanswered"]
    B -->|"any other exception"| X["bug: logged as an error"]
    U --> K{"after the table:<br/>missing any series or date<br/>the last download had?"}
    K -->|"yes"| KEEP["keep the last download<br/>table shows partial, kept previous download"]
    K -->|"no"| WRITE["write the new result<br/>table shows partial"]
```

If BEA doesn't answer when the dictionary asks for one parameter's values, it's named under `no_answer` in `dictionary.json` and the list from the last run is used if there is one. If there's no old list, any table that needed it keeps its last download.

## 5. BEA rows into series (`table_to_series` in standardize_json.py)

1. Group rows by their code columns plus frequency. Labels don't split a series, so a renamed country stays one series and the names go in `label_history`.
2. If one code holds several measures told apart only by their labels, split them.
3. For MNE, older years (`NonbankAffiliatesOnly=1`) and newer years (`=0`) join into one history, and `definition_history` says where it switches.
4. Every date is put in order, and missing values become null.
5. `merge_identical`: series with exactly the same dates, values and units become one. The extra ids are listed under `also_as`. For MNE, which id stays on top goes: a series built only from the original 25 shapes, then one that joins original and extra shapes, then one from extra shapes only. Within each, the smallest id wins. So adding the extra shapes never takes an id away from a series that `main` already had.

## Where to look when something's wrong

| Symptom | Look at |
|---|---|
| A dataset or table failed | `reports/coverage.md` (status per table), then `reports/issues.json` for that table |
| "rerun to try again" / "didn't answer" | BEA was busy or the network dropped. Rerun: it skips everything already downloaded |
| An MNE series has no data | `reports/shapes_MNE.json`: every shape tried for it and BEA's exact reason for each "no" |
| `no_answer` in a `dictionary.json` | BEA didn't answer for that list this run, so it's last run's list (or missing) |
| A series id disappeared | its `also_as` in the series file: it was merged into an identical series |
| Odd jump in an MNE series | its `definition_history`: before the switch year it's nonbank affiliates only |
| Want BEA asked again from scratch | `--refresh` (ignores saved responses and remembered refusals) |
| Changed the JSON format | `python -m bea_corpus standardize` rebuilds it from `data/raw/` with no downloading |
| What exactly was run and printed | `reports/run_log.txt` |
