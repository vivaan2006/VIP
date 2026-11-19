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


ALL_ENTRIES = MNE_CFG["request_shapes"] + MNE_CFG["extra_request_shapes"]


def shapes_for(table):
    return download.expand_shapes(ALL_ENTRIES, table)


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
    assert download.expand_shapes(MNE_CFG["request_shapes"], series) == ORIGINAL_25


@pytest.mark.parametrize("series, count", [("1", 70), ("2", 49), ("4", 72), ("9", 35), ("16", 38), ("22", 27),
                                           ("30", 26), ("60", 54), ("62", 28), ("64", 27), ("45", 25)])
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
    assert directions("60") == {"outward", "inward"}     # outward per BEA, plus inward by-destination (supplied)
    assert directions("61") == {"outward"}
    assert directions("2") == {"inward", "state"}        # gross PPE: inward, and by state
    assert directions("20") == {"parent"}
    assert directions("9") == {"state"}


def test_classifications_go_only_to_the_series_they_describe():
    def classes(series):
        return {s["Classification"] for s in shapes_for(series)}
    assert {"CountryByDestination", "IndustryByDestination"} <= classes("60")     # goods / services supplied
    assert "CountryByDestination" not in classes("4") and "IndustryofSales" in classes("4")
    assert "IndustryofSales" not in classes("1") and "TypeofExpenditure" not in classes("64")
    assert OLD_YEARS in shapes_for("4") and OLD_YEARS in shapes_for("16")


def test_bea_own_call_for_services_supplied_is_tried():
    assert {"DirectionOfInvestment": "outward", "Classification": "CountryByDestination", "Country": "000",
            "OwnershipLevel": "0", "NonbankAffiliatesOnly": "0"} in shapes_for("60")


def test_null_leaves_a_parameter_out():
    shape = {**ORIGINAL_25[-1], "Country": None}
    assert shape in shapes_for("9")
    assert "Country" not in download.with_shape({"SeriesID": "9", **BASE}, shape)


# ---------- what happens on a run ----------

def test_old_and_new_years_join_and_the_change_is_marked():
    fake = FakeBEA([(OLD_YEARS, [row(y) for y in range(2000, 2009)]),
                    (NEW_YEARS, [row(y) for y in range(2009, 2013)])])
    d, t = run_table(fake)
    series = json.loads(Path("data/corpus/MNE/series/4.json").read_text())["series"]
    assert len(series) == 1 and series[0]["start"] == "2000-01-01" and series[0]["end"] == "2012-01-01"
    assert [h["settings"]["NonbankAffiliatesOnly"] for h in series[0]["definition_history"]] == ["1", "0"]
    assert series[0]["definition_history"][1]["from"] == "2009-01-01"
    assert t.series == 1 and t.requests == 72 and t.requests_ok == 2
    assert set(levels(d)) == {"info"}


def test_refusals_are_saved_after_each_table_and_skipped_next_run():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])])
    run_table(fake)                                   # no extra save call: the table saves its own memory
    assert len(memory()) == 71 and all("Invalid combination" in m["message"] for m in memory().values())
    assert Path("reports/shapes_MNE.json").exists()

    fake.calls.clear()
    d, t = run_table(fake)
    assert fake.calls == [NEW_YEARS] and t.series == 1
    assert t.check == "72 request shapes, 71 known-empty skipped"
    report = json.loads(Path("reports/shapes_MNE.json").read_text())["4"]
    assert sum(r["status"] == "skipped" for r in report) == 71


def test_memory_survives_an_interrupted_run(monkeypatch):
    run_table(FakeBEA([(NEW_YEARS, [row(2010)])]))
    d = download.Downloader(FakeBEA(), {"datasets": {"MNE": MNE_CFG}})
    monkeypatch.setattr(d, "_get", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        d.download_table("MNE", MNE_CFG, "5", "", {"SeriesID": "5", **BASE})
    assert len(memory("4")) == 71                     # the finished table is still remembered


def test_memory_depends_on_the_whole_request_and_expires():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])])
    run_table(fake)
    fake.calls.clear()
    run_table(fake, base={"Country": "650", "Year": "2015"})     # different request: nothing is skipped
    assert len(fake.calls) == 72

    old = (date.today() - timedelta(days=download.SHAPE_MEMORY_DAYS + 1)).isoformat()
    path = download.shape_memory_path("MNE")
    data = json.loads(path.read_text())
    for entry in data["4"].values():
        entry["checked"] = old
    path.write_text(json.dumps(data))
    fake.calls.clear()
    run_table(fake)
    assert len(fake.calls) == 72                       # expired refusals are asked again


def test_refresh_tries_everything_and_forgets_shapes_that_now_work():
    run_table(FakeBEA([(NEW_YEARS, [row(2010)])]))
    fake = FakeBEA([(NEW_YEARS, [row(2010)]), (OLD_YEARS, [row(2005)])])  # BEA added the old years
    d, t = run_table(fake, use_cache=False)
    assert len(fake.calls) == 72 and t.requests_ok == 2
    assert len(memory()) == 70 and download.shape_key({"SeriesID": "4", **BASE, **OLD_YEARS}) not in memory()


def test_no_answer_is_a_warning_is_never_remembered_and_is_tried_again():
    fake = FakeBEA([(NEW_YEARS, [row(2010)])], flaky=[OLD_YEARS])
    d, t = run_table(fake)
    warnings = [i for i in d.issues if i.level == "warning"]
    assert len(warnings) == 1 and "1 of 72" in warnings[0].issue and "read timed out" in warnings[0].issue
    assert download._status_word(t) == "partial" and t.unanswered == 1
    assert download.shape_key({"SeriesID": "4", **BASE, **OLD_YEARS}) not in memory()

    fake.flaky.clear()
    fake.calls.clear()
    run_table(fake)
    assert OLD_YEARS in fake.calls


def test_no_answer_for_everything_is_an_error():
    d, t = run_table(FakeBEA(flaky=shapes_for("4")))
    assert levels(d) == ["error"] and "72 of 72" in d.issues[0].issue and "rerun" in d.issues[0].issue
    assert download._status_word(t) == "FAIL"


def test_nothing_works_says_why_even_on_a_rerun():
    d, t = run_table(FakeBEA())
    assert levels(d) == ["error"] and "Invalid combination" in d.issues[0].issue
    d, t = run_table(FakeBEA())                        # everything skipped this time
    assert levels(d) == ["error"] and "72 skipped" in d.issues[0].issue and "Invalid combination" in d.issues[0].issue


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


# ---------- round 2 ----------

def test_an_extra_shape_with_the_same_numbers_never_takes_an_existing_id():
    by_country_industry = {"DirectionOfInvestment": "outward", "Classification": "CountryByIndustry",
                           "OwnershipLevel": "0", "NonbankAffiliatesOnly": "0"}
    by_country = {**by_country_industry, "Classification": "IndustryByCountry"}   # extra, sorts first
    rows = [row(y, value=str(1000 + y)) for y in range(2009, 2014)]
    run_table(FakeBEA([(by_country_industry, rows), (by_country, rows)]))
    series = json.loads(Path("data/corpus/MNE/series/4.json").read_text())["series"]
    assert len(series) == 1 and ".CountryByIndustry." in series[0]["series_id"]
    assert ".IndustryByCountry." in series[0]["also_as"][0]["series_id"]
    assert "_rank" not in series[0]


def test_overlapping_years_keep_the_newer_setting_without_splitting():
    old = [row(y) for y in range(2000, 2010)]
    old[-1] = {**old[-1], "Column": "All Industries (nonbank)", "DataValue": "1"}   # 2009 again, other label
    fake = FakeBEA([(NEW_YEARS, [row(y) for y in range(2009, 2013)]), (OLD_YEARS, old)])
    d, t = run_table(fake)
    series = json.loads(Path("data/corpus/MNE/series/4.json").read_text())["series"]
    assert len(series) == 1 and ".m" not in series[0]["series_id"].removeprefix("bea.mne")
    assert series[0]["values"][series[0]["dates"].index("2009-01-01")] == 5000.0   # NonbankAffiliatesOnly 0 kept
    assert not [i for i in d.issues if i.level == "warning"]


def test_kept_download_is_what_the_report_shows():
    run_table(FakeBEA([(NEW_YEARS, [row(y) for y in range(2009, 2013)])]))
    d, t = run_table(FakeBEA(flaky=[NEW_YEARS]), use_cache=False)
    assert t.series == 1 and t.observations == 4 and "kept previous download" in t.check


def test_interrupted_series_keeps_most_of_what_it_learned(monkeypatch):
    fake = FakeBEA()
    real = fake.get_data

    def stop_late(dataset, **request):
        if len(fake.calls) >= 45:
            raise KeyboardInterrupt
        return real(dataset, **request)
    monkeypatch.setattr(fake, "get_data", stop_late)
    with pytest.raises(KeyboardInterrupt):
        run_table(fake)
    assert len(memory()) == 40                          # saved every 20 shapes


def test_damaged_memory_file_does_not_stop_the_run():
    path = download.shape_memory_path("MNE")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"4": ["not", "a", "dict"], "5": {"x": {"checked": None}}}))
    d, t = run_table(FakeBEA([(NEW_YEARS, [row(2010)])]))
    assert t.series == 1


def test_each_call_starts_clean():
    fake = FakeBEA([(NEW_YEARS, [])], flaky=[OLD_YEARS])
    d = download.Downloader(fake, {"datasets": {}})
    t = download.TableResult("4", "")
    d._get("MNE", t, OLD_YEARS, probe=True, log_transient=False)
    assert d._last_transient
    d._get("MNE", t, NEW_YEARS, probe=True)              # BEA answers (no rows) after a no-answer
    assert not d._last_transient and not d._last_error and not d._last_bug


def test_restandardize_keeps_history_and_ids():
    fake = FakeBEA([(OLD_YEARS, [row(y) for y in range(2000, 2009)]),
                    (NEW_YEARS, [row(y) for y in range(2009, 2013)])])
    run_table(fake)
    before = json.loads(Path("data/corpus/MNE/series/4.json").read_text())["series"]
    Path("config").mkdir()
    Path("config/datasets.yaml").write_text(yaml.safe_dump({"datasets": {"MNE": MNE_CFG}}))
    download.restandardize("config/datasets.yaml")
    after = json.loads(Path("data/corpus/MNE/series/4.json").read_text())["series"]
    assert [s["series_id"] for s in after] == [s["series_id"] for s in before]
    assert after[0]["definition_history"] == before[0]["definition_history"]


class LinesDown(FakeBEA):
    """Regional: BEA can't be reached for the list of a table's lines."""

    def get_parameter_values_filtered(self, *a, **k):
        raise BEATransientError("Network problem, gave up after 4 tries")


def test_regional_outage_on_the_line_list_keeps_the_last_download():
    path = Path("data/corpus/Regional/series/CAINC1.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"series": [{"series_id": "x", "n_obs": 3}]}))
    d = download.Downloader(LinesDown(), {"datasets": {}})
    t = d.download_table("Regional", {"table_param": "TableName"}, "CAINC1", "", {"TableName": "CAINC1"})
    assert path.exists() and t.series == 1 and t.unanswered == 1


def test_original_shape_keeps_its_id_even_when_the_extra_sorts_first():
    original = {"DirectionOfInvestment": "outward", "Classification": "CountryByIndustry"}
    extra = {"DirectionOfInvestment": "outward", "Classification": "CountryByDestination",
             "OwnershipLevel": "0", "NonbankAffiliatesOnly": "0"}   # ".CountryByDestination." < ".CountryByIndustry."
    rows = [row(y, value=str(1000 + y), series="60") for y in range(2009, 2014)]
    run_table(FakeBEA([(original, rows), (extra, rows)]), table="60")
    series = json.loads(Path("data/corpus/MNE/series/60.json").read_text())["series"]
    assert len(series) == 1 and ".CountryByIndustry." in series[0]["series_id"]


class WholeTableBusy(FakeBEA):
    """BEA times out on the whole table but answers one frequency at a time."""

    def get_data(self, dataset, use_cache=True, **request):
        self.calls.append(request)
        if "," in request.get("Frequency", ""):
            raise BEATransientError("Network problem: read timed out")
        return {"Data": [{"TableName": "T1", "SeriesCode": "A", "LineNumber": "1", "LineDescription": "GDP",
                          "TimePeriod": "2020" if request["Frequency"] == "A" else "2020Q1", "DataValue": "1"}]}


def test_whole_table_no_answer_covered_by_smaller_requests_is_complete():
    d = download.Downloader(WholeTableBusy(), {"datasets": {}})
    t = d.download_table("NIPA", {"table_param": "TableName"}, "T1", "", {"TableName": "T1", "Frequency": "A,Q"})
    assert t.series == 2 and t.unanswered == 0 and download._status_word(t) == "ok"


def test_one_by_one_no_answer_is_not_no_data():
    fake = FakeBEA([({"AreaOrCountry": "China"}, [row(2010)])], flaky=[{"AreaOrCountry": "Japan"}])
    d = download.Downloader(fake, {"datasets": {}})
    t = download.TableResult("BalGds", "")
    still = d._ask_one_by_one("ITA", t, {}, "AreaOrCountry", ["China", "Japan", "Narnia"], [])
    assert still == ["Narnia"] and t.unanswered == 1
