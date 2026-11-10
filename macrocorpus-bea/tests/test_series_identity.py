"""How rows become series: by codes, not by names that change over time."""

import random

from bea_corpus.standardize_json import table_to_series, to_series


def mne_row(year, code, name, value, order="1.00"):
    return {"Year": str(year), "SeriesID": "30", "SeriesName": "Position", "Row": name, "Column": "Position",
            "RowCode": code, "ColumnCode": "0000", "TableRowDisplayOrder": order,
            "TableScale": "Millions of Dollars", "DataValueUnformatted": str(value), "DataValue": f"{value:,}"}


def test_renamed_series_keeps_one_history():
    # same RowCode 660, BEA renamed it in 2009 and moved it in the display order
    rows = [mne_row(y, "660", "East Timor", 10 + y - 2002, "48.00") for y in range(2002, 2009)]
    rows += [mne_row(y, "660", "Timor-Leste", 20 + y - 2009, "52.00") for y in range(2009, 2026)]
    recs, stats = to_series({"Data": rows}, "MNE", "30")
    assert len(recs) == 1 and stats["renamed"] == 1
    r = recs[0]
    assert r["start"] == "2002-01-01" and r["end"] == "2025-01-01" and r["n_obs"] == 24
    assert r["dimensions"]["Row"] == "Timor-Leste"  # latest name
    assert [h["labels"]["Row"] for h in r["label_history"]] == ["East Timor", "Timor-Leste"]
    assert r["label_history"][0]["to"] == "2008-01-01"
    assert r["unit"] == "USD" and r["unit_multiplier"] == 6


def test_labels_that_mark_different_measures_are_split():
    # GDPbyIndustry table 25 style: industry 11 carries several measures, told apart only by the label
    rows = [{"TableID": "25", "Frequency": "A", "Year": str(y), "Quarter": str(y), "Industry": "11",
             "IndustrYDescription": label, "DataValue": str(v + y)}
            for y in range(2015, 2020) for label, v in (("Energy inputs", 1), ("Value added", 100))]
    recs, stats = to_series({"Data": rows}, "GDPbyIndustry", "25")
    assert len(recs) == 2 and stats["measure_splits"] == 1
    assert {r["title"] for r in recs} == {"Energy inputs", "Value added"}
    assert all(r["n_obs"] == 5 for r in recs)
    assert len({r["series_id"] for r in recs}) == 2 and not any("~" in r["series_id"] for r in recs)


def test_ids_do_not_depend_on_row_order():
    rows = [{"TableID": "25", "Frequency": "A", "Year": str(y), "Quarter": str(y), "Industry": ind,
             "IndustrYDescription": label, "DataValue": str(v)}
            for y in range(2015, 2020) for ind in ("11", "21")
            for label, v in (("Energy inputs", 1), ("Value added", 2), ("Compensation", 3))]
    ids = None
    for seed in range(5):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        recs, _ = to_series({"Data": shuffled}, "GDPbyIndustry", "25")
        got = {r["series_id"]: (r["title"], r["values"]) for r in recs}
        assert ids is None or got == ids
        ids = got


def test_duplicates_and_conflicts_are_counted_not_hidden():
    base = {"TableName": "T1", "SeriesCode": "A", "LineNumber": "1", "LineDescription": "GDP", "TimePeriod": "2020"}
    rows = [{**base, "DataValue": "5"}, {**base, "DataValue": "5"}, {**base, "TimePeriod": "2021", "DataValue": "6"},
            {**base, "TimePeriod": "2021", "DataValue": "7"}]
    recs, stats = to_series({"Data": rows}, "NIPA", "T1")
    assert len(recs) == 1 and recs[0]["values"] == [5.0, 6.0]  # first kept
    assert stats["duplicates"] == 1 and stats["conflicts"] == 1
    assert "kept 6.0, also got 7.0" in stats["conflict_examples"][0]


def test_same_series_in_two_responses_is_merged():
    row = {"Code": "CAINC1-1", "GeoFips": "13000", "GeoName": "Georgia", "CL_UNIT": "Dollars", "UNIT_MULT": "0"}
    state = ({"GeoFips": "STATE", "LineCode": "1"}, {"Data": [{**row, "TimePeriod": "2020", "DataValue": "1"}]})
    county = ({"GeoFips": "COUNTY", "LineCode": "1"}, {"Data": [{**row, "TimePeriod": "2020", "DataValue": "1"},
                                                                {**row, "TimePeriod": "2021", "DataValue": "2"}]})
    recs, stats = table_to_series([state, county], "Regional", "CAINC1")
    assert len(recs) == 1 and recs[0]["values"] == [1.0, 2.0] and stats["duplicates"] == 1


def test_identity_params_separate_directions():
    rows = [mne_row(2020, "100", "Canada", 5)]
    out = ({"DirectionOfInvestment": "outward"}, {"Data": rows})
    inw = ({"DirectionOfInvestment": "inward"}, {"Data": [mne_row(2020, "100", "Canada", 9)]})
    recs, stats = table_to_series([out, inw], "MNE", "8", identity_params=["DirectionOfInvestment"])
    assert len(recs) == 2 and not stats.get("conflicts")
    assert {r["dimensions"]["DirectionOfInvestment"]: r["values"][0] for r in recs} == {"outward": 5, "inward": 9}


def test_every_row_is_read_no_cap():
    # a column that only shows up after row 2,000 still counts toward which series a row belongs to
    rows = [{"Code": "X", "GeoFips": "01000", "TimePeriod": str(1000 + i), "DataValue": "1"} for i in range(2500)]
    rows += [{"Code": "X", "GeoFips": "01000", "Extra": "B", "TimePeriod": "2999", "DataValue": "2"}]
    recs, _ = to_series({"Data": rows}, "Regional", "T")
    assert len(recs) == 2 and sum(r["n_obs"] for r in recs) == 2501
