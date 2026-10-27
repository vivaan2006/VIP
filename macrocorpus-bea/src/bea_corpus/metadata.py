"""Metadata extraction for BEA table groups, so we can compare metadata granularity.

For each group in config/metadata_groups.yaml:
  1. parameters      GetParameterList                      -> bea_<ds>_parameters.json
  2. classifications GetParameterValues for each one       -> bea_<ds>_<Param>.json
  3. sample          GetData for a couple of years only     -> bea_<ds>_sample.json
                     (dimensions + notes + observations, each observation tagged with series_id)
  4. series          one entry per series in the sample     -> bea_<ds>_series_metadata.json
                     (source id, description, units, frequency, labels from step 2, notes)
  5. summary         per-dataset structure + checks         -> bea_metadata_summary.json

Files go to data/BEA/<Dataset>/, same layout as the reference NIPA/Regional/GDPbyIndustry/ITA code.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .client import BEAClient, BEAError
from .common import as_list, clean_text, desc_of, key_of
from .standardize_json import parse_number, parse_time

OUT = Path("data/BEA")

SOURCE_ID_FIELDS = ["TimeSeriesId", "SeriesCode"]
DESCRIPTION_FIELDS = ["TimeSeriesDescription", "LineDescription", "IndustrYDescription",
                      "IndustryDescription", "SeriesName", "Description", "Row"]
_BRACKET = re.compile(r"\[([^\]]+)\]")


# ---------- small helpers ----------

def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def _snake(name: str) -> str:
    name = name.strip()
    special = {"TableID": "table_id", "TableName": "table", "SeriesID": "series_id_code", "AreaOrCountry": "area_or_country"}
    if name in special:
        return special[name]
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def catalog_stats(values: list) -> dict:
    keys = [key_of(v) for v in values]
    nonempty = [k for k in keys if k]
    dupes = sorted({k for k in nonempty if nonempty.count(k) > 1})
    return {"values": len(values), "unique_keys": len(set(nonempty)),
            "duplicate_keys": dupes, "with_description": sum(1 for v in values if desc_of(v))}


# ---------- the core: works on any GetData result ----------

def build_series(dataset: str, results: dict, request: dict, cfg: dict,
                 catalogs: dict[str, list]) -> tuple[list[dict], list[dict], dict]:
    """Group a GetData result into series and link every observation to one.

    Returns (series_metadata, tagged_observations, checks).
    """
    rows = as_list(results.get("Data"))
    notes = {str(n.get("NoteRef", "")).strip().upper(): clean_text(n.get("NoteText", ""))
             for n in as_list(results.get("Notes")) if isinstance(n, dict)}
    id_fields = cfg["series_id"]
    labels = {f: _label_index(catalogs.get(param, [])) for f, param in (cfg.get("labels") or {}).items()}
    req_ci = {k.strip().lower(): v for k, v in request.items()}

    series: dict[str, dict] = {}
    values_at = defaultdict(lambda: defaultdict(set))  # id -> date -> distinct published values
    labels_at = defaultdict(lambda: defaultdict(set))  # id -> date -> row labels (line description / country)
    tagged, bad_time = [], 0

    for r in rows:
        t = parse_time(r)
        if t is None:
            bad_time += 1
            continue
        date, freq = t
        parts = [str(r.get(f, req_ci.get(f.lower(), ""))).strip() for f in id_fields]
        sid = ":".join(["BEA", dataset, *parts] + ([] if "Frequency" in id_fields else [freq]))

        raw_value = r.get("DataValueUnformatted", r.get("DataValue"))
        value, code = parse_number(raw_value)
        repeat = date in values_at[sid]
        values_at[sid][date].add(str(raw_value).replace(",", "").strip())
        labels_at[sid][date].add(clean_text(r.get("LineDescription") or r.get("Row") or r.get("TimeSeriesDescription")
                                        or r.get("IndustrYDescription") or "") + f" (line {r.get('LineNumber')})"
                                 * bool(r.get("LineNumber")))
        refs = [x.strip().upper() for x in str(r.get("NoteRef", "")).split(",") if x.strip()]
        tagged.append({"series_id": sid, **r})

        s = series.get(sid)
        if s is None:
            unit, mult, unit_source = _units(r)
            note_texts = [notes[x] for x in refs if x in notes]
            s = series[sid] = {
                "series_id": sid,
                "source_series_id": next((str(r[f]) for f in SOURCE_ID_FIELDS if r.get(f)), None),
                "dataset": dataset,
                "description": _describe(r),
                "classifications": {f: _classification(labels[f], str(r.get(f, req_ci.get(f.lower(), ""))).strip())
                                    for f in labels},
                "frequency": freq,
                "source_frequency": r.get("Frequency"),
                "unit": unit, "unit_multiplier": mult, "unit_source": unit_source,
                "units_in_notes": sorted({b for t_ in note_texts for b in _BRACKET.findall(t_)}),
                "metric": r.get("METRIC_NAME") or r.get("Metric_Name"),
                "line_number": r.get("LineNumber"),
                "notes": sorted(set(note_texts)),
                "n_obs": 0, "n_missing": 0, "missing_codes": {},
                "first_period": date, "last_period": date,
            }
        if repeat:  # same series printed twice in the table: count the date once
            continue
        s["n_obs"] += 1
        if value is None:
            s["n_missing"] += 1
            s["missing_codes"][code or "blank"] = s["missing_codes"].get(code or "blank", 0) + 1
        s["first_period"] = min(s["first_period"], date)
        s["last_period"] = max(s["last_period"], date)

    # An ID is only broken if two different numbers claim the same date.
    conflicts = sorted(sid for sid, by_date in values_at.items() if any(len(v) > 1 for v in by_date.values()))
    # Two labels on the same date = same series printed on two lines. Different dates = renamed over time.
    repeated, renamed = [], {}
    for sid, by_date in labels_at.items():
        if sid in conflicts:
            continue
        all_labels = sorted(set().union(*by_date.values()))
        if len(all_labels) < 2:
            continue
        if any(len(v) > 1 for v in by_date.values()):
            repeated.append(sid)
            series[sid]["appears_as"] = all_labels
        else:
            order = sorted(by_date)  # oldest label first
            seq = list(dict.fromkeys(next(iter(by_date[d])) for d in order))
            renamed[sid] = seq
            series[sid]["names_over_time"] = seq
    repeated.sort()
    linked = sum(1 for o in tagged if o["series_id"] in series)
    checks = {
        "observations": len(rows),
        "observations_linked_to_series": linked,
        "observations_with_bad_time": bad_time,
        "series": len(series),
        "ids_with_conflicting_values": len(conflicts),
        "conflict_examples": conflicts[:5],
        "ids_repeated_on_several_lines": len(repeated),
        "repeated_examples": repeated[:5],
        "ids_with_renamed_description": len(renamed),
        "renamed_examples": dict(list(renamed.items())[:3]),
        "canonical_id_status": "unique_in_sample" if not conflicts else "NOT_unique_in_sample",
    }
    return list(series.values()), tagged, checks


def _label_index(values: list) -> dict[str, list[str]]:
    """code -> every distinct description BEA gives it, in catalog order.

    Some catalogs reuse a code under several names (MNE Industry 5221 is "Banks",
    "Depository credit intermediation (banking)" and "Depository institutions (banking)").
    A plain dict would silently keep the last one, so we keep them all.
    """
    out: dict[str, list[str]] = defaultdict(list)
    for v in values:
        k, d = key_of(v), desc_of(v)
        if d and d not in out[k]:
            out[k].append(d)
    return dict(out)


def _classification(index: dict[str, list[str]], code: str) -> dict:
    names = index.get(code, [])
    out = {"code": code, "label": names[0] if names else ""}
    if len(names) > 1:
        out["alt_labels"] = names[1:]
    return out


def _describe(r: dict) -> str:
    """Series description. MNE rows only carry the table title in SeriesName, which is the
    same for every series, so add the row/column labels to make it series specific."""
    base = next((clean_text(r[f]) for f in DESCRIPTION_FIELDS if r.get(f)), "")
    if r.get("SeriesName") and base == clean_text(r["SeriesName"]):
        extra = [clean_text(r[f]) for f in ("Row", "ColumnGParent", "ColumnParent", "Column")
                 if r.get(f) and str(r[f]).strip() not in ("", "None")]
        base = "; ".join([base, *extra])
    return base


def _units(r: dict) -> tuple[str, int | None, str]:
    if r.get("CL_UNIT") not in (None, ""):
        try:
            mult = int(float(r.get("UNIT_MULT", 0)))
        except (TypeError, ValueError):
            mult = None
        return r["CL_UNIT"], mult, "CL_UNIT/UNIT_MULT"
    if r.get("TableScale"):  # MNE
        scale = r["TableScale"]
        mult = 6 if "million" in scale.lower() else 3 if "thousand" in scale.lower() else \
            9 if "billion" in scale.lower() else None
        return scale, mult, "TableScale"
    return "", None, "notes_only"


def findings_for(dataset: str, series: list[dict], stats: dict, results: dict, rows: list) -> list[str]:
    out = []
    if rows and "TimePeriod" not in rows[0]:
        out.append("No TimePeriod field; time comes from Year (and Quarter if present).")
    srcs = {s["unit_source"] for s in series}
    if "TableScale" in srcs:
        out.append("No CL_UNIT/UNIT_MULT; units only in TableScale text.")
    if "notes_only" in srcs:
        out.append("Units are only given in the table notes.")
    for s in series:
        text = " ".join(s["units_in_notes"]).lower()
        if s["unit_multiplier"] == 6 and "billion" in text:
            out.append("UNIT_MULT=6 (millions) but table note says billions: API returns millions.")
            break
    if any(s["source_series_id"] is None for s in series):
        out.append("No BEA series identifier in the rows; series must be identified by combining fields.")
    if stats.get("canonical_id_status") != "unique_in_sample":
        out.append(f"Proposed series ID is not unique: {stats['ids_with_conflicting_values']} IDs have two different values for the same date.")
    if stats.get("ids_repeated_on_several_lines"):
        out.append(f"{stats['ids_repeated_on_several_lines']} series are printed on more than one line of the table "
                   "(same numbers); line number is display only, the source ID is the real identity.")
    if stats.get("ids_with_renamed_description"):
        ex = next(iter(stats["renamed_examples"].values()))
        out.append(f"{stats['ids_with_renamed_description']} series keep the same code but change name over time "
                   f"(e.g. {' -> '.join(ex)}).")
    codes = defaultdict(int)
    for s in series:
        for c, n in s["missing_codes"].items():
            codes[c] += n
    if codes:
        out.append(f"Missing values in sample, by BEA code: {dict(codes)}.")
    freqs = sorted({s["frequency"] for s in series})
    if len(freqs) > 1:
        out.append(f"Multiple frequencies in one table ({', '.join(freqs)}); frequency must be part of the ID.")
    if any("SIC" in n for s in series for n in s["notes"]):
        out.append("Some tables are on the old SIC industry classification (structural break vs NAICS).")
    unlabeled = sorted({f for s in series for f, v in s["classifications"].items() if v["code"] and not v["label"]})
    if unlabeled:
        out.append(f"Codes without a description in the catalog: {', '.join(unlabeled)}.")
    multi = sorted({f for s in series for f, v in s["classifications"].items() if v.get("alt_labels")})
    if multi:
        out.append(f"Catalog reuses codes under several names ({', '.join(multi)}); kept all names as alt_labels.")
    empty = sum(1 for s in series if s["n_obs"] and s["n_missing"] == s["n_obs"])
    if empty:
        out.append(f"{empty} of {len(series)} sampled series have no values at all in the sample years.")
    return out


# ---------- one group end to end ----------

def extract_group(client: BEAClient, dataset: str, cfg: dict) -> dict:
    folder = OUT / dataset
    prefix = f"bea_{dataset.lower()}"
    summary = {"dataset": dataset, "errors": []}

    try:
        params = client.get_parameter_list(dataset)
    except BEAError as exc:
        params = []
        summary["errors"].append(f"parameters: {exc}")
    _write(folder / f"{prefix}_parameters.json", params)
    summary["api_parameters"] = [p.get("ParameterName", "").strip() for p in params]
    summary["parameter_descriptions"] = {p.get("ParameterName", "").strip(): clean_text(p.get("ParameterDescription", ""))
                                         for p in params}
    summary["required_parameters"] = [p.get("ParameterName", "").strip() for p in params
                                      if str(p.get("ParameterIsRequiredFlag")) == "1"]

    catalogs, cat_stats = {}, {}
    for param in cfg.get("classifications", []):
        try:
            values = as_list(client.get_parameter_values(dataset, param))
        except BEAError as exc:
            summary["errors"].append(f"{param}: {exc}")
            continue
        catalogs[param] = values
        cat_stats[param] = catalog_stats(values)
        _write(folder / f"{prefix}_{param}.json", values)
    summary["classifications"] = cat_stats

    request = dict(cfg["sample"])
    try:
        results = client.get_data(dataset, **request)
    except BEAError as exc:
        summary["errors"].append(f"sample: {exc}")
        return summary

    rows = as_list(results.get("Data"))
    series, tagged, checks = build_series(dataset, results, request, cfg, catalogs)
    dims = as_list(results.get("Dimensions"))
    sample = {"dataset": dataset, **{_snake(k): v for k, v in request.items()},
              "dimensions": dims, "notes": as_list(results.get("Notes")), "observations": tagged}
    _write(folder / f"{prefix}_sample.json", sample)
    _write(folder / f"{prefix}_series_metadata.json", series)

    summary.update({
        "sample_file": f"{dataset}/{prefix}_sample.json",
        "sample_top_level_keys": list(sample.keys()),
        "series_metadata_file": f"{dataset}/{prefix}_series_metadata.json",
        "proposed_hierarchy": ["provider", "dataset", *[_snake(f) for f in cfg["series_id"]]]
                              + ([] if "Frequency" in cfg["series_id"] else ["frequency"]) + ["observation"],
        "sample_request": request,
        "sample_observation_keys": list(rows[0].keys()) if rows else [],
        "dimension_columns": [d.get("Name") for d in dims if isinstance(d, dict)],
        "proposed_canonical_series_id": "BEA:" + dataset + ":" + ":".join(f"{{{f}}}" for f in cfg["series_id"])
                                        + ("" if "Frequency" in cfg["series_id"] else ":{frequency}"),
        "source_series_id_field": next((f for f in SOURCE_ID_FIELDS if rows and f in rows[0]), None),
        "unit_fields": sorted({s["unit_source"] for s in series}),
        "frequencies": sorted({s["frequency"] for s in series}),
        "notes_count": len(as_list(results.get("Notes"))),
        "canonical_id_status": ("verified_unique_in_sample" if checks["canonical_id_status"] == "unique_in_sample"
                                else "not_unique_in_sample"),
        "checks": checks,
        "example_series": series[0] if series else None,
        "findings": findings_for(dataset, series, checks, results, rows),
    })
    return summary


LIMITATIONS = [
    "Samples cover one table/group per dataset and only a couple of years, not full histories.",
    "Series IDs are checked for uniqueness within the sample only, not across all of BEA.",
    "The API returns the latest revised values only (no real-time vintages).",
    "Units follow BEA's own fields (CL_UNIT/UNIT_MULT, or TableScale for MNE); table notes can disagree.",
]


def run(config_path: str | Path = "config/metadata_groups.yaml", only: list[str] | None = None,
        client: BEAClient | None = None, refresh: bool = False) -> dict:
    groups = yaml.safe_load(Path(config_path).read_text())["groups"]
    if only:
        groups = {k: v for k, v in groups.items() if k in only}
    client = client or BEAClient(cache_dir="data/raw/cache")
    client.refresh = refresh
    out = {"provider": "Bureau of Economic Analysis", "provider_code": "BEA",
           "audit_type": "representative_metadata_granularity_pilot",
           "generated_at_utc": datetime.now(timezone.utc).isoformat(),
           "scope": {"dataset_families_sampled": len(groups), "full_provider_coverage": False},
           "datasets": {}}
    for name, cfg in groups.items():
        print(f"== {name}")
        s = extract_group(client, name, cfg)
        out["datasets"][name] = s
        c = s.get("checks", {})
        print(f"   classifications: " + ", ".join(f"{k}={v['values']}" for k, v in s.get("classifications", {}).items()))
        print(f"   sample: {c.get('observations', 0)} obs -> {c.get('series', 0)} series, "
              f"{c.get('observations_linked_to_series', 0)} linked, id {c.get('canonical_id_status', '-')}")
        for e in s["errors"]:
            print(f"   ERROR {e}")
    out["findings"] = [f"{name}: {f}" for name, s in out["datasets"].items() for f in s.get("findings", [])]
    out["limitations"] = LIMITATIONS
    _write(OUT / "bea_metadata_summary.json", out)
    _write(OUT / "bea_metadata_granularity.json", granularity(out))
    print_comparison(out)
    return out


def granularity(summary: dict) -> dict:
    """Same layout as the reference bea_metadata_granularity.json."""
    ds = summary["datasets"]
    return {
        "provider": "Bureau of Economic Analysis",
        "dataset_families_inspected": list(ds),
        "dataset_structures": {n: {"api_parameters": s.get("api_parameters", []),
                                   "parameter_descriptions": s.get("parameter_descriptions", {}),
                                   "classifications": {k: v["values"] for k, v in s.get("classifications", {}).items()},
                                   "source_series_id_field": s.get("source_series_id_field"),
                                   "unit_fields": s.get("unit_fields", []),
                                   "frequencies": s.get("frequencies", []),
                                   "proposed_canonical_series_id": s.get("proposed_canonical_series_id"),
                                   "canonical_id_status": s.get("canonical_id_status")}
                               for n, s in ds.items()},
        "verified_data_samples": {n: {"request": s.get("sample_request"),
                                      "observations": s.get("checks", {}).get("observations"),
                                      "series": s.get("checks", {}).get("series"),
                                      "observations_linked_to_series": s.get("checks", {}).get("observations_linked_to_series")}
                                  for n, s in ds.items() if s.get("checks")},
        "granularity_recommendation": {
            "provider_level": ["provider_name", "provider_url"],
            "dataset_level": ["dataset_name", "dataset_description"],
            "table_level": ["table_id", "table_description"],
            "series_level": ["source_series_id", "line_code", "frequency", "geography", "industry", "indicator",
                             "classification_codes_and_labels", "units", "unit_multiplier", "unit_source"],
            "observation_level": ["time_period", "value", "annotation"],
        },
    }


def print_comparison(summary: dict) -> None:
    print("\n| Dataset | Params | Classifications (values) | Source ID field | Units from | Freq | Series in sample | ID unique |")
    print("|---|---|---|---|---|---|---|---|")
    for name, s in summary["datasets"].items():
        cls = ", ".join(f"{k} ({v['values']})" for k, v in s.get("classifications", {}).items())
        c = s.get("checks", {})
        print(f"| {name} | {len(s.get('api_parameters', []))} | {cls} | {s.get('source_series_id_field') or 'none'} | "
              f"{', '.join(s.get('unit_fields', []))} | {', '.join(s.get('frequencies', []))} | {c.get('series', 0)} | "
              f"{'yes' if c.get('canonical_id_status') == 'unique_in_sample' else 'no'} |")
    print(f"\nWrote {OUT}/<Dataset>/ and {OUT}/bea_metadata_summary.json")
