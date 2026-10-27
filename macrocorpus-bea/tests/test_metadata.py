"""Metadata extraction, offline. Row shapes copied from real BEA responses."""

import json
from pathlib import Path

from bea_corpus import metadata as M
from bea_corpus.client import BEAClient

ROOT = Path(__file__).resolve().parents[1]


def fa_row(code, line, desc, year, value):
    return {"TableName": "FAAt101", "SeriesCode": code, "LineNumber": line, "LineDescription": desc,
            "TimePeriod": str(year), "METRIC_NAME": "Current Dollars", "CL_UNIT": "Level", "UNIT_MULT": "6",
            "DataValue": value, "NoteRef": "FAAt101"}


def mne_row(year, row, value):
    return {"Year": str(year), "SeriesID": "4", "SeriesName": "Direct Investment Position", "Row": row,
            "Column": "All Industries Total", "RowCode": "660", "ColumnCode": "0000",
            "TableScale": "Millions of Dollars", "DataValueUnformatted": value, "DataValue": value,
            "TableRowDisplayOrder": "231.04"}


FAKE = {
    "FixedAssets": {"Data": [fa_row("k1wtotl1es00", "1", "Fixed assets and consumer durable goods", y, "297,647")
                             for y in (2022, 2023)]
                    + [fa_row("k1ttotl1es00", ln, d, y, "92,135") for ln, d in (("2", "Fixed assets"),
                       ("16", "Private and government fixed assets")) for y in (2022, 2023)],
                    "Notes": [{"NoteRef": "FAAt101", "NoteText": "Table 1.1. Net Stock [Billions of dollars; yearend]"}],
                    "Dimensions": [{"Name": "SeriesCode", "IsValue": "0"}, {"Name": "DataValue", "IsValue": "1"}]},
    "MNE": {"Data": [mne_row(2005, "East Timor", "(D)"), mne_row(2013, "Timor-Leste", "1")]},
    "IIP": {"Data": [{"TypeOfInvestment": "FinAssetsExclFinDeriv", "Component": c, "Frequency": f, "Year": "2024",
                      "TimeSeriesId": f"TSI_{c}_{f}", "TimeSeriesDescription": f"{c} {f}",
                      "TimePeriod": "2024" if f == "A" else "2024Q1", "CL_UNIT": "USD", "UNIT_MULT": "6",
                      "DataValue": "100", "NoteRef": ""} for c in ("Pos", "ChgPos") for f in ("A", "QNSA")]},
}
CATALOG = {"Component": [{"Key": "Pos", "Desc": "Position"}, {"Key": "ChgPos", "Desc": "Change in position"}],
           "Country": [{"key": "660", "desc": "Timor-Leste"}],  # MNE really uses lowercase
           "TableName": [{"Key": "FAAt101", "Desc": "Table 1.1"},
                                                                            {"Key": "FAAt101", "Desc": "dupe"}]}


def fake_request(self, q):
    m, ds = q["method"], q.get("DatasetName")
    if m == "GetParameterList":
        return {"BEAAPI": {"Results": {"Parameter": [{"ParameterName": "Year", "ParameterIsRequiredFlag": "1"}]}}}
    if m == "GetParameterValues":
        return {"BEAAPI": {"Results": {"ParamValue": CATALOG.get(q["ParameterName"], [])}}}
    if m == "GetData":
        if ds == "NIUnderlyingDetail":
            return {"BEAAPI": {"Results": {"Error": {"APIErrorDescription": "bad table"}}}}
        if ds == "IIP":
            return {"BEAAPI": {"Results": [{"Notes": []}], "Data": FAKE["IIP"]["Data"]}}
        return {"BEAAPI": {"Results": FAKE.get(ds, {"Data": []})}}
    raise AssertionError(m)


def test_metadata_run(tmp_path, monkeypatch):
    monkeypatch.setenv("BEA_API_KEY", "k")
    monkeypatch.setattr(BEAClient, "_request", fake_request)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "metadata_groups.yaml").write_text((ROOT / "config" / "metadata_groups.yaml").read_text())
    monkeypatch.chdir(tmp_path)

    out = M.run(client=BEAClient(cache_dir=None, min_interval=0, error_pause=0))["datasets"]

    fa = out["FixedAssets"]
    assert fa["checks"]["canonical_id_status"] == "unique_in_sample"
    assert fa["checks"]["ids_repeated_on_several_lines"] == 1          # same series on lines 2 and 16
    assert fa["classifications"]["TableName"]["duplicate_keys"] == ["FAAt101"]
    assert any("billions" in f for f in fa["findings"])                 # UNIT_MULT=6 vs note says billions
    series = json.loads(Path("data/BEA/FixedAssets/bea_fixedassets_series_metadata.json").read_text())
    assert {s["series_id"] for s in series} == {"BEA:FixedAssets:FAAt101:k1wtotl1es00:A",
                                                "BEA:FixedAssets:FAAt101:k1ttotl1es00:A"}
    assert all(s["n_obs"] == 2 for s in series)                         # repeated line not double counted
    sample = json.loads(Path("data/BEA/FixedAssets/bea_fixedassets_sample.json").read_text())
    assert {o["series_id"] for o in sample["observations"]} <= {s["series_id"] for s in series}

    mne = out["MNE"]
    assert mne["checks"]["ids_with_renamed_description"] == 1
    assert mne["example_series"]["series_id"] == "BEA:MNE:Outward:4:660:0000:A"   # direction from the request
    assert mne["example_series"]["unit_source"] == "TableScale"
    assert mne["example_series"]["classifications"]["RowCode"]["label"] == "Timor-Leste"
    assert mne["source_series_id_field"] is None

    iip = out["IIP"]
    assert iip["checks"]["series"] == 4 and iip["frequencies"] == ["A", "Q"]
    assert iip["example_series"]["classifications"]["Component"]["label"] == "Position"

    # same layout as the reference files
    assert sample["table"] == "FAAt101" and sample["year"] == "2022,2023"
    assert list(sample)[-3:] == ["dimensions", "notes", "observations"]
    summ = json.loads(Path("data/BEA/bea_metadata_summary.json").read_text())
    assert {"scope", "datasets", "findings", "limitations"} <= set(summ)
    assert summ["datasets"]["FixedAssets"]["canonical_id_status"] == "verified_unique_in_sample"
    assert summ["datasets"]["FixedAssets"]["sample_file"] == "FixedAssets/bea_fixedassets_sample.json"
    gran = json.loads(Path("data/BEA/bea_metadata_granularity.json").read_text())
    assert "IIP" in gran["dataset_structures"] and "granularity_recommendation" in gran

    assert out["NIUnderlyingDetail"]["errors"]                           # one bad group doesn't stop the rest
    assert Path("data/BEA/bea_metadata_summary.json").exists()


def test_conflicting_values_flagged():
    results = {"Data": [fa_row("X1", "1", "a", 2023, "1"), fa_row("X1", "2", "b", 2023, "2")]}
    _, _, checks = M.build_series("FixedAssets", results, {}, {"series_id": ["TableName", "SeriesCode"]}, {})
    assert checks["canonical_id_status"] == "NOT_unique_in_sample"


def test_duplicate_codes_keep_all_labels():
    idx = M._label_index([{"key": "5221", "desc": "Banks"}, {"key": "5221", "desc": "Depository institutions"},
                          {"key": "5221", "desc": "Banks"}])
    c = M._classification(idx, "5221")
    assert c == {"code": "5221", "label": "Banks", "alt_labels": ["Depository institutions"]}
    assert M._classification(idx, "999") == {"code": "999", "label": ""}


def test_mne_description_is_series_specific():
    a = M._describe(mne_row(2024, "Chile", "1"))
    b = M._describe(mne_row(2024, "Peru", "1"))
    assert a != b and a.startswith("Direct Investment Position; Chile")
    # tables with a real line description are unchanged
    assert M._describe(fa_row("x", "1", "Fixed assets", 2022, "1")) == "Fixed assets"
