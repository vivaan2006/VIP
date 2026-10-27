"""Offline run of `download` over all 12 datasets with a fake BEA API.

Each fake GetData response copies the real shape of that dataset (field names from the
BEA API User Guide and real responses, wrapping quirks from the official `beaapi` package):
  - GDPbyIndustry / UnderlyingGDPbyIndustry / InputOutput / IIP: Results is a 1-item list
  - IIP: Data sits next to Results, not inside it
  - GDPbyIndustry: no TimePeriod, uses Year + Quarter (roman numerals)
  - MNE / InputOutput: Year only
  - Regional: one line per call
  - suppressed values like "(D)" and "n.s."
"""

import json
from pathlib import Path

import pytest

from bea_corpus import download
from bea_corpus.client import BEAClient
from bea_corpus.standardize_json import parse_time, to_series

ROOT = Path(__file__).resolve().parents[1]
YEARS = range(2015, 2025)

TABLE_PARAM = {"NIPA": "TableName", "NIUnderlyingDetail": "TableName", "FixedAssets": "TableName",
               "GDPbyIndustry": "TableID", "UnderlyingGDPbyIndustry": "TableID", "InputOutput": "TableID",
               "ITA": "Indicator", "IIP": "TypeOfInvestment", "IntlServTrade": "TypeOfService",
               "IntlServSTA": "Channel", "MNE": "SeriesID", "Regional": "TableName"}
TABLES = {
    "NIPA": [{"TableName": f"T1010{i}", "Description": f"Table 1.1.{i}"} for i in range(1, 9)],
    "NIUnderlyingDetail": [{"TableName": "U20305", "Description": "PCE by type"}],
    "FixedAssets": [{"TableName": "FAAt101", "Description": "Fixed assets"}],
    "GDPbyIndustry": [{"Key": "1", "Desc": "Value added"}, {"Key": "5", "Desc": "Shares"}],
    "UnderlyingGDPbyIndustry": [{"Key": "210", "Desc": "Underlying"}],
    "InputOutput": [{"Key": k, "Desc": f"IO {k}"} for k in ("56", "57", "58", "59")],
    "ITA": [{"Key": "BalGds", "Desc": "Balance on goods"}, {"Key": "ExpGds", "Desc": "Exports"}],
    "IIP": [{"Key": "FinAssetsExclFinDeriv", "Desc": "Assets"}],
    "IntlServTrade": [{"Key": "Travel", "Desc": "Travel"}],
    "IntlServSTA": [{"Key": "Mofas", "Desc": "Affiliates"}],
    "MNE": [{"key": k, "desc": f"series {k}"} for k in ("0", "27", "30", "31", "33")],  # no "25": warning
    "Regional": [{"Key": "SAINC1", "Desc": "State income"}, {"Key": "MAGDP2", "Desc": "Metro GDP"},
                 {"Key": "BADTABLE", "Desc": "Always errors"}],
}
REGIONAL_LINES = {"SAINC1": ["1", "2", "3"], "MAGDP2": ["1", "2"], "BADTABLE": ["1"]}
# Narnia never has data. Atlantis is left out of AreaOrCountry=ALL but comes back when asked for alone.
ITA_COUNTRIES = [{"Key": "AllCountries"}, {"Key": "China"}, {"Key": "Narnia"}, {"Key": "Atlantis"}]
NIPA_LINES = 150  # x 3 frequencies = 450 series in one table: more than the old 300-series cap


def _nipa_like(q):
    freqs = q.get("Frequency", "A").split(",")
    rows = []
    for line in range(1, NIPA_LINES + 1):
        for f in freqs:
            periods = ([str(y) for y in YEARS] if f == "A" else
                       [f"{y}Q{k}" for y in YEARS for k in range(1, 5)] if f == "Q" else
                       [f"{y}M{m:02d}" for y in YEARS for m in range(1, 13)])
            rows += [{"TableName": q["TableName"], "SeriesCode": f"S{line}", "LineNumber": str(line),
                      "LineDescription": f"Line {line}", "TimePeriod": p, "METRIC_NAME": "Fisher Quantity Index",
                      "CL_UNIT": "Level", "UNIT_MULT": "0", "DataValue": f"{1000 + i:,}", "NoteRef": ""}
                     for i, p in enumerate(periods)]
    return {"Data": rows, "PublicTable": f"Table {q['TableName']}"}


def fake_get_data(ds, q):
    if ds in ("NIPA", "NIUnderlyingDetail", "FixedAssets"):
        return _nipa_like(q)
    if ds in ("GDPbyIndustry", "UnderlyingGDPbyIndustry"):
        rows = [{"TableID": q["TableID"], "Frequency": "Q", "Year": str(y), "Quarter": qq, "Industry": ind,
                 "IndustrYDescription": f"Industry {ind}", "DataValue": "12.5", "NoteRef": ""}
                for ind in ("11", "21", "22") for y in YEARS for qq in ("I", "II", "III", "IV")]
        rows += [{"TableID": q["TableID"], "Frequency": "A", "Year": str(y), "Quarter": str(y), "Industry": "11",
                  "IndustrYDescription": "Industry 11", "DataValue": "(D)", "NoteRef": "(D)"} for y in YEARS]
        return {"Data": rows}
    if ds == "InputOutput":
        return {"Data": [{"TableID": 56, "Year": str(y), "RowCode": r, "RowDescr": f"Row {r}", "ColCode": c,
                          "ColDescr": f"Col {c}", "DataValue": "3.0", "NoteRef": ""}
                         for r in ("111CA", "113FF") for c in ("211", "212") for y in YEARS]}
    if ds in ("ITA", "IIP"):
        if q.get("AreaOrCountry") == "Narnia":
            raise download.BEAError("[40] Invalid AreaOrCountry")
        areas = (["AllCountries", "China"] if q.get("AreaOrCountry") == "ALL" else [q["AreaOrCountry"]]) \
            if ds == "ITA" else ["AllCountries"]
        rows = [{"Indicator": q.get("Indicator", ""), "TypeOfInvestment": q.get("TypeOfInvestment", ""),
                 "AreaOrCountry": a, "Frequency": f, "Year": str(y), "TimeSeriesId": f"{a}{f}X",
                 "TimeSeriesDescription": "Some series", "TimePeriod": f"{y}Q{k}" if f != "A" else str(y),
                 "CL_UNIT": "USD", "UNIT_MULT": "6", "DataValue": "-1,234", "NoteRef": ""}
                for a in areas for f in ("A", "QSA") for y in YEARS for k in ((1, 2, 3, 4) if f != "A" else (0,))]
        return {"Data": [{k: v for k, v in r.items() if v != ""} for r in rows]}
    if ds in ("IntlServTrade", "IntlServSTA"):
        return {"Data": [{"TypeOfService": "Travel", "TradeDirection": d, "AreaOrCountry": "AllCountries",
                          "TimeSeriesId": f"TS{d}", "TimeSeriesDescription": f"Travel {d}", "TimePeriod": str(y),
                          "CL_UNIT": "USD", "UNIT_MULT": "6", "DataValue": "100", "NoteRef": ""}
                         for d in ("Exports", "Imports") for y in YEARS]}
    if ds == "MNE":
        return {"Data": [{"Year": str(y), "SeriesID": q["SeriesID"], "SeriesName": "Position", "Row": c,
                          "Column": "All Industries", "RowCode": c, "ColumnCode": "0000",
                          "DataValue": "n.s." if c == "XX" else "5,000", "TableScale": "Millions of Dollars"}
                         for c in ("308", "309", "XX") for y in YEARS]}
    if ds == "Regional":
        table, line, geo = q["TableName"], q["LineCode"], q["GeoFips"]
        if table == "BADTABLE" or (table.startswith("MA") and geo == "MSA"):
            raise download.BEAError("[40] Invalid parameter")
        if table == "SAINC1" and line == "3":
            return {"Data": []}  # a line the dictionary lists but that has no data
        return {"Data": [{"Code": f"{table}-{line}", "GeoFips": f, "GeoName": n, "TimePeriod": str(y),
                          "CL_UNIT": "Dollars", "UNIT_MULT": "0", "DataValue": "500"}
                         for f, n in (("01000", "Alabama"), ("13000", "Georgia")) for y in YEARS]}
    raise AssertionError(ds)


def fake_request(self, query):
    m, ds = query["method"], query.get("DatasetName")
    self.last_bytes, self.last_seconds = 2048, 0.01
    req = {"RequestParam": [{"ParameterName": "USERID", "ParameterValue": self.api_key}]}

    def ok(results, wrap=False):
        return {"BEAAPI": {"Request": req, "Results": [results] if wrap else results}}

    def err(text):
        return {"BEAAPI": {"Request": req, "Results": {"Error": {"APIErrorCode": "40", "APIErrorDescription": text}}}}

    if m == "GetDataSetList":
        names = list(TABLE_PARAM) + ["APIDatasetMetaData"] + getattr(self, "extra_datasets", [])
        return ok({"Dataset": [{"DatasetName": n, "DatasetDescription": n} for n in names]})
    if m == "GetParameterList":
        params = [{"ParameterName": TABLE_PARAM[ds], "ParameterIsRequiredFlag": "1"},
                  {"ParameterName": "Year", "ParameterIsRequiredFlag": "1", "AllValue": "ALL"},
                  {"ParameterName": "SomethingNew", "ParameterIsRequiredFlag": "1", "AllValue": "ALL"}]
        if ds == "Regional":
            params += [{"ParameterName": "LineCode", "ParameterIsRequiredFlag": "1"},
                       {"ParameterName": "GeoFips", "ParameterIsRequiredFlag": "1"}]
        return ok({"Parameter": params})
    if m == "GetParameterValues":
        name = query["ParameterName"]
        if name == TABLE_PARAM[ds]:
            return ok({"ParamValue": TABLES[ds]})
        if name == "LineCode":
            return err("LineCode needs a TableName")
        return ok({"ParamValue": [{"Key": "ALL", "Desc": "everything"}]})
    if m == "GetParameterValuesFiltered":
        if ds == "Regional":
            return ok({"ParamValue": [{"Key": k, "Desc": f"line {k}"} for k in REGIONAL_LINES[query["TableName"]]]})
        if ds == "ITA":
            return ok({"ParamValue": ITA_COUNTRIES})
        return err("[34] GetParameterValuesFiltered has not been implemented on this dataset")
    if m == "GetData":
        assert query.get("SomethingNew") == "ALL", "required params should be auto-filled"
        try:
            res = fake_get_data(ds, query)
        except download.BEAError as exc:
            return err(str(exc))
        if ds == "IIP":
            return {"BEAAPI": {"Request": req, "Results": [{"Dimensions": []}], "Data": res["Data"]}}
        return ok(res, wrap=ds in ("GDPbyIndustry", "UnderlyingGDPbyIndustry", "InputOutput"))
    raise AssertionError(m)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("BEA_API_KEY", "secret-test-key")
    monkeypatch.setattr(BEAClient, "_request", fake_request)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "datasets.yaml").write_text((ROOT / "config" / "datasets.yaml").read_text())
    monkeypatch.chdir(tmp_path)
    return tmp_path


def client(**kw):
    return BEAClient(cache_dir="data/raw/cache", min_interval=0, error_pause=0, **kw)


def issues():
    return json.loads(Path("reports/issues.json").read_text())


def series_file(dataset, table):
    return json.loads(Path(f"data/corpus/{dataset}/series/{table}.json").read_text())


def test_downloads_all_twelve_datasets(workdir):
    results = {r.dataset: r for r in download.run(client=client())}
    assert len(results) == 12
    for name, r in results.items():
        assert r.series > 0, name
        d = json.loads(Path(f"data/corpus/{name}/dictionary.json").read_text())
        assert d["values"][TABLE_PARAM[name]], f"{name} dictionary is missing its table list"
        for t in r.tables:
            if t.series:
                recs = series_file(name, download.safe_name(t.table))["series"]
                assert len(recs) == t.series
                assert all(len(x["dates"]) == len(x["values"]) and x["dates"] == sorted(x["dates"]) for x in recs)
    cov = json.loads(Path("reports/coverage.json").read_text())
    assert len(cov["datasets"]) == 12 and Path("reports/coverage.md").exists()
    assert not any("secret-test-key" in p.read_text() for p in Path("data").rglob("*.json"))


def test_every_series_in_a_table_no_cap(workdir):
    results = {r.dataset: r for r in download.run(only=["NIPA"], client=client())}
    nipa = results["NIPA"]
    assert len(nipa.tables) == 5                         # sample_size from the config
    assert {t.series for t in nipa.tables} == {NIPA_LINES * 3}  # all 450 series, not stopped at 300


def test_regional_pulls_every_line_and_logs_gaps(workdir):
    r = download.run(only=["Regional"], client=client())[0]
    by = {t.table: t for t in r.tables}
    sainc = by["SAINC1"]
    assert sainc.requests == 3 and sainc.expected == 3 and sainc.missing == ["3"]
    assert sainc.series == 2 * 2                          # lines 1-2 x two states
    magdp = by["MAGDP2"]
    assert magdp.requests_ok == magdp.requests == 2      # MSA probe failed quietly, then STATE for both lines
    assert by["BADTABLE"].requests_ok == 0
    found = issues()
    assert not any(i["table"] == "MAGDP2" for i in found)
    assert any(i["table"] == "SAINC1" and i["level"] == "warning" and "1 of 3 lines" in i["issue"] for i in found)
    assert any(i["table"] == "BADTABLE" and i["level"] == "error" for i in found)


def test_checks_against_dictionary(workdir):
    results = {r.dataset: r for r in download.run(only=["ITA", "GDPbyIndustry"], client=client())}
    ita = results["ITA"].tables[0]
    assert ita.check == "every AreaOrCountry in the dictionary" and ita.missing == ["Narnia"]
    countries = {r["dimensions"]["AreaOrCountry"] for r in series_file("ITA", ita.table)["series"]}
    assert countries == {"AllCountries", "China", "Atlantis"}  # Atlantis recovered by asking on its own
    gdp = results["GDPbyIndustry"].tables[0]
    assert gdp.check == "whole table in one call"        # filtered list not available on this dataset
    levels = {(i["dataset"], i["level"]) for i in issues()}
    assert ("ITA", "warning") in levels and ("GDPbyIndustry", "info") in levels
    assert any(i["dataset"] == "ITA" and "on their own" in i["issue"] and i["level"] == "info" for i in issues())


def test_coverage_and_config_problems_are_logged(workdir, monkeypatch):
    monkeypatch.setattr(BEAClient, "extra_datasets", ["BrandNewDataset"], raising=False)
    results = {r.dataset: r for r in download.run(client=client())}
    found = issues()
    assert any(i["dataset"] == "BrandNewDataset" and i["level"] == "error" for i in found)
    assert not any(i["dataset"] == "APIDatasetMetaData" for i in found)
    assert any(i["dataset"] == "MNE" and i["table"] == "25" and "not in the dictionary" in i["issue"] for i in found)
    assert [t.table for t in results["MNE"].tables] == ["30", "31", "27", "25", "33"]  # explicit list kept
    assert any(i["dataset"] == "Regional" and i["level"] == "info" and "LineCode" in i["issue"] for i in found)
    assert len(results["InputOutput"].tables) == 2       # per-dataset sample_size


def test_restandardize_matches(workdir):
    download.run(only=["NIPA", "Regional"], client=client())
    before = series_file("Regional", "SAINC1")["series"]
    download.restandardize()
    assert series_file("Regional", "SAINC1")["series"] == before


def test_quirky_shapes():
    gdp, _ = to_series(fake_get_data("GDPbyIndustry", {"TableID": "1"}), "GDPbyIndustry", "1")
    assert {r["frequency"] for r in gdp} == {"A", "Q"}
    annual = next(r for r in gdp if r["frequency"] == "A")
    assert annual["n_obs"] == 0 and annual["missing_codes"] == {"(D)": 10}
    mne, _ = to_series(fake_get_data("MNE", {"SeriesID": "30"}), "MNE", "30")
    assert any(r["missing_codes"].get("n.s.") == 10 for r in mne)


def test_parse_time():
    assert parse_time({"TimePeriod": "2024Q3"}) == ("2024-07-01", "Q")
    assert parse_time({"TimePeriod": "2024M07"}) == ("2024-07-01", "M")
    assert parse_time({"TimePeriod": "2024M7"}) == ("2024-07-01", "M")
    assert parse_time({"TimePeriod": "2024"}) == ("2024-01-01", "A")
    assert parse_time({"Year": "2020", "Quarter": "III"}) == ("2020-07-01", "Q")
    assert parse_time({"Year": "2020", "Quarter": "2020"}) == ("2020-01-01", "A")
    assert parse_time({"Year": "2020"}) == ("2020-01-01", "A")
    assert parse_time({"TimePeriod": "garbage"}) is None


def test_single_row_dict():
    recs, _ = to_series({"Data": {"GeoFips": "1", "TimePeriod": "2020", "DataValue": "5"}}, "Regional", "X")
    assert len(recs) == 1 and recs[0]["values"] == [5.0]


def test_pick_evenly():
    assert download.pick_evenly(list(range(10)), 3) == [0, 4, 9]  # round(4.5) -> 4 (banker's rounding)
    assert download.pick_evenly([1, 2], 5) == [1, 2]
