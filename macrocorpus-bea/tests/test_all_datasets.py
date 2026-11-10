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
    "MNE": [{"key": "0", "desc": "all"}] + [{"key": k, "desc": f"series {k}"}
                                            for k in ("22", "27", "30", "31", "33", "6")],  # no "25": warning
    "Regional": [{"Key": "SAINC1", "Desc": "State income"}, {"Key": "MAGDP2", "Desc": "Metro GDP"},
                 {"Key": "BADTABLE", "Desc": "Always errors"}, {"Key": "CAINC1", "Desc": "County income"},
                 {"Key": "CAINC91", "Desc": "County earnings flows"}],
}
REGIONAL_LINES = {"SAINC1": ["1", "2", "3"], "MAGDP2": ["1", "2"], "BADTABLE": ["1"], "CAINC1": ["1", "2"],
                  "CAINC91": ["10", "20"]}
# which geography levels each fake Regional table is published at (everything else errors, like BEA)
REGIONAL_LEVELS = {"SAINC1": {"STATE"}, "MAGDP2": {"STATE"}, "CAINC1": {"COUNTY", "STATE"}, "CAINC91": {"COUNTY"}}
REGIONAL_PLACES = {"STATE": [("01000", "Alabama"), ("13000", "Georgia")],
                   # BEA's county pull also carries the state rows, so they overlap with the STATE pull
                   "COUNTY": [("10001", "Kent"), ("13121", "Fulton"), ("13000", "Georgia")]}
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
        direction, sid = q.get("DirectionOfInvestment", ""), q["SeriesID"]
        if q.get("Classification", "Country") != "Country":
            raise download.BEAError("[40] Invalid Classification for this series")
        if sid == "6":  # operations series: needs an ownership level, exists both outward and inward
            if q.get("OwnershipLevel") != "0" or direction not in ("outward", "inward"):
                raise download.BEAError("[40] OwnershipLevel required")
        elif "OwnershipLevel" in q or (sid == "22") != (direction == "inward"):  # 22 inward only, rest outward
            raise download.BEAError("[40] Invalid combination of DirectionOfInvestment and SeriesID")
        return {"Data": [{"Year": str(y), "SeriesID": q["SeriesID"], "SeriesName": "Position", "Row": c,
                          "Column": "All Industries", "RowCode": c, "ColumnCode": "0000",
                          "DataValue": "n.s." if c == "XX" else "5,000", "TableScale": "Millions of Dollars"}
                         for c in ("308", "309", "XX") for y in YEARS]}
    if ds == "Regional":
        table, line, geo = q["TableName"], q["LineCode"], q["GeoFips"]
        if table == "BADTABLE" or geo not in REGIONAL_LEVELS[table]:
            raise download.BEAError("[40] Invalid GeoFips for this table")
        if table == "SAINC1" and line == "3":
            return {"Data": []}  # a line the dictionary lists but that has no data
        return {"Data": [{"Code": f"{table}-{line}", "GeoFips": f, "GeoName": n, "TimePeriod": str(y),
                          "CL_UNIT": "Dollars", "UNIT_MULT": "0", "DataValue": "500"}
                         for f, n in REGIONAL_PLACES[geo] for y in YEARS]}
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
        if ds == "IntlServSTA":
            params += [{"ParameterName": "AreaOrCountry", "ParameterIsRequiredFlag": "1", "AllValue": "ALL"}]
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
        if ds == "IntlServSTA" and name == "AreaOrCountry":
            return ok({"ParamValue": [{"Key": k} for k in ("ALL", "AllCountries", "China", "Narnia")]})
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
    r = download.run(only=["Regional"], sample_size=1000, client=client())[0]
    by = {t.table: t for t in r.tables}
    sainc = by["SAINC1"]
    assert sainc.check.endswith("at STATE") and sainc.expected == 3 and sainc.missing == ["3"]
    assert sainc.series == 2 * 2                          # lines 1-2 x two states
    magdp = by["MAGDP2"]
    assert magdp.series == 2 * 2 and magdp.check.endswith("at STATE")  # metro levels refused, STATE kept
    assert by["BADTABLE"].series == 0
    found = issues()
    assert not any(i["table"] == "MAGDP2" and i["level"] != "info" for i in found)
    assert any(i["table"] == "SAINC1" and i["level"] == "warning" and "1 of 3 lines" in i["issue"] for i in found)
    assert any(i["table"] == "BADTABLE" and i["level"] == "error" for i in found)


def test_regional_keeps_every_geography_level_and_counties(workdir):
    by = {t.table: t for t in download.run(only=["Regional"], sample_size=1000, client=client())[0].tables}
    # CAINC1 is published for counties AND states: both kept, Georgia (in both pulls) only once
    ca = series_file("Regional", "CAINC1")["series"]
    assert by["CAINC1"].check.endswith("at COUNTY, STATE")
    geos = sorted(s["dimensions"]["GeoFips"] for s in ca)
    assert geos == sorted(["10001", "13121", "13000", "01000"] * 2)
    assert len({s["series_id"] for s in ca}) == len(ca)
    # CAINC91 only exists at county level: the old STATE-then-national logic got nothing here
    kent = [s for s in series_file("Regional", "CAINC91")["series"] if s["dimensions"]["GeoFips"] == "10001"]
    assert len(kent) == 2 and by["CAINC91"].missing == []


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


def test_runs_from_any_folder(tmp_path, monkeypatch):
    from bea_corpus import cli
    monkeypatch.chdir(tmp_path)  # no config/ here, like running from the repo root
    cli._go_to_project_root()
    assert Path.cwd() == cli.PROJECT_ROOT and Path("config/datasets.yaml").exists()


def test_full_run_ignores_hand_picked_tables_and_skips_all(workdir):
    r = download.run(only=["MNE"], sample_size=1000, client=client())[0]
    assert sorted(t.table for t in r.tables) == ["22", "27", "30", "31", "33", "6"]  # every series ID, no "0" = all
    assert all(t.series for t in r.tables)
    inward = series_file("MNE", "22")["series"]
    assert {s["dimensions"]["DirectionOfInvestment"] for s in inward} == {"inward"}
    assert not [i for i in issues() if i["level"] == "error"]


def test_mne_keeps_every_shape_that_has_data(workdir):
    download.run(only=["MNE"], sample_size=1000, client=client())
    six = series_file("MNE", "6")["series"]
    # series 6 exists outward AND inward: both kept, told apart in the id, not first-one-wins
    assert {s["dimensions"]["DirectionOfInvestment"] for s in six} == {"outward", "inward"}
    assert len({s["series_id"] for s in six}) == len(six) == 2 * 3
    assert all(".outward." in s["series_id"] or ".inward." in s["series_id"] for s in six)
    assert all(s["unit"] == "USD" and s["unit_multiplier"] == 6 for s in six)  # from TableScale


def test_no_shape_works_logs_one_error(workdir):
    d = download.Downloader(client(), {"datasets": {}})
    t = download.TableResult("22", "")
    got = d._all_shapes("MNE", t, {"SeriesID": "22", "Year": "ALL", "SomethingNew": "ALL"},
                        [{"DirectionOfInvestment": "parent"}, {"DirectionOfInvestment": "state"}])
    assert got == [] and (t.requests, t.requests_ok) == (2, 0)
    assert len(d.issues) == 1 and d.issues[0].level == "error"


def test_intlservsta_one_request_per_country(workdir, monkeypatch):
    real = fake_get_data

    def picky(ds, q):  # like BEA: refuses every country + industry + year at once
        if ds == "IntlServSTA":
            if q.get("AreaOrCountry", "ALL") == "ALL":
                raise download.BEAError("[331] Exactly one Industry must be requested or exactly one AreaOrCountry")
            if q["AreaOrCountry"] == "Narnia":
                return {"Data": []}
            return {"Data": [{**r, "AreaOrCountry": q["AreaOrCountry"]} for r in real(ds, q)["Data"]]}
        return real(ds, q)

    monkeypatch.setitem(globals(), "fake_get_data", picky)
    monkeypatch.setattr(BEAClient, "_request", fake_request)
    r = download.run(only=["IntlServSTA"], client=client())[0]
    t = r.tables[0]
    assert t.requests == 3 and t.requests_ok == 2 and t.missing == ["Narnia"]  # ALL is skipped, not requested
    areas = {s["dimensions"]["AreaOrCountry"] for s in series_file("IntlServSTA", t.table)["series"]}
    assert areas == {"AllCountries", "China"}
    assert not [i for i in issues() if i["level"] == "error"]


def test_empty_whole_table_is_retried_one_frequency_at_a_time(workdir, monkeypatch):
    real = fake_get_data

    def empty_when_mixed(ds, q):  # like U20403: nothing for Frequency=A,Q,M, data one frequency at a time
        if ds == "NIUnderlyingDetail" and "," in q.get("Frequency", ""):
            return {"Data": []}
        return real(ds, q)

    monkeypatch.setitem(globals(), "fake_get_data", empty_when_mixed)
    t = download.run(only=["NIUnderlyingDetail"], client=client())[0].tables[0]
    assert download._status_word(t) == "ok"
    assert t.requests == 4 and t.requests_ok == 3  # the empty whole-table call, then A, Q, M
    freqs = {s["frequency"] for s in series_file("NIUnderlyingDetail", t.table)["series"]}
    assert freqs == {"A", "Q", "M"}
    assert any("smaller requests" in i["issue"] and i["level"] == "info" for i in issues())


def test_empty_table_is_fail_not_ok_and_old_files_go(workdir, monkeypatch):
    download.run(only=["NIUnderlyingDetail"], client=client())
    path = Path("data/corpus/NIUnderlyingDetail/series/U20305.json")
    assert path.exists()
    monkeypatch.setitem(globals(), "fake_get_data", lambda ds, q: {"Data": []})
    t = download.run(only=["NIUnderlyingDetail"], client=client(), use_cache=False)[0].tables[0]
    assert download._status_word(t) == "FAIL" and t.series == 0
    assert not path.exists()  # an old run's file doesn't pass for this run's result
    assert any(i["level"] == "error" and i["table"] == "U20305" for i in issues())


def test_subset_run_keeps_the_other_datasets_in_the_reports(workdir):
    download.run(only=["NIPA", "ITA"], client=client())
    download.run(only=["ITA"], client=client())
    names = [d["dataset"] for d in json.loads(Path("reports/coverage.json").read_text())["datasets"]]
    assert sorted(names) == ["ITA", "NIPA"]
    assert "| NIPA |" in Path("reports/coverage.md").read_text()


def test_every_results_entry_is_kept():
    from bea_corpus.client import extract_results
    payload = {"BEAAPI": {"Results": [{"Statistic": "x", "Data": [{"a": 1}]}, {"Data": {"a": 2}}]}}
    assert extract_results(payload)["Data"] == [{"a": 1}, {"a": 2}]
