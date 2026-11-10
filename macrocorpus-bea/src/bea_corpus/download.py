"""Download every BEA dataset's dictionary, plus every series in a sample of its tables.

One command does all of it (`python -m bea_corpus download`, also the default command):

  0. coverage   GetDataSetList: every BEA dataset must be in config/datasets.yaml
  for each dataset:
  1. dictionary GetParameterList + GetParameterValues for every parameter
                -> data/corpus/<Dataset>/dictionary.json
  2. tables     `sample_size` tables spread across the dataset's table list (or the ones in `tables`);
                `python run.py` uses 1000, i.e. every table
  3. series     EVERY series in each sampled table: all lines, all years, all frequencies
                -> data/corpus/<Dataset>/series/<table>.json   (standard JSON, one record per series)
                raw BEA responses -> data/raw/<Dataset>/<table>.json
  4. check      compare what came back with what the dictionary says the table contains
  5. report     reports/coverage.md, reports/coverage.json, reports/issues.json

Errors never stop the run. Each one is logged in reports/issues.json and the run moves on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .client import BEAClient, BEAError
from .common import as_list, desc_of, key_of, safe_name
from .standardize_json import table_to_series

DATA = Path("data")
CORPUS = DATA / "corpus"
RAW = DATA / "raw"
REPORTS = Path("reports")

NOT_A_DATASET = {"apidatasetmetadata"}  # listed by GetDataSetList but holds no data
YEAR_CHUNK = 10  # when a whole-table request comes back empty, years per smaller request


@dataclass
class Issue:
    dataset: str
    table: str
    level: str  # error = nothing usable came back, warning = data may be incomplete, info = worth knowing
    issue: str


@dataclass
class TableResult:
    table: str
    description: str
    requests: int = 0
    requests_ok: int = 0
    series: int = 0
    observations: int = 0
    empty_series: int = 0
    mb: float = 0.0
    check: str = "whole table in one call"
    expected: int | None = None
    missing: list[str] = field(default_factory=list)


@dataclass
class DatasetResult:
    dataset: str
    table_param: str
    tables_in_dataset: int = 0
    dictionary_params: int = 0
    dictionary_params_listed: int = 0
    tables: list[TableResult] = field(default_factory=list)

    @property
    def series(self) -> int:
        return sum(t.series for t in self.tables)


# ---------- helpers ----------

def pick_evenly(items: list, n: int) -> list:
    """n items spread across the list (first, ..., last) so the sample covers the whole catalog."""
    if n >= len(items):
        return list(items)
    if n <= 1:
        return items[:1]
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def regional_geographies(table: str) -> list[str]:
    """Every geography level to try for a Regional table. ALL of the ones that return data are kept.

    County tables (CA...) come at county level and are also published for states, metro and
    micropolitan areas, metro divisions, combined areas and metro/nonmetro portions. State tables
    (SA..., SQ...) are tried at every level too; the ones BEA doesn't publish just come back empty.
    """
    t = table.upper()
    if t.startswith("PORT"):
        return ["PORT"]
    if t.startswith("MA"):
        return ["MSA", "MIC", "DIV", "CSA", "PORT", "STATE"]
    if t.startswith("CA"):
        return ["COUNTY", "STATE", "MSA", "MIC", "DIV", "CSA", "PORT"]
    return ["STATE", "COUNTY", "MSA", "MIC", "DIV", "CSA", "PORT"]


REGIONAL_LAST_RESORT = "00000"  # national total, only if no geography level works at all


def _write_json(path: Path, obj, indent: int | None = 1) -> None:
    """Write via a temp file and rename, so a run that's interrupted never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=False))
    tmp.replace(path)


def _has_rows(got: tuple[dict, dict] | None) -> bool:
    return bool(got and as_list(got[1].get("Data")))


def _fill_required(param_list: list[dict], params: dict, skip: set[str]) -> dict:
    """Add every required parameter we didn't set, using the dataset's 'all' value."""
    out = dict(params)
    given = {k.lower() for k in out} | {s.lower() for s in skip} | {"userid", "method", "datasetname"}
    for p in param_list:
        name = p.get("ParameterName", "").strip()
        if str(p.get("ParameterIsRequiredFlag")) == "1" and name.lower() not in given:
            all_value = p.get("AllValue") or p.get("ParameterDefaultValue")
            if all_value:
                out[name] = all_value
    return out


# ---------- the pipeline ----------

class Downloader:
    def __init__(self, client: BEAClient, config: dict, use_cache: bool = True):
        self.client = client
        self.defaults = config.get("defaults", {})
        self.datasets: dict = config["datasets"]
        self.use_cache = use_cache
        self.issues: list[Issue] = []
        self._last_error = ""
        self._split_values: list[str] = []
        self._years: list[str] = []

    def log(self, dataset: str, table: str, level: str, issue: str) -> None:
        self.issues.append(Issue(dataset, table, level, issue))

    # 0. coverage
    def check_dataset_coverage(self) -> None:
        try:
            listed = {d["DatasetName"] for d in self.client.get_dataset_list()}
        except BEAError as exc:
            self.log("ALL", "", "warning", f"Could not get BEA's dataset list to check coverage: {exc}")
            return
        configured = {name.lower() for name in self.datasets}
        for name in sorted(listed):
            if name.lower() not in configured | NOT_A_DATASET:
                self.log(name, "", "error", "BEA dataset is not in config/datasets.yaml, so it is not downloaded.")
        bea = {n.lower() for n in listed}
        for name in self.datasets:
            if name.lower() not in bea:
                self.log(name, "", "warning", "In config/datasets.yaml but not in BEA's dataset list.")

    # 1. dictionary
    def download_dictionary(self, dataset: str) -> tuple[list[dict], dict[str, list]]:
        params = self.client.get_parameter_list(dataset)
        values: dict[str, list] = {}
        failed: dict[str, str] = {}
        for p in params:
            name = p.get("ParameterName", "").strip()
            try:
                values[name] = as_list(self.client.get_parameter_values(dataset, name))
            except BEAError as exc:
                failed[name] = str(exc)
        if failed:
            self.log(dataset, "", "info", f"BEA can't list values for parameter(s) {', '.join(failed)} "
                                          "on their own (they depend on the table).")
        _write_json(CORPUS / dataset / "dictionary.json", {
            "dataset": dataset, "retrieved": date.today().isoformat(), "parameters": params,
            "values": values, "could_not_list": failed})
        return params, values

    # 2-4. one dataset
    def download_dataset(self, dataset: str) -> DatasetResult:
        cfg = self.datasets[dataset]
        table_param = cfg["table_param"]
        result = DatasetResult(dataset, table_param)
        try:
            param_list, values = self.download_dictionary(dataset)
        except BEAError as exc:
            self.log(dataset, "", "error", f"Could not get the parameter list: {exc}")
            return result
        result.dictionary_params = len(param_list)
        result.dictionary_params_listed = len(values)

        # BEA lists an "all" placeholder next to the real tables in some datasets (MNE SeriesID 0); skip it
        catalog = {key_of(v): desc_of(v) for v in values.get(table_param, [])
                   if key_of(v).lower() != "all" and desc_of(v).strip().lower() != "all"}
        result.tables_in_dataset = len(catalog)
        if not catalog:
            self.log(dataset, "", "error", f"The dictionary has no values for {table_param}, so no tables to sample.")
            return result

        chosen = [str(t) for t in cfg.get("tables", [])] or pick_evenly(
            list(catalog), int(cfg.get("sample_size", self.defaults.get("sample_size", 5))))
        for table in chosen:
            if table not in catalog:
                self.log(dataset, table, "warning", f"Chosen table is not in the dictionary's {table_param} list.")
        base = _fill_required(param_list, cfg.get("params", {}), skip={table_param, "LineCode", "GeoFips",
                                                                        cfg.get("split_by", "")})
        self._split_values = [key_of(v) for v in values.get(cfg.get("split_by", ""), [])
                              if key_of(v) and key_of(v).lower() != "all"]
        self._years = sorted({key_of(v) for v in values.get("Year", []) if key_of(v).isdigit()})

        for table in chosen:
            t = self.download_table(dataset, cfg, table, catalog.get(table, ""), {**base, table_param: table})
            result.tables.append(t)
            print(f"  {_status_word(t):<7} {table:<28} {t.series:>6} series  {t.requests_ok}/{t.requests} requests"
                  f"  {t.mb:>7.2f} MB")
        if result.tables and result.series == 0:
            self.log(dataset, "", "error", "No series came back from any sampled table.")
        return result

    def download_table(self, dataset: str, cfg: dict, table: str, description: str, params: dict) -> TableResult:
        t = TableResult(table, description)
        if dataset == "Regional":
            responses = self._regional_table(t, params)
        elif cfg.get("split_by"):
            responses = self._split(dataset, t, params, cfg["split_by"], self._split_values)
        elif cfg.get("request_shapes"):
            responses = self._all_shapes(dataset, t, params, cfg["request_shapes"])
        else:
            responses = self._whole_table(dataset, t, params)

        if dataset != "Regional" and cfg.get("check") and responses:
            self._check_against_dictionary(dataset, cfg, table, t, responses)

        try:
            records, stats = table_to_series(responses, dataset, table, identity_params=cfg.get("identity_params"))
        except Exception as exc:  # one odd response shouldn't stop the whole run
            records, stats = [], {}
            self.log(dataset, table, "error", f"Couldn't turn BEA's response into series: {exc!r:.200}")
        t.series = len(records)
        t.observations = sum(r["n_obs"] for r in records)
        t.empty_series = sum(1 for r in records if r["n_obs"] == 0)
        self._log_stats(dataset, table, stats)

        if responses and not records:
            self.log(dataset, table, "error", "BEA answered but no data rows came back, even in smaller requests.")
        if t.empty_series:
            self.log(dataset, table, "info", f"{t.empty_series} of {t.series} series have no values at all "
                                             "(every value suppressed or not available).")
        raw_file = RAW / dataset / f"{safe_name(table)}.json"
        series_file = CORPUS / dataset / "series" / f"{safe_name(table)}.json"
        if records:
            _write_json(raw_file, {"dataset": dataset, "table": table, "responses": [
                {"request": req, "results": res} for req, res in responses]}, indent=None)
            _write_json(series_file, {
                "dataset": dataset, "table": table, "table_description": description,
                "retrieved": date.today().isoformat(), "n_series": len(records), "series": records})
        else:  # don't leave an older run's file around looking like this run's result
            for old in (raw_file, series_file):
                old.unlink(missing_ok=True)
        return t

    def _log_stats(self, dataset: str, table: str, stats: dict) -> None:
        if stats.get("bad_time"):
            self.log(dataset, table, "warning", f"{stats['bad_time']} rows had a time period we couldn't read.")
        if stats.get("conflicts"):
            self.log(dataset, table, "warning",
                     f"{stats['conflicts']} times BEA sent two different numbers for the same series and date. "
                     f"Kept the first. e.g. {'; '.join(stats.get('conflict_examples', [])[:3])}")
        if stats.get("renamed"):
            self.log(dataset, table, "info", f"{stats['renamed']} series changed names over time; each kept as one "
                                             "history with every name in label_history.")
        if stats.get("measure_splits"):
            self.log(dataset, table, "info", f"{stats['measure_splits']} code(s) carry several measures told apart "
                                             "only by their labels; split into separate series.")
        if stats.get("unit_changes"):
            self.log(dataset, table, "warning", f"{stats['unit_changes']} series change units partway through; "
                                                "see unit_history.")

    def _get(self, dataset: str, t: TableResult, request: dict, probe: bool = False) -> tuple[dict, dict] | None:
        """One GetData call. Returns (request, results), or None after logging the error.

        Every call is counted in t.requests; only calls that returned data rows count as ok.
        probe=True is for trying out a request shape (e.g. which geography a table has):
        a failure there is expected, so it isn't logged.
        """
        t.requests += 1
        try:
            results = self.client.get_data(dataset, use_cache=self.use_cache, **request)
        except Exception as exc:  # API errors and network errors alike: log and move on
            self._last_error = str(exc)
            if not probe:
                self.log(dataset, t.table, "error", f"GetData failed for {_short(request)}: {str(exc)[:200]}")
            return None
        t.mb += self.client.last_bytes / (1024 * 1024)
        if as_list(results.get("Data")):
            t.requests_ok += 1
        return request, results

    def _whole_table(self, dataset: str, t: TableResult, params: dict) -> list[tuple[dict, dict]]:
        """The whole table in one call. If BEA sends it back empty or refuses it, ask in smaller pieces:
        one frequency at a time, then ten years at a time. (NIUnderlyingDetail U20403-U20405 come back
        empty for Frequency=A,Q,M but have data one frequency at a time.)"""
        self._last_error = ""
        got = self._get(dataset, t, params, probe=True)
        if _has_rows(got):
            return [got]
        first_problem = self._last_error or "came back empty"
        responses = self._pieces(dataset, t, params, stage=0)
        if responses:
            self.log(dataset, t.table, "info", f"Whole-table request {first_problem[:150]}; got the data in "
                                               f"{len(responses)} smaller requests instead.")
        else:
            self.log(dataset, t.table, "error", f"Whole-table request {first_problem[:200]}, and smaller requests "
                                                "(one frequency / ten years at a time) had no data either.")
        return responses

    def _pieces(self, dataset: str, t: TableResult, params: dict, stage: int) -> list[tuple[dict, dict]]:
        freqs = [f for f in str(params.get("Frequency", "")).split(",") if f]
        year_all = str(params.get("Year", "")).upper() in ("ALL", "X")
        if stage == 0 and len(freqs) > 1:
            parts, next_stage = [{**params, "Frequency": f} for f in freqs], 1
        elif stage <= 1 and year_all and self._years:
            chunks = [self._years[i:i + YEAR_CHUNK] for i in range(0, len(self._years), YEAR_CHUNK)]
            parts, next_stage = [{**params, "Year": ",".join(c)} for c in chunks], 2
        else:
            return []
        out = []
        for part in parts:
            got = self._get(dataset, t, part, probe=True)
            if _has_rows(got):
                out.append(got)
            elif next_stage < 2:
                out += self._pieces(dataset, t, part, next_stage)
        return out

    def _split(self, dataset: str, t: TableResult, params: dict, dim: str,
               split_values: list[str]) -> list[tuple[dict, dict]]:
        """One request per value of `dim` (IntlServSTA refuses every country, industry and year at once)."""
        t.check, t.expected = f"one request per {dim} in the dictionary", len(split_values)
        if not split_values:
            self.log(dataset, t.table, "error", f"The dictionary lists no {dim} values to split the table by.")
            return []
        responses = []
        for value in split_values:
            got = self._get(dataset, t, {**params, dim: value}, probe=True)
            if got and as_list(got[1].get("Data")):
                responses.append(got)
            else:
                t.missing.append(value)
        if not responses:
            self.log(dataset, t.table, "error", f"None of the {len(split_values)} {dim} values returned data.")
        elif t.missing:
            self.log(dataset, t.table, "info", f"{len(t.missing)} of {len(split_values)} {dim} values have no data "
                                               f"for this table (e.g. {', '.join(t.missing[:5])}).")
        return responses

    def _all_shapes(self, dataset: str, t: TableResult, params: dict,
                    shapes: list[dict]) -> list[tuple[dict, dict]]:
        """Try every request shape and keep ALL that return data.

        MNE needs this: each series exists for some directions of investment, classifications and
        ownership levels and not others (BEA errors on the rest), and many exist for several, e.g.
        employment of foreign affiliates of U.S. companies (outward) AND of U.S. affiliates of foreign
        companies (inward). Stopping at the first that works would drop the others.
        """
        responses = []
        for extra in shapes:
            got = self._get(dataset, t, {**params, **extra}, probe=True)
            if _has_rows(got):
                responses.append(got)
        if not responses:
            self.log(dataset, t.table, "error", f"No data with any of the {len(shapes)} request shapes tried.")
        return responses

    def _regional_table(self, t: TableResult, params: dict) -> list[tuple[dict, dict]]:
        """Regional returns one line of a table per call, so ask for every line the dictionary lists."""
        table = t.table
        try:
            lines = [key_of(v) for v in as_list(
                self.client.get_parameter_values_filtered("Regional", "LineCode", TableName=table))]
        except BEAError as exc:
            self.log("Regional", table, "error", f"Could not list the table's lines: {exc}")
            return []
        lines = [ln for ln in dict.fromkeys(lines) if ln]
        t.check, t.expected = "every line in the dictionary", len(lines)
        if not lines:
            self.log("Regional", table, "error", "The dictionary lists no lines for this table.")
            return []

        # find EVERY geography level the table has: probe each on the first line (or the next two lines,
        # if the first has no data anywhere), then keep all that returned data
        candidates = regional_geographies(table)
        responses, working = [], []
        for probe_lines in (lines[:1], lines[1:3]):
            for geo in candidates:
                for line in probe_lines:
                    got = self._get("Regional", t, {**params, "LineCode": line, "GeoFips": geo}, probe=True)
                    if _has_rows(got):
                        working.append(geo)
                        responses.append(got)
                        break
            if working:
                break
        if not working:
            got = self._get("Regional", t, {**params, "LineCode": lines[0], "GeoFips": REGIONAL_LAST_RESORT},
                            probe=True)
            if _has_rows(got):
                working, responses = [REGIONAL_LAST_RESORT], [got]
        if not working:
            t.missing = lines
            self.log("Regional", table, "error", f"No geography level ({', '.join(candidates)}) returned data "
                                                 "for the table's first lines.")
            return []
        t.check = f"every line in the dictionary, at {', '.join(working)}"
        done = {(r["LineCode"], r["GeoFips"]) for r, _ in responses}
        empty = 0
        for geo in working:
            for line in lines:
                if (line, geo) in done:
                    continue
                got = self._get("Regional", t, {**params, "LineCode": line, "GeoFips": geo}, probe=True)
                if _has_rows(got):
                    responses.append(got)
                else:
                    empty += 1
        returned = {r["LineCode"] for r, res in responses if as_list(res.get("Data"))}
        t.missing = [ln for ln in lines if ln not in returned]
        if t.missing:
            self.log("Regional", table, "warning", f"{len(t.missing)} of {len(lines)} lines returned no data at any "
                                                   f"geography level (e.g. {', '.join(t.missing[:5])}).")
        if empty:
            self.log("Regional", table, "info", f"{empty} line x geography requests had no data (not every line "
                                                "is published at every level).")
        return responses

    def _check_against_dictionary(self, dataset: str, cfg: dict, table: str, t: TableResult,
                                  responses: list[tuple[dict, dict]]) -> None:
        """Compare the values of `check` in the data with the dictionary's list for this table.

        Anything missing is asked for on its own, in case BEA left it out of the ALL request.
        Whatever still has no data after that is logged.
        """
        dim = cfg["check"]
        try:
            listed = {key_of(v) for v in as_list(self.client.get_parameter_values_filtered(
                dataset, dim, **{cfg["table_param"]: table}))}
        except BEAError as exc:
            self.log(dataset, table, "info", f"BEA can't list {dim} per table here ({str(exc)[:120]}); "
                                             "checked as a whole-table call instead.")
            return
        found = {str(row.get(dim, "")).strip() for _, res in responses for row in as_list(res.get("Data"))}
        t.check, t.expected = f"every {dim} in the dictionary", len(listed)
        missing = sorted(listed - found)
        t.missing = self._ask_one_by_one(dataset, t, responses[0][0], dim, missing, responses)
        recovered = len(missing) - len(t.missing)
        if recovered:
            self.log(dataset, table, "info", f"{recovered} {dim} value(s) were left out of the ALL request "
                                             "but came back when asked for on their own.")
        if t.missing:
            self.log(dataset, table, "warning", f"{len(t.missing)} of {len(listed)} {dim} values in the dictionary "
                                                f"have no data, even when asked for on their own "
                                                f"(e.g. {', '.join(t.missing[:5])}).")

    def _ask_one_by_one(self, dataset: str, t: TableResult, request: dict, dim: str, missing: list[str],
                        responses: list[tuple[dict, dict]]) -> list[str]:
        """Re-request every missing value alone. Returns what still has no data."""
        still = []
        for value in missing:
            got = self._get(dataset, t, {**request, dim: value}, probe=True)  # BEA rejects some listed values
            if _has_rows(got):
                responses.append(got)
            else:
                still.append(value)
        return still

    # all datasets
    def run(self, only: list[str] | None = None) -> list[DatasetResult]:
        if not only:
            self.check_dataset_coverage()
        results = []
        for name in self.datasets:
            if only and name not in only:
                continue
            print(f"\n== {name}")
            results.append(self.download_dataset(name))
        write_reports(results, self.issues, partial=bool(only))
        return results


def _short(request: dict) -> str:
    keep = {k: v for k, v in request.items() if str(v).upper() not in ("ALL", "X")}
    return ", ".join(f"{k}={v}" for k, v in keep.items()) or "all"


def _status_word(t: TableResult) -> str:
    if not t.series:
        return "FAIL"
    return "partial" if t.missing else "ok"


def dataset_status(d: DatasetResult, issues: list[Issue]) -> str:
    levels = {i.level for i in issues if i.dataset == d.dataset}
    if d.series == 0:
        return "failed"
    return "issues" if levels & {"error", "warning"} else "ok"


# ---------- reports ----------

def write_reports(results: list[DatasetResult], issues: list[Issue], partial: bool = False) -> None:
    """Write coverage.json, issues.json and coverage.md.

    partial=True (a run with --only): the datasets that were just run replace their old entries and
    everything else from the last report is kept, so a quick re-run of one dataset doesn't wipe the rest.
    """
    REPORTS.mkdir(exist_ok=True)
    summary = [{**{k: v for k, v in asdict(d).items() if k != "tables"},
                "status": dataset_status(d, issues), "series": d.series,
                "tables": [asdict(t) for t in d.tables]} for d in results]
    issue_rows = [asdict(i) for i in issues]
    if partial and (REPORTS / "coverage.json").exists():
        rerun = {d.dataset for d in results}
        try:
            old = json.loads((REPORTS / "coverage.json").read_text()).get("datasets", [])
            old_issues = json.loads((REPORTS / "issues.json").read_text())
        except (ValueError, OSError):
            old, old_issues = [], []
        summary = [d for d in old if d["dataset"] not in rerun] + summary
        issue_rows = [i for i in old_issues if i["dataset"] not in rerun] + issue_rows
    _write_json(REPORTS / "coverage.json", {"run": date.today().isoformat(), "datasets": summary}, indent=2)
    _write_json(REPORTS / "issues.json", issue_rows, indent=2)

    lines = [
        "# BEA coverage check",
        "",
        f"Run on {date.today().isoformat()}. For each dataset: the full dictionary, then every series in "
        "the chosen tables. `Checked` says how we know a table is complete. `Requests with data` counts "
        "every request made, including ones that tried a geography or setting BEA doesn't publish.",
        "",
        "| Dataset | Status | Dictionary params listed | Tables in dataset | Downloaded | Requests with data | "
        "Series | Observations | MB |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for d in summary:
        tables = d["tables"]
        ok = sum(t["requests_ok"] for t in tables)
        req = sum(t["requests"] for t in tables)
        obs = sum(t["observations"] for t in tables)
        mb = sum(t["mb"] for t in tables)
        lines.append(f"| {d['dataset']} | {d['status']} | {d['dictionary_params_listed']}/"
                     f"{d['dictionary_params']} | {d['tables_in_dataset']} | {len(tables)} | {ok}/{req} | "
                     f"{d['series']} | {obs} | {mb:.1f} |")
    total = sum(d["series"] for d in summary)
    lines += ["", f"**{total} series** downloaded in total.", "", "## Tables", "",
              "| Dataset | Table | Series | Requests with data | Checked | Missing |", "|---|---|---|---|---|---|"]
    for d in summary:
        for t in d["tables"]:
            missing = f"{len(t['missing'])} of {t['expected']}" if t["expected"] is not None else "-"
            lines.append(f"| {d['dataset']} | {t['table']} | {t['series']} | {t['requests_ok']}/{t['requests']} | "
                         f"{t['check']} | {missing} |")
    lines += ["", "## Issues", ""]
    order = {"error": 0, "warning": 1, "info": 2}
    lines += [f"- **{i['level']}** {i['dataset']}{' / ' + i['table'] if i['table'] else ''}: {i['issue']}"
              for i in sorted(issue_rows, key=lambda i: (order[i["level"]], i["dataset"]))] or ["None."]
    (REPORTS / "coverage.md").write_text("\n".join(lines) + "\n")

    print("\n" + "\n".join(lines[4:4 + len(summary) + 2]))
    counts = {lvl: sum(1 for i in issue_rows if i["level"] == lvl) for lvl in order}
    print(f"\n{total} series. Issues: {counts['error']} errors, {counts['warning']} warnings, {counts['info']} info.")
    print("Wrote data/corpus/, reports/coverage.md, reports/coverage.json, reports/issues.json")


# ---------- entry points ----------

def run(config_path: str | Path = "config/datasets.yaml", only: list[str] | None = None,
        sample_size: int | None = None, use_cache: bool = True,
        client: BEAClient | None = None) -> list[DatasetResult]:
    config = yaml.safe_load(Path(config_path).read_text())
    if sample_size:
        config.setdefault("defaults", {})["sample_size"] = sample_size
        for cfg in config["datasets"].values():
            cfg.pop("sample_size", None)
            cfg.pop("tables", None)  # a hand-picked list would cap the dataset below sample_size
    return Downloader(client or BEAClient(), config, use_cache=use_cache).run(only)


def restandardize(config_path: str | Path = "config/datasets.yaml") -> dict:
    """Rebuild data/corpus/*/series/ from the raw responses on disk (no API calls).

    Handy when we change the JSON format: edit standardize_json.py, run this, done.
    """
    if not RAW.exists():
        raise SystemExit("No data/raw/ yet. Run `python -m bea_corpus download` first.")
    datasets = yaml.safe_load(Path(config_path).read_text()).get("datasets", {})
    counts = {}
    for ds_dir in sorted(p for p in RAW.iterdir() if p.is_dir() and p.name != "cache"):
        n = 0
        identity = datasets.get(ds_dir.name, {}).get("identity_params")
        for raw in sorted(ds_dir.glob("*.json")):
            blob = json.loads(raw.read_text())
            if "responses" not in blob:  # left over from the old layout
                continue
            records, _ = table_to_series([(r["request"], r["results"]) for r in blob["responses"]],
                                         blob["dataset"], blob["table"], identity_params=identity)
            out = CORPUS / blob["dataset"] / "series" / raw.name
            old = json.loads(out.read_text()) if out.exists() else {}
            _write_json(out, {**old, "dataset": blob["dataset"], "table": blob["table"],
                              "n_series": len(records), "series": records})
            n += len(records)
        counts[ds_dir.name] = n
        print(f"  {ds_dir.name:<26} {n:>6} series")
    return counts
