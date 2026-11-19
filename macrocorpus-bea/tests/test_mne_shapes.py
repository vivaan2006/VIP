"""MNE request shapes: which shapes get tried, what is remembered between runs, and what gets logged.

Uses a small stand-in for the BEA client, so no HTTP and no API key.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from bea_corpus import download
from bea_corpus.client import BEAError, BEATransientError

ROOT = Path(__file__).resolve().parents[1]
MNE_CFG = yaml.safe_load((ROOT / "config" / "datasets.yaml").read_text())["datasets"]["MNE"]
BASE = {"Country": "all", "Year": "ALL"}

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
OLD_YEARS = {"DirectionOfInvestment": "outward", "Classification": "Country", "OwnershipLevel": "0",
             "NonbankAffiliatesOnly": "1"}
NEW_YEARS = {**OLD_YEARS, "NonbankAffiliatesOnly": "0"}


def shapes_for(table):
    return download.expand_shapes(MNE_CFG["request_shapes"], table)


def row(year, value="5,000", series="4"):
    return {"Year": str(year), "SeriesID": series, "SeriesName": "Sales", "Row": "Canada", "RowCode": "100",
            "Column": "All Industries", "ColumnCode": "0000", "DataValue": value, "TableScale": "Millions of Dollars"}


class FakeBEA:
    """Answers like BEA for a few shapes and refuses the rest. `available` maps a shape to its rows."""

    def __init__(self, available=(), flaky=(), broken=()):
        self.available = list(available)
        self.flaky = list(flaky)    # shapes BEA never answers (timeouts)
        self.broken = list(broken)  # shapes that hit a bug on our side
        self.calls = []
        self.last_bytes = 100

    def get_data(self, dataset, use_cache=True, **request):
        shape = {k: v for k, v in request.items() if k not in ("SeriesID", "Year") and (k, v) != ("Country", "all")}
        self.calls.append(shape)
        if shape in self.flaky:
            raise BEATransientError("Network problem, gave up after 4 tries: read timed out")
        if shape in self.broken:
            raise KeyError("oops")
        for known, rows in self.available:
            if shape == known:
                return {"Data": rows}
        raise BEAError("[40] Invalid combination of DirectionOfInvestment, Classification and SeriesID")


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def run_table(fake, table="4", use_cache=True, base=BASE):
    d = download.Downloader(fake, {"datasets": {"MNE": MNE_CFG}}, use_cache=use_cache)
    t = d.download_table("MNE", MNE_CFG, table, "", {"SeriesID": table, **base})
    return d, t


def memory(table="4"):
    return json.loads(download.shape_memory_path("MNE").read_text()).get(table, {})


def levels(d):
    return [i.level for i in d.issues]


# ---------- which shapes ----------

def test_parse_ids():
    assert download.parse_ids("1-3, 7") == {"1", "2", "3", "7"}
    assert download.parse_ids(["5", 6]) == {"5", "6"}
    with pytest.raises(ValueError, match="Bad series range"):
        download.parse_ids("a-b")


def test_lists_expand_and_series_limits_an_entry():
    entries = [{"A": ["x", "y"], "B": ["1", "2"]}, {"series": "5-6", "A": "z"}, {"A": "x", "B": "1"}]
    assert len(download.expand_shapes(entries, "1")) == 4      # 2 x 2, the repeat of {x, 1} dropped
    assert {"A": "z"} in download.expand_shapes(entries, "6")
    assert {"A": "z"} not in download.expand_shapes(entries, "7")


@pytest.mark.parametrize("series", ["1", "4", "9", "16", "22", "45", "58", "62", "70"])
def test_every_series_still_gets_the_original_25_first(series):
    assert shapes_for(series)[:25] == ORIGINAL_25


@pytest.mark.parametrize("series, count", [("1", 80), ("2", 55), ("9", 31), ("16", 38), ("22", 27), ("30", 26),
                                           ("60", 51), ("62", 28), ("45", 25)])
def test_shape_counts(series, count):
    assert len(shapes_for(series)) == count


@pytest.mark.parametrize("series", ["1", "4", "9", "16", "20", "22", "30", "62"])
def test_ownership_level_only_on_activities_of_multinationals(series):
    added = shapes_for(series)[25:]
    amne = series in download.parse_ids("1-21, 46-61")
    assert all(("OwnershipLevel" in s) == amne for s in added)


def test_directions_follow_bea_series_list():
    def directions(series):
        return {s["DirectionOfInvestment"] for s in shapes_for(series)[25:]}
    assert directions("60") == {"outward"}               # services supplied: outward only
    assert directions("2") == {"inward", "state"}        # gross PPE: inward, and by state
    assert directions("20") == {"parent"}
    assert directions("9") == {"state"}


def test_new_classifications_are_tried():
    def classes(series):
        return {s["Classification"] for s in shapes_for(series)}
    assert {"CountryByDestination", "IndustryByDestination"} <= classes("60")
    assert {"CountryofUBObyDestination", "IndustryofSales", "IndustryByDestination"} <= classes("4")
    assert OLD_YEARS in shapes_for("4") and OLD_YEARS in shapes_for("16")
    assert {**ORIGINAL_25[-1], "NonbankAffiliatesOnly": "0", "Country": "000"} in shapes_for("9")


# ---------- what happens on a run ----------

def test_old_and_new_years_join_and_the_change_is_marked():
    fake = FakeBEA([(OLD_YEARS, [row(y) for y in range(2000, 2009)]),
                    (NEW_YEARS, [row(y) for y in range(2009, 2013)])])
    d, t = run_table(fake)
    series = json.loads(Path("data/corpus/MNE/series/4.json").read_text())["series"]
    assert len(series) == 1 and series[0]["start"] == "2000-01-01" and series[0]["end"] == "2012-01-01"
    assert [h["settings"]["NonbankAffiliatesOnly"] for h in series[0]["definition_history"]] == ["1", "0"]
    assert series[0]["definition_history"][1]["from"] == "2009-01-01"
    assert t.series == 1 and t.requests == 80 and t.requests_ok == 2
    assert set(levels(d)) == {"info"}


def test_refusals_are_saved_after_each_table_and_skipped_next_run():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])])
    run_table(fake)                                   # no extra save call: the table saves its own memory
    assert len(memory()) == 79 and all("Invalid combination" in m["message"] for m in memory().values())
    assert Path("reports/shapes_MNE.json").exists()

    fake.calls.clear()
    d, t = run_table(fake)
    assert fake.calls == [NEW_YEARS] and t.series == 1
    assert t.check == "80 request shapes, 79 known-empty skipped"
    report = json.loads(Path("reports/shapes_MNE.json").read_text())["4"]
    assert sum(r["status"] == "skipped" for r in report) == 79


def test_memory_survives_an_interrupted_run(monkeypatch):
    run_table(FakeBEA([(NEW_YEARS, [row(2010)])]))
    d = download.Downloader(FakeBEA(), {"datasets": {"MNE": MNE_CFG}})
    monkeypatch.setattr(d, "_get", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        d.download_table("MNE", MNE_CFG, "5", "", {"SeriesID": "5", **BASE})
    assert len(memory("4")) == 79                     # the finished table is still remembered


def test_memory_depends_on_the_whole_request_and_expires():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])])
    run_table(fake)
    fake.calls.clear()
    run_table(fake, base={"Country": "650", "Year": "2015"})     # different request: nothing is skipped
    assert len(fake.calls) == 80

    old = (date.today() - timedelta(days=download.SHAPE_MEMORY_DAYS + 1)).isoformat()
    path = download.shape_memory_path("MNE")
    data = json.loads(path.read_text())
    for entry in data["4"].values():
        entry["checked"] = old
    path.write_text(json.dumps(data))
    fake.calls.clear()
    run_table(fake)
    assert len(fake.calls) == 80                       # expired refusals are asked again


def test_refresh_tries_everything_and_forgets_shapes_that_now_work():
    run_table(FakeBEA([(NEW_YEARS, [row(2010)])]))
    fake = FakeBEA([(NEW_YEARS, [row(2010)]), (OLD_YEARS, [row(2005)])])  # BEA added the old years
    d, t = run_table(fake, use_cache=False)
    assert len(fake.calls) == 80 and t.requests_ok == 2
    assert len(memory()) == 78 and download.shape_key({"SeriesID": "4", **BASE, **OLD_YEARS}) not in memory()


def test_no_answer_is_a_warning_is_never_remembered_and_is_tried_again():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])], flaky=[OLD_YEARS])
    d, t = run_table(fake)
    warnings = [i for i in d.issues if i.level == "warning"]
    assert len(warnings) == 1 and "1 of 80" in warnings[0].issue and "read timed out" in warnings[0].issue
    assert download._status_word(t) == "partial" and t.unanswered == 1
    assert download.shape_key({"SeriesID": "4", **BASE, **OLD_YEARS}) not in memory()

    fake.flaky.clear()
    fake.calls.clear()
    run_table(fake)
    assert OLD_YEARS in fake.calls


def test_no_answer_for_everything_is_an_error():
    d, t = run_table(FakeBEA(flaky=shapes_for("4")))
    assert levels(d) == ["error"] and "80 of 80" in d.issues[0].issue and "rerun" in d.issues[0].issue
    assert download._status_word(t) == "FAIL"


def test_nothing_works_says_why_even_on_a_rerun():
    d, t = run_table(FakeBEA())
    assert levels(d) == ["error"] and "Invalid combination" in d.issues[0].issue
    d, t = run_table(FakeBEA())                        # everything skipped this time
    assert levels(d) == ["error"] and "80 skipped" in d.issues[0].issue and "Invalid combination" in d.issues[0].issue


def test_discontinued_series_with_no_data_is_info():
    d, t = run_table(FakeBEA(), table="16")
    assert levels(d) == ["info"] and "discontinued" in d.issues[0].issue


def test_our_own_bug_is_an_error_and_not_remembered():
    d, t = run_table(FakeBEA([(NEW_YEARS, [row(2010)])], broken=[OLD_YEARS]))
    assert "error" in levels(d) and any("Unexpected error" in i.issue for i in d.issues)
    assert download.shape_key({"SeriesID": "4", **BASE, **OLD_YEARS}) not in memory()


def test_no_answer_keeps_the_last_good_download():
    run_table(FakeBEA([(NEW_YEARS, [row(y) for y in range(2009, 2013)])]))
    before = Path("data/corpus/MNE/series/4.json").read_text()
    d, t = run_table(FakeBEA(flaky=[NEW_YEARS]), use_cache=False)    # --refresh during an outage
    assert Path("data/corpus/MNE/series/4.json").read_text() == before
    assert any("Kept the previous download" in i.issue for i in d.issues)


def test_sample_run_updates_the_shape_report_instead_of_replacing_it():
    run_table(FakeBEA([(NEW_YEARS, [row(2010)])]), table="4")
    run_table(FakeBEA([(NEW_YEARS, [row(2010, series="5")])]), table="5")
    assert set(json.loads(Path("reports/shapes_MNE.json").read_text())) == {"4", "5"}


def test_probe_that_gets_no_answer_is_logged_elsewhere_too():
    d = download.Downloader(FakeBEA(flaky=[{"GeoFips": "COUNTY"}]), {"datasets": {}})
    t = download.TableResult("CAINC1", "")
    assert d._get("Regional", t, {"GeoFips": "COUNTY"}, probe=True) is None
    assert levels(d) == ["warning"] and t.unanswered == 1
    assert d._get("Regional", t, {"GeoFips": "MSA"}, probe=True) is None   # plain "no such data": quiet
    assert len(d.issues) == 1


def test_split_no_answer_is_not_no_data():
    d = download.Downloader(FakeBEA([({"AreaOrCountry": "China"}, [row(2010)])], flaky=[{"AreaOrCountry": "Japan"}]),
                            {"datasets": {}})
    t = download.TableResult("Mofas", "")
    got = d._split("IntlServSTA", t, {}, "AreaOrCountry", ["China", "Japan", "Narnia"])
    assert len(got) == 1 and t.missing == ["Narnia"] and t.unanswered == 1
    assert any(i.level == "warning" and "didn't answer" in i.issue and "Japan" in i.issue for i in d.issues)
