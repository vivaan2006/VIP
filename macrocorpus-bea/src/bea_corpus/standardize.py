"""Turn raw BEA `Results` into the MacroCorpus standard format.

Two tables come out of every dataset, whatever its native shape:

observations : series_id, date, value            (long format, one row per point)
metadata     : one row per series (source, dataset, table, description,
               frequency, units, seasonal adjustment, coverage, ...)

`value` is kept exactly as published; multiply by 10**unit_mult for
base units (e.g. unit_mult=6 means the value is in millions).
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

MISSING_MARKERS = {"", "(NA)", "(D)", "(NM)", "(L)", "(X)", "---", "n.a.", "NA"}

# Columns each dataset uses to tell series apart within one GetData response.
SERIES_KEYS = {
    "NIPA": ["LineNumber"],
    "NIUnderlyingDetail": ["LineNumber"],
    "FixedAssets": ["LineNumber"],
    "Regional": ["GeoFips"],
    "ITA": ["TimeSeriesId", "Frequency"],
    "IIP": ["TimeSeriesId", "Frequency"],
    "IntlServTrade": ["TimeSeriesId"],
    "GDPbyIndustry": ["Industry", "TableID"],
}

DESCRIPTION_KEYS = ["LineDescription", "TimeSeriesDescription", "IndustryDescription"]


@dataclass
class SeriesSpec:
    """One entry from config/series.yaml."""

    id: str
    dataset: str
    params: dict
    name: str = ""
    category: str = ""
    seasonal_adjustment: str = ""
    transform: str = "none"
    select: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "SeriesSpec":
        return cls(**d)


def parse_value(raw) -> float:
    if raw is None:
        return np.nan
    s = str(raw).strip()
    if s in MISSING_MARKERS:
        return np.nan
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return np.nan


_PERIOD = re.compile(r"^(\d{4})(?:([QM])(\d{1,2}))?$")


def parse_period(tp: str) -> tuple[pd.Timestamp, str]:
    """'2024' -> (2024-01-01, 'A'); '2024Q3' -> (2024-07-01, 'Q'); '2024M7' -> (2024-07-01, 'M')."""
    m = _PERIOD.match(str(tp).strip())
    if not m:
        raise ValueError(f"Unrecognised TimePeriod: {tp!r}")
    year, kind, n = int(m.group(1)), m.group(2), m.group(3)
    if kind is None:
        return pd.Timestamp(year, 1, 1), "A"
    n = int(n)
    if kind == "Q":
        return pd.Timestamp(year, 3 * (n - 1) + 1, 1), "Q"
    return pd.Timestamp(year, n, 1), "M"


def standardize(results: dict, spec: SeriesSpec, retrieved: date | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = results.get("Data", [])
    if not rows:
        raise ValueError(f"{spec.id}: response contained no data")
    df = pd.DataFrame(rows)
    df = _apply_selection(df, spec)

    parsed = df["TimePeriod"].map(parse_period)
    df["date"] = parsed.map(lambda p: p[0])
    df["freq"] = parsed.map(lambda p: p[1])
    df["value"] = df["DataValue"].map(parse_value)

    keys = [k for k in SERIES_KEYS.get(spec.dataset, []) if k in df.columns] + ["freq"]
    groups = df.groupby(keys, sort=False)

    obs_frames, meta_rows = [], []
    multi = df.groupby(keys).ngroups > 1
    for key, g in groups:
        key = key if isinstance(key, tuple) else (key,)
        id_parts = key if df["freq"].nunique() > 1 else key[:-1]  # frequency only when the request mixes them
        sid = spec.id if not multi else f"{spec.id}.{'.'.join(_slug(k) for k in id_parts)}"
        g = g.sort_values("date", kind="stable")
        repeated = g[g.duplicated("date", keep=False)]
        if repeated.groupby("date")["value"].nunique(dropna=False).gt(1).any():
            warnings.warn(f"{sid}: BEA sent different numbers for the same date; kept the first")
        g = g.drop_duplicates("date", keep="first")
        obs_frames.append(pd.DataFrame({"series_id": sid, "date": g["date"], "value": g["value"]}))
        meta_rows.append(_metadata_row(sid, g, spec, results, retrieved))

    obs = pd.concat(obs_frames, ignore_index=True)
    return obs, pd.DataFrame(meta_rows)


def _apply_selection(df: pd.DataFrame, spec: SeriesSpec) -> pd.DataFrame:
    sel = spec.select or {}
    if "line" in sel:
        out = df[df["LineNumber"].astype(str) == str(sel["line"])]
    elif "match" in sel:
        desc_col = next(c for c in DESCRIPTION_KEYS if c in df.columns)
        hits = df[df[desc_col].str.contains(sel["match"], case=False, regex=True, na=False)]
        lines = hits["LineNumber"].unique() if "LineNumber" in hits.columns else []
        if len(lines) > 1:
            # Several lines share the wording; keep the first (top-level) one.
            hits = hits[hits["LineNumber"] == lines[0]]
        out = hits
    else:
        out = df
    if "frequency" in sel and "Frequency" in out.columns:
        out = out[out["Frequency"] == sel["frequency"]]
    if out.empty:
        raise ValueError(f"{spec.id}: selection {sel} matched no rows")
    return out.copy()


def _metadata_row(sid, g: pd.DataFrame, spec: SeriesSpec, results: dict, retrieved) -> dict:
    first = g.iloc[0]
    desc_col = next((c for c in DESCRIPTION_KEYS if c in g.columns), None)
    description = first[desc_col] if desc_col else results.get("Statistic", spec.name)
    valid = g.dropna(subset=["value"])
    return {
        "series_id": sid,
        "name": spec.name or description,
        "source": "BEA",
        "country": "US",
        "geo": first.get("GeoName", "United States"),
        "geo_code": first.get("GeoFips", "00000"),
        "dataset": spec.dataset,
        "table": spec.params.get("TableName") or spec.params.get("Indicator") or spec.params.get("TableID", ""),
        "line": first.get("LineNumber", spec.params.get("LineCode", "")),
        "bea_series_code": first.get("SeriesCode") or first.get("TimeSeriesId") or first.get("Code", ""),
        "description": description,
        "category": spec.category,
        "frequency": first["freq"],
        "unit": first.get("CL_UNIT", results.get("UnitOfMeasure", "")),
        "unit_mult": int(parse_value(first.get("UNIT_MULT", 0)) or 0),
        "metric": first.get("METRIC_NAME", first.get("Metric_Name", "")),
        "seasonal_adjustment": spec.seasonal_adjustment,
        "transform": spec.transform,
        "first_date": valid["date"].min().date() if len(valid) else None,
        "last_date": valid["date"].max().date() if len(valid) else None,
        "n_obs": int(len(valid)),
        "retrieved": (retrieved or date.today()).isoformat(),
    }


def _slug(x) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(x)).strip("_")
