"""Turn a raw BEA GetData response (any of the 12 datasets) into standard JSON series.

Every BEA dataset returns a flat list of rows. Each row has:
  - some "dimension" columns that say WHICH series it belongs to
    (NIPA: TableName/LineNumber/SeriesCode, Regional: Code/GeoFips, ITA: Indicator/AreaOrCountry/...)
  - some time column(s)   (TimePeriod like 2024Q3, or Year + Quarter for GDPbyIndustry)
  - the number            (DataValue, as text with commas and codes like "(D)")
  - unit info             (CL_UNIT, UNIT_MULT)

So the same recipe works for all of them: group rows by their dimension columns,
sort by date, and write one JSON record per group. No per-dataset parsing code needed;
anything new BEA adds is picked up automatically.

Output record (see schema/series.schema.json and docs/json_format.md):
{"series_id", "source", "country", "dataset", "table", "table_title", "title",
 "frequency", "unit", "unit_multiplier", "metric", "dimensions",
 "start", "end", "n_obs", "n_missing", "missing_codes", "retrieved", "request",
 "dates": [...], "values": [...]}
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import date

SCHEMA_VERSION = "0.1"

TIME_COLS = {"TimePeriod", "Year", "Quarter"}
# Columns that describe the number, not which series it is.
NOT_DIMENSIONS = TIME_COLS | {"DataValue", "NoteRef", "CL_UNIT", "UNIT_MULT",
                              "METRIC_NAME", "Metric_Name", "UnitOfMeasure"}
# BEA's codes for "no number here". (*) = nonzero but rounds to 0 (MNE).
MISSING_CODES = {"", "(NA)", "(D)", "(NM)", "(L)", "(X)", "(S)", "(*)", "---", "n.a.", "n.s.", "NA"}

_PERIOD = re.compile(r"^(\d{4})(?:([QMA])(\d{1,2}))?$")
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}


# ---------- small parsers ----------

def parse_number(raw) -> tuple[float | None, str | None]:
    """Return (value, missing_code). '1,234.5' -> (1234.5, None); '(D)' -> (None, '(D)')."""
    if raw is None:
        return None, ""
    s = str(raw).strip()
    if s in MISSING_CODES:
        return None, s
    try:
        return float(s.replace(",", "")), None
    except ValueError:
        return None, s


def parse_time(row: dict) -> tuple[str, str] | None:
    """Return (ISO date of period start, frequency A/Q/M), or None if unreadable."""
    tp = row.get("TimePeriod")
    if tp:
        m = _PERIOD.match(str(tp).strip())
        if m:
            year, kind, n = int(m.group(1)), m.group(2), m.group(3)
            if kind in (None, "A"):
                return f"{year}-01-01", "A"
            n = int(n)
            if kind == "Q" and 1 <= n <= 4:
                return f"{year}-{3 * (n - 1) + 1:02d}-01", "Q"
            if kind == "M" and 1 <= n <= 12:
                return f"{year}-{n:02d}-01", "M"
        return None
    # GDPbyIndustry style: Year + Quarter ("I".."IV", or the year itself for annual rows)
    year = str(row.get("Year", "")).strip()
    if not year.isdigit():
        return None
    q = str(row.get("Quarter", "")).strip().upper()
    qn = _ROMAN.get(q) or (int(q) if q.isdigit() and 1 <= int(q) <= 4 else None)
    if qn:
        return f"{year}-{3 * (qn - 1) + 1:02d}-01", "Q"
    return f"{year}-01-01", "A"


# Columns that name or describe a series. They can change over time (East Timor -> Timor-Leste),
# so they never decide which series a row belongs to; they're kept as metadata instead.
LABEL_COLS = {"row", "column", "columnparent", "columngparent", "geoname"}
# Columns that are neither identity nor labels: display positions, scale text, extra value columns.
IGNORE_COLS = {"tablescale", "datavalueunformatted"}


def _is_label(col: str) -> bool:
    c = col.lower()
    return "desc" in c or "name" in c or c == "title" or c in LABEL_COLS


def _is_ignored(col: str) -> bool:
    c = col.lower()
    return c in IGNORE_COLS or "displayorder" in c


def _slug(x) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(x)).strip("_")


def _hash(x) -> str:
    return hashlib.sha1(repr(x).encode()).hexdigest()[:8]


def _unit_mult(raw) -> int:
    try:
        return int(float(str(raw).replace(",", "")))
    except (TypeError, ValueError):
        return 0


_SCALE = {"thousand": 3, "million": 6, "billion": 9, "trillion": 12}


def unit_of(row: dict, results: dict) -> tuple[str, int]:
    """(unit, power-of-ten multiplier) for one row.

    Most datasets send CL_UNIT/UNIT_MULT on every row. MNE only sends text like
    "Millions of Dollars" in TableScale, so read the scale and currency out of that.
    """
    unit = row.get("CL_UNIT") or results.get("UnitOfMeasure") or ""
    mult = _unit_mult(row.get("UNIT_MULT", 0))
    scale = str(row.get("TableScale") or "").strip()
    if scale:
        low = scale.lower()
        if not row.get("UNIT_MULT"):
            mult = next((p for word, p in _SCALE.items() if word in low), 0)
        if not unit:
            unit = "USD" if "dollar" in low else scale
    return unit, mult


# ---------- main ----------

def dimension_columns(rows: list[dict], results: dict) -> list[str]:
    """Every column that says which series a row belongs to (codes and labels). Looks at all rows."""
    cols: dict[str, None] = {}
    for r in rows:
        for k in r:
            cols.setdefault(k, None)
    # BEA lists its columns in Results.Dimensions; IsValue=1 means "this is a number column".
    value_cols = {d.get("Name") for d in results.get("Dimensions", []) or []
                  if isinstance(d, dict) and str(d.get("IsValue")) == "1"}
    return [c for c in cols if c not in NOT_DIMENSIONS and c not in value_cols and not _is_ignored(c)]


def to_series(results: dict, dataset: str, table: str, request: dict | None = None,
              retrieved: date | None = None, identity_params: list[str] | None = None) -> tuple[list[dict], dict]:
    """Convert one GetData `Results` object into standard series records (see table_to_series)."""
    return table_to_series([(request or {}, results)], dataset, table, retrieved, identity_params)


def table_to_series(responses: list[tuple[dict, dict]], dataset: str, table: str,
                    retrieved: date | None = None,
                    identity_params: list[str] | None = None) -> tuple[list[dict], dict]:
    """Convert every response for one table into standard series records.

    All responses are merged first, so a series that shows up in two requests (state totals in both
    the STATE and COUNTY pulls, a one-by-one retry) is one record, not two.

    How rows are grouped into series:
      - by CODE columns only (SeriesCode, RowCode, GeoFips, Industry, ...) plus frequency, so a series
        whose name changed over time keeps one unbroken history. Every name it had is kept in
        `label_history`.
      - `identity_params` are request settings that aren't in the rows but change what the rows mean
        (MNE's DirectionOfInvestment: the same RowCode is a different series inward vs outward).
      - if two rows share the codes AND the date but carry different labels and different numbers,
        the labels are telling apart different measures (GDPbyIndustry table 25 lists energy inputs,
        value added, ... all under industry 11). Those are split by label and the id says so.
      - two rows with the same codes, labels and date: equal numbers are a harmless repeat;
        different numbers are a conflict. The first is kept and the conflict is counted, never hidden.

    Returns (records, stats).
    """
    identity_params = list(identity_params or [])
    retrieved_s = (retrieved or date.today()).isoformat()
    stats = Counter()
    conflicts: list[str] = []

    # all rows, each remembering which request it came from
    entries = []
    columns: dict[str, None] = {}
    for i, (request, results) in enumerate(responses):
        rows = results.get("Data") or []
        if isinstance(rows, dict):
            rows = [rows]
        for c in dimension_columns(rows, results):
            columns.setdefault(c, None)
        for r in rows:
            entries.append((r, i))
    stats["rows"] = len(entries)
    code_cols = [c for c in columns if not _is_label(c)]
    label_cols = [c for c in columns if _is_label(c)]
    id_params = [p for p in identity_params if p not in columns]

    # 1. group by codes + frequency
    groups: dict[tuple, list] = defaultdict(list)
    for r, i in entries:
        t = parse_time(r)
        if t is None:
            stats["bad_time"] += 1
            continue
        d, freq = t
        request = responses[i][0]
        codes = tuple(str(r.get(c, "")).strip() for c in code_cols) + \
            tuple(str(request.get(p, "")).strip() for p in id_params)
        labels = tuple(str(r.get(c, "")).strip() for c in label_cols)
        v, code = parse_number(r.get("DataValue"))
        groups[codes + (freq,)].append((d, labels, v, code, r, i))

    # 2. split a code group by label only where labels mark different measures
    series_groups: list[tuple[tuple, tuple | None, list]] = []
    for key, items in groups.items():
        by_date: dict[str, dict[tuple, float | None]] = defaultdict(dict)
        for d, labels, v, *_ in items:
            by_date[d].setdefault(labels, v)
        measures = any(len(set(vals.values())) > 1 and len(vals) > 1 for vals in by_date.values())
        if measures:
            stats["measure_splits"] += 1
            by_label: dict[tuple, list] = defaultdict(list)
            for it in items:
                by_label[it[1]].append(it)
            series_groups += [(key, lab, its) for lab, its in by_label.items()]
        else:
            series_groups.append((key, None, items))

    # 3. one record per group
    table_title = ""
    for _, res in responses:
        table_title = table_title or res.get("PublicTable") or res.get("Statistic") or ""
    records, used_ids = [], set()
    for key, measure, items in series_groups:
        freq = key[-1]
        code_values = dict(zip(code_cols + id_params, key[:-1]))
        chosen: dict[str, tuple] = {}
        for it in items:
            d, labels, v = it[0], it[1], it[2]
            if d not in chosen:
                chosen[d] = it
                continue
            if chosen[d][2] == v and chosen[d][3] == it[3]:
                stats["duplicates"] += 1
            else:
                stats["conflicts"] += 1
                if len(conflicts) < 20:
                    conflicts.append(f"{'.'.join(key[:-1])} {d}: kept {chosen[d][2]}, also got {v}")
        dates = sorted(chosen)
        values, missing = [], Counter()
        for d in dates:
            values.append(chosen[d][2])
            if chosen[d][2] is None:
                missing[chosen[d][3] or "blank"] += 1
        observed = [d for d, v in zip(dates, values) if v is not None]

        # labels over time: the latest names go in `dimensions`, every change in `label_history`
        history = []
        for d in dates:
            labels = dict(zip(label_cols, chosen[d][1]))
            if not history or history[-1]["labels"] != labels:
                history.append({"labels": labels, "from": d, "to": d})
            else:
                history[-1]["to"] = d
        latest = history[-1]["labels"] if history else {}
        if len(history) > 1:
            stats["renamed"] += 1

        # units over time, same idea
        last_row, last_i = chosen[dates[-1]][4], chosen[dates[-1]][5]
        unit, mult = unit_of(last_row, responses[last_i][1])
        units_seen = list(dict.fromkeys(unit_of(chosen[d][4], responses[chosen[d][5]][1]) for d in dates))
        if len(units_seen) > 1:
            stats["unit_changes"] += 1

        # id: codes only, so it's the same every run and doesn't depend on row order
        code_part = ".".join(_slug(v) for v in code_values.values() if v)
        if not code_part or len(code_part) > 60:
            code_part = _hash(sorted(code_values.items()))
        sid = f"bea.{dataset.lower()}.{_slug(table)}.{code_part}"
        if measure is not None:
            sid += f".m{_hash(measure)}"
        sid += f".{freq}"
        if sid in used_ids:  # two different code sets slugged the same: tell them apart by a hash, not by order
            sid = f"{sid}~{_hash((key, measure))}"
        used_ids.add(sid)

        label_values = [v for v in latest.values() if v and v != "None"]
        first_row = chosen[dates[0]][4]
        record = {
            "schema_version": SCHEMA_VERSION,
            "series_id": sid,
            "source": "BEA",
            "country": "US",
            "dataset": dataset,
            "table": table,
            "table_title": table_title,
            "title": " | ".join(dict.fromkeys(label_values)) or table_title or table,
            "frequency": freq,
            "unit": unit,
            "unit_multiplier": mult,
            "metric": last_row.get("METRIC_NAME") or last_row.get("Metric_Name") or first_row.get("METRIC_NAME") or "",
            "dimensions": {**code_values, **latest},
            "start": observed[0] if observed else None,
            "end": observed[-1] if observed else None,
            "n_obs": len(observed),
            "n_missing": len(dates) - len(observed),
            "missing_codes": dict(missing),
            "retrieved": retrieved_s,
            "request": responses[items[0][5]][0] or {},
            "dates": dates,
            "values": values,
        }
        if len(history) > 1:
            record["label_history"] = history
        if len(units_seen) > 1:
            record["unit_history"] = [{"unit": u, "unit_multiplier": m} for u, m in units_seen]
        records.append(record)
    stats["series"] = len(records)
    out = dict(stats)
    if conflicts:
        out["conflict_examples"] = conflicts
    return records, out


def metadata_only(record: dict) -> dict:
    """The record without its data arrays (for the index file)."""
    return {k: v for k, v in record.items() if k not in ("dates", "values")}
