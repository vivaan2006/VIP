"""Command line entry point.

    python -m bea_corpus datasets                    # list BEA datasets
    python -m bea_corpus params NIPA                 # parameters of a dataset
    python -m bea_corpus params NIPA TableName       # valid values of a parameter
    python -m bea_corpus fetch                       # download + standardize config/series.yaml
    python -m bea_corpus profile                     # statistical profile of the corpus

  Main corpus download (all 12 datasets, standard JSON):
    python -m bea_corpus                             # = download: dictionaries + all series in sampled tables
    python -m bea_corpus download --only NIPA ITA    # just some datasets
    python -m bea_corpus standardize                 # rebuild JSON from raw files, no API calls
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

from .client import BEAClient, BEAError
from .profile import profile_corpus
from .standardize import SeriesSpec, standardize

PROCESSED = Path("data/processed")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def load_specs(path: str | Path) -> list[SeriesSpec]:
    cfg = yaml.safe_load(Path(path).read_text())
    return [SeriesSpec.from_dict(s) for s in cfg["series"]]


def cmd_datasets(args) -> None:
    for d in BEAClient().get_dataset_list():
        print(f"{d['DatasetName']:<26} {d['DatasetDescription']}")


def cmd_params(args) -> None:
    client = BEAClient()
    if args.parameter:
        for v in client.get_parameter_values(args.dataset, args.parameter):
            key = v.get("Key") or v.get("TableName") or next(iter(v.values()))
            desc = v.get("Desc") or v.get("Description") or ""
            print(f"{key:<16} {desc}")
    else:
        for p in client.get_parameter_list(args.dataset):
            req = "required" if str(p.get("ParameterIsRequiredFlag")) == "1" else "optional"
            print(f"{p['ParameterName']:<22} {req:<9} {p.get('ParameterDescription', '')}")


def cmd_fetch(args) -> None:
    client = BEAClient(cache_dir=None if args.no_cache else "data/raw/cache")
    specs = load_specs(args.config)
    if args.only:
        specs = [s for s in specs if s.id in args.only]
    all_obs, all_meta, failed = [], [], []
    for spec in specs:
        try:
            results = client.get_data(spec.dataset, use_cache=not args.refresh, **spec.params)
            obs, meta = standardize(results, spec)
        except (BEAError, ValueError) as exc:
            print(f"  FAIL {spec.id}: {exc}", file=sys.stderr)
            failed.append(spec.id)
            continue
        all_obs.append(obs)
        all_meta.append(meta)
        print(f"  ok   {spec.id:<28} {len(meta):>3} series, {len(obs):>6} obs")

    if not all_obs:
        sys.exit("Nothing fetched.")
    PROCESSED.mkdir(parents=True, exist_ok=True)
    obs = pd.concat(all_obs, ignore_index=True)
    meta = pd.concat(all_meta, ignore_index=True)
    obs.to_csv(PROCESSED / "observations.csv", index=False)
    meta.to_csv(PROCESSED / "series_metadata.csv", index=False)
    print(f"\nWrote {len(meta)} series / {len(obs)} observations to {PROCESSED}/")
    if failed:
        sys.exit(f"{len(failed)} spec(s) failed: {', '.join(failed)}")


def cmd_profile(args) -> None:
    obs = pd.read_csv(PROCESSED / "observations.csv", parse_dates=["date"])
    meta = pd.read_csv(PROCESSED / "series_metadata.csv", dtype={"geo_code": str})
    prof = profile_corpus(obs, meta)
    out = Path("reports")
    out.mkdir(exist_ok=True)
    prof.to_csv(out / "profile.csv", index=False)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(prof.drop(columns=["name"]).head(30).to_string(index=False))
    print(f"\nWrote reports/profile.csv ({len(prof)} series)")


def cmd_download(args) -> None:
    from .download import run
    run(args.config, only=args.only, sample_size=args.sample_size, use_cache=not args.refresh,
        client=BEAClient(cache_dir="data/raw/cache"))


def cmd_standardize(args) -> None:
    from .download import restandardize
    restandardize()


def cmd_metadata(args) -> None:
    from .metadata import run
    run(args.config, only=args.only, refresh=args.refresh)


def main(argv=None) -> None:
    _load_env()
    p = argparse.ArgumentParser(prog="bea_corpus", description="BEA slice of MacroCorpus")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("datasets", help="list BEA API datasets").set_defaults(func=cmd_datasets)

    pp = sub.add_parser("params", help="explore a dataset's parameters")
    pp.add_argument("dataset")
    pp.add_argument("parameter", nargs="?")
    pp.set_defaults(func=cmd_params)

    pf = sub.add_parser("fetch", help="download + standardize the configured series")
    pf.add_argument("--config", default="config/series.yaml")
    pf.add_argument("--only", nargs="+", help="spec ids to fetch")
    pf.add_argument("--refresh", action="store_true", help="ignore cached responses")
    pf.add_argument("--no-cache", action="store_true", help="don't read or write the cache")
    pf.set_defaults(func=cmd_fetch)

    sub.add_parser("profile", help="profile the fetched corpus").set_defaults(func=cmd_profile)

    pd_ = sub.add_parser("download", help="(default) every dataset's dictionary + all series in sampled tables")
    pd_.add_argument("--config", default="config/datasets.yaml")
    pd_.add_argument("--only", nargs="+", help="dataset names, e.g. NIPA Regional")
    pd_.add_argument("--sample-size", type=int, help="tables per dataset (overrides the config)")
    pd_.add_argument("--refresh", action="store_true", help="ignore cached responses")
    pd_.set_defaults(func=cmd_download)

    sub.add_parser("standardize", help="rebuild data/corpus series JSON from data/raw (offline)"
                   ).set_defaults(func=cmd_standardize)

    pm = sub.add_parser("metadata", help="metadata + sample series for the groups in config/metadata_groups.yaml")
    pm.add_argument("--config", default="config/metadata_groups.yaml")
    pm.add_argument("--only", nargs="+", help="dataset names, e.g. IIP MNE")
    pm.add_argument("--refresh", action="store_true", help="ignore cached responses")
    pm.set_defaults(func=cmd_metadata)

    argv = sys.argv[1:] if argv is None else argv
    args = p.parse_args(argv or ["download"])  # no command = the full download
    args.func(args)


if __name__ == "__main__":
    main()
