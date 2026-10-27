"""Download every BEA dataset's dictionary, plus every series in a sample of its tables.

One command does all of it (`python -m bea_corpus download`, also the default command):

  0. coverage   GetDataSetList: every BEA dataset must be in config/datasets.yaml
  for each dataset:
  1. dictionary GetParameterList + GetParameterValues for every parameter
                -> data/corpus/<Dataset>/dictionary.json
  2. sample     `sample_size` tables spread across the dataset's table list (or the ones in `tables`)
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
from .standardize_json import to_series

DATA = Path("data")
CORPUS = DATA / "corpus"
RAW = DATA / "raw"
REPORTS = Path("reports")

NOT_A_DATASET = {"apidatasetmetadata"}  # listed by GetDataSetList but holds no data


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
    """Geography to request for a Regional table, best first. Metro tables need MSA, port tables PORT."""
    t = table.upper()
    if t.startswith("MA"):
        return ["MSA", "STATE", "00000"]
    if t.startswith("PORT"):
        return ["PORT", "00000"]
    return ["STATE", "00000"]


def _write_json(path: Path, obj, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=indent, ensure_ascii=False))


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

        catalog = {key_of(v): desc_of(v) for v in values.get(table_param, [])}
        result.tables_in_dataset = len(catalog)
        if not catalog:
            self.log(dataset, "", "error", f"The dictionary has no values for {table_param}, so no tables to sample.")
            return result

        chosen = [str(t) for t in cfg.get("tables", [])] or pick_evenly(
            list(catalog), int(cfg.get("sample_size", self.defaults.get("sample_size", 5))))
        for table in chosen:
            if table not in catalog:
                self.log(dataset, table, "warning", f"Chosen table is not in the dictionary's {table_param} list.")
        base = _fill_required(param_list, cfg.get("params", {}), skip={table_param, "LineCode", "GeoFips"})

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
        else:
            responses = [r for r in [self._get(dataset, t, params)] if r is not None]

        records = []
        for request, results in responses:
            recs, stats = to_series(results, dataset, table, request=request)
            records += recs
            if stats.get("bad_time"):
                self.log(dataset, table, "warning", f"{stats['bad_time']} rows had a time period we couldn't read.")
        t.series = len(records)
        t.observations = sum(r["n_obs"] for r in records)
        t.empty_series = sum(1 for r in records if r["n_obs"] == 0)

        if dataset != "Regional" and cfg.get("check"):
            self._check_against_dictionary(dataset, cfg, table, t, responses)

        if t.requests_ok and not records:
            self.log(dataset, table, "warning", "BEA answered but the table had no data rows.")
        if t.empty_series:
            self.log(dataset, table, "info", f"{t.empty_series} of {t.series} series have no values at all "
                                             "(every value suppressed or not available).")
        if responses:
            _write_json(RAW / dataset / f"{safe_name(table)}.json",
                        {"dataset": dataset, "table": table, "responses": [
                            {"request": req, "results": res} for req, res in responses]}, indent=None)
            _write_json(CORPUS / dataset / "series" / f"{safe_name(table)}.json", {
                "dataset": dataset, "table": table, "table_description": description,
                "retrieved": date.today().isoformat(), "n_series": len(records), "series": records})
        return t

    def _get(self, dataset: str, t: TableResult, request: dict) -> tuple[dict, dict] | None:
        """One GetData call. Returns (request, results), or None after logging the error."""
        t.requests += 1
        try:
            results = self.client.get_data(dataset, use_cache=self.use_cache, **request)
        except Exception as exc:  # API errors and network errors alike: log and move on
            self.log(dataset, t.table, "error", f"GetData failed for {_short(request)}: {str(exc)[:200]}")
            return None
        t.requests_ok += 1
        t.mb += self.client.last_bytes / (1024 * 1024)
        return request, results

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

        responses, geo = [], None
        for candidate in regional_geographies(table):  # find the geography the table supports
            first = self._get("Regional", t, {**params, "LineCode": lines[0], "GeoFips": candidate})
            if first:
                responses, geo = [first], candidate
                break
        if geo is None:
            t.missing = lines
            self.log("Regional", table, "error", "No geography worked for this table's first line.")
            return []
        for line in lines[1:]:
            got = self._get("Regional", t, {**params, "LineCode": line, "GeoFips": geo})
            if got:
                responses.append(got)
        returned = {r["LineCode"] for r, res in responses if as_list(res.get("Data"))}
        t.missing = [ln for ln in lines if ln not in returned]
        if t.missing:
            self.log("Regional", table, "warning", f"{len(t.missing)} of {len(lines)} lines returned no data "
                                                   f"(e.g. {', '.join(t.missing[:5])}).")
        return responses

    def _check_against_dictionary(self, dataset: str, cfg: dict, table: str, t: TableResult,
                                  responses: list[tuple[dict, dict]]) -> None:
        """Compare the values of `check` in the data with the dictionary's list for this table."""
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
        t.missing = sorted(listed - found)
        if t.missing:
            self.log(dataset, table, "warning", f"{len(t.missing)} of {len(listed)} {dim} values in the dictionary "
                                                f"have no data (e.g. {', '.join(t.missing[:5])}).")

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
        write_reports(results, self.issues)
        return results


def _short(request: dict) -> str:
    keep = {k: v for k, v in request.items() if str(v).upper() not in ("ALL", "X")}
    return ", ".join(f"{k}={v}" for k, v in keep.items()) or "all"


def _status_word(t: TableResult) -> str:
    if not t.requests_ok:
        return "FAIL"
    return "partial" if t.missing or t.requests_ok < t.requests else "ok"


def dataset_status(d: DatasetResult, issues: list[Issue]) -> str:
    levels = {i.level for i in issues if i.dataset == d.dataset}
    if d.series == 0:
        return "failed"
    return "issues" if levels & {"error", "warning"} else "ok"


# ---------- reports ----------

def write_reports(results: list[DatasetResult], issues: list[Issue]) -> None:
    REPORTS.mkdir(exist_ok=True)
    summary = [{**{k: v for k, v in asdict(d).items() if k != "tables"},
                "status": dataset_status(d, issues), "series": d.series,
                "tables": [asdict(t) for t in d.tables]} for d in results]
    _write_json(REPORTS / "coverage.json", {"run": date.today().isoformat(), "datasets": summary}, indent=2)
    _write_json(REPORTS / "issues.json", [asdict(i) for i in issues], indent=2)

    lines = [
        "# BEA coverage check",
        "",
        f"Run on {date.today().isoformat()}. For each dataset: the full dictionary, then every series in a sample "
        "of its tables. `Checked` says how we know a table is complete: either we compared it with the "
        "dictionary's list, or BEA returns the whole table in one call.",
        "",
        "| Dataset | Status | Dictionary params listed | Tables in dataset | Sampled | Requests ok | Series | "
        "Observations | MB |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for d in results:
        ok = sum(t.requests_ok for t in d.tables)
        req = sum(t.requests for t in d.tables)
        obs = sum(t.observations for t in d.tables)
        mb = sum(t.mb for t in d.tables)
        lines.append(f"| {d.dataset} | {dataset_status(d, issues)} | {d.dictionary_params_listed}/"
                     f"{d.dictionary_params} | {d.tables_in_dataset} | {len(d.tables)} | {ok}/{req} | "
                     f"{d.series} | {obs} | {mb:.1f} |")
    total = sum(d.series for d in results)
    lines += ["", f"**{total} series** downloaded in total.", "", "## Sampled tables", "",
              "| Dataset | Table | Series | Requests ok | Checked | Missing |", "|---|---|---|---|---|---|"]
    for d in results:
        for t in d.tables:
            missing = f"{len(t.missing)} of {t.expected}" if t.expected is not None else "-"
            lines.append(f"| {d.dataset} | {t.table} | {t.series} | {t.requests_ok}/{t.requests} | {t.check} | "
                         f"{missing} |")
    lines += ["", "## Issues", ""]
    order = {"error": 0, "warning": 1, "info": 2}
    lines += [f"- **{i.level}** {i.dataset}{' / ' + i.table if i.table else ''}: {i.issue}"
              for i in sorted(issues, key=lambda i: (order[i.level], i.dataset))] or ["None."]
    (REPORTS / "coverage.md").write_text("\n".join(lines) + "\n")

    print("\n" + "\n".join(lines[4:4 + len(results) + 2]))
    counts = {lvl: sum(1 for i in issues if i.level == lvl) for lvl in order}
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
    return Downloader(client or BEAClient(), config, use_cache=use_cache).run(only)


def restandardize() -> dict:
    """Rebuild data/corpus/*/series/ from the raw responses on disk (no API calls).

    Handy when we change the JSON format: edit standardize_json.py, run this, done.
    """
    if not RAW.exists():
        raise SystemExit("No data/raw/ yet. Run `python -m bea_corpus download` first.")
    counts = {}
    for ds_dir in sorted(p for p in RAW.iterdir() if p.is_dir() and p.name != "cache"):
        n = 0
        for raw in sorted(ds_dir.glob("*.json")):
            blob = json.loads(raw.read_text())
            if "responses" not in blob:  # left over from the old layout
                continue
            records = [rec for r in blob["responses"]
                       for rec in to_series(r["results"], blob["dataset"], blob["table"], request=r["request"])[0]]
            out = CORPUS / blob["dataset"] / "series" / raw.name
            old = json.loads(out.read_text()) if out.exists() else {}
            _write_json(out, {**old, "dataset": blob["dataset"], "table": blob["table"],
                              "n_series": len(records), "series": records})
            n += len(records)
        counts[ds_dir.name] = n
        print(f"  {ds_dir.name:<26} {n:>6} series")
    return counts
