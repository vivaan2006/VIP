"""MNE request shapes: which shapes get tried, what is remembered between runs, and what gets logged.

Uses a small stand-in for the BEA client, so no HTTP and no API key.
"""

import json
from pathlib import Path

import pytest
import yaml

from bea_corpus import download
from bea_corpus.client import BEAError, BEATransientError

ROOT = Path(__file__).resolve().parents[1]
MNE_CFG = yaml.safe_load((ROOT / "config" / "datasets.yaml").read_text())["datasets"]["MNE"]

# The 25 shapes the code tried before this change. Every series must still get all of them.
ORIGINAL_25 = (
    [{"DirectionOfInvestment": "outward", "Classification": c} for c in ("Country", "Industry", "CountryByIndustry")]
    + [{"DirectionOfInvestment": "inward", "Classification": c} for c in (
        "Country", "Industry", "CountryByIndustry", "CountryofUBO", "CountryofUBObyIndustry",
        "CountrybyType", "CountryofUBObyType")]
    + [{"DirectionOfInvestment": d, "Classification": c, "OwnershipLevel": o, "NonbankAffiliatesOnly": "0"}
       for d in ("outward", "inward") for c in ("Country", "Industry", "CountryByIndustry") for o in ("0", "1")]
    + [{"DirectionOfInvestment": "parent", "Classification": "Industry", "OwnershipLevel": "1",
        "NonbankAffiliatesOnly": "0"}]
    + [{"DirectionOfInvestment": "state", "Classification": "StatebyCountryofUBO", "OwnershipLevel": o,
        "NonbankAffiliatesOnly": "0"} for o in ("0", "1")]
)


def row(year, value="5,000"):
    return {"Year": str(year), "SeriesID": "16", "SeriesName": "Balance Sheet", "Row": "Canada", "RowCode": "100",
            "Column": "All Industries", "ColumnCode": "0000", "DataValue": value, "TableScale": "Millions of Dollars"}


class FakeBEA:
    """Answers like BEA for a few shapes and refuses the rest. `available` maps a shape to its rows."""

    def __init__(self, available, flaky=()):
        self.available = available
        self.flaky = list(flaky)  # shapes that time out (BEA never answers)
        self.calls = []
        self.last_bytes = 100

    def get_data(self, dataset, use_cache=True, **request):
        shape = {k: v for k, v in request.items() if k not in ("SeriesID", "Country", "Year")}
        self.calls.append(shape)
        if shape in self.flaky:
            raise BEATransientError("Network problem, gave up after 4 tries: read timed out")
        for known, rows in self.available:
            if shape == known:
                return {"Data": rows}
        raise BEAError("[40] Invalid combination of DirectionOfInvestment, Classification and SeriesID")


OLD_YEARS = {"DirectionOfInvestment": "outward", "Classification": "Country", "OwnershipLevel": "0",
             "NonbankAffiliatesOnly": "1"}
NEW_YEARS = {**OLD_YEARS, "NonbankAffiliatesOnly": "0"}


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def run_table(fake, table="16", use_cache=True):
    d = download.Downloader(fake, {"datasets": {"MNE": MNE_CFG}}, use_cache=use_cache)
    t = d.download_table("MNE", MNE_CFG, table, "Balance Sheet",
                         {"SeriesID": table, "Country": "all", "Year": "ALL"})
    d._save_shapes("MNE")
    return d, t


# ---------- which shapes ----------

def test_parse_ids():
    assert download.parse_ids("1-3, 7") == {"1", "2", "3", "7"}
    assert download.parse_ids(["5", 6]) == {"5", "6"}


def test_lists_expand_and_series_limits_an_entry():
    entries = [{"A": ["x", "y"], "B": ["1", "2"]}, {"series": "5-6", "A": "z"}, {"A": "x", "B": "1"}]
    assert len(download.expand_shapes(entries, "1")) == 4      # 2 x 2, the repeat of {x, 1} dropped
    assert {"A": "z"} in download.expand_shapes(entries, "6")
    assert {"A": "z"} not in download.expand_shapes(entries, "7")


@pytest.mark.parametrize("series", ["1", "9", "16", "22", "45", "58", "62", "70"])
def test_every_series_still_gets_the_original_25(series):
    shapes = download.expand_shapes(MNE_CFG["request_shapes"], series)
    assert all(s in shapes for s in ORIGINAL_25)


@pytest.mark.parametrize("series, count", [("16", 66), ("55", 66), ("22", 28), ("62", 31), ("45", 25)])
def test_new_shapes_only_go_to_their_series_group(series, count):
    shapes = download.expand_shapes(MNE_CFG["request_shapes"], series)
    assert len(shapes) == count
    added = shapes[25:]
    if series in download.parse_ids("1-21, 46-61"):      # AMNE: every new shape has an ownership level
        assert all("OwnershipLevel" in s for s in added)
    else:                                                # DI: none do
        assert not any("OwnershipLevel" in s for s in added)


def test_new_classifications_are_tried_for_amne():
    shapes = download.expand_shapes(MNE_CFG["request_shapes"], "60")
    for cls in ("IndustryByDestination", "CountryByDestination", "CountryofUBObyDestination", "IndustryofSales"):
        assert any(s["Classification"] == cls for s in shapes), cls
    assert OLD_YEARS in shapes  # nonbank-only years


# ---------- what happens on a run ----------

def test_old_and_new_years_join_into_one_series():
    fake = FakeBEA([(OLD_YEARS, [row(y) for y in range(2000, 2009)]),
                    (NEW_YEARS, [row(y) for y in range(2009, 2013)])])
    d, t = run_table(fake)
    series = json.loads(Path("data/corpus/MNE/series/16.json").read_text())["series"]
    assert len(series) == 1 and series[0]["start"] == "2000-01-01" and series[0]["end"] == "2012-01-01"
    assert t.series == 1 and t.requests == 66 and t.requests_ok == 2
    assert not [i for i in d.issues if i.level in ("error", "warning")]


def test_refused_shapes_are_remembered_and_skipped_next_run():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])])
    run_table(fake)
    memory = json.loads(download.shape_memory_path("MNE").read_text())["16"]
    assert len(memory) == 65 and all("Invalid combination" in m for m in memory.values())

    fake.calls.clear()
    d, t = run_table(fake)
    assert fake.calls == [NEW_YEARS] and t.series == 1     # only the shape that has data is asked again
    assert t.check == "66 request shapes, 65 known-empty skipped"
    report = json.loads(Path("reports/shapes_MNE.json").read_text())["16"]
    assert sum(r["status"] == "skipped" for r in report) == 65


def test_refresh_tries_everything_and_forgets_shapes_that_now_work():
    run_table(FakeBEA([(NEW_YEARS, [row(2010)])]))
    fake = FakeBEA([(NEW_YEARS, [row(2010)]), (OLD_YEARS, [row(2005)])])  # BEA added the old years
    d, t = run_table(fake, use_cache=False)
    assert len(fake.calls) == 66 and t.requests_ok == 2
    memory = json.loads(download.shape_memory_path("MNE").read_text())["16"]
    assert download.shape_key(OLD_YEARS) not in memory and len(memory) == 64


def test_no_answer_is_a_warning_and_is_tried_again():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])], flaky=[OLD_YEARS])
    d, t = run_table(fake)
    warnings = [i for i in d.issues if i.level == "warning"]
    assert len(warnings) == 1 and "1 of 66" in warnings[0].issue and "read timed out" in warnings[0].issue
    assert download._status_word(t) == "partial" and len(t.missing) == 1
    memory = json.loads(download.shape_memory_path("MNE").read_text())["16"]
    assert download.shape_key(OLD_YEARS) not in memory  # never remembered as "no data"

    fake.flaky.clear()
    fake.calls.clear()
    run_table(fake)
    assert OLD_YEARS in fake.calls


def test_nothing_works_says_why():
    d, t = run_table(FakeBEA([]))
    errors = [i for i in d.issues if i.level == "error"]
    assert len(errors) == 1 and "Invalid combination" in errors[0].issue and "66 request shapes" in errors[0].issue
    assert download._status_word(t) == "FAIL"


def test_only_network_trouble_is_not_reported_as_no_data():
    fake = FakeBEA([], flaky=download.expand_shapes(MNE_CFG["request_shapes"], "45"))
    d, t = run_table(fake, table="45")
    assert [i.level for i in d.issues] == ["warning"]  # "rerun", not "no data with any shape"


def test_probe_that_gets_no_answer_is_logged_elsewhere_too():
    d = download.Downloader(FakeBEA([], flaky=[{"GeoFips": "COUNTY"}]), {"datasets": {}})
    t = download.TableResult("CAINC1", "")
    assert d._get("Regional", t, {"GeoFips": "COUNTY"}, probe=True) is None
    assert [i.level for i in d.issues] == ["warning"]
    assert d._get("Regional", t, {"GeoFips": "MSA"}, probe=True) is None   # plain "no such data": quiet
    assert len(d.issues) == 1
