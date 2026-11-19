"""One command to set everything up and download all 12 BEA datasets.

    python run.py                    # install, then download everything
    python run.py --sample           # quick test run, a few tables per dataset
    python run.py --sample 10        # 10 tables per dataset
    python run.py --only NIPA MNE    # just some datasets
    python run.py --refresh          # ignore saved responses and ask BEA again

What it does:
  1. installs the Python packages the code needs (into whatever python you ran this with)
  2. makes sure there's a BEA API key (asks for it once and saves it to macrocorpus-bea/.env)
  3. runs the download and saves everything it prints to macrocorpus-bea/reports/run_log.txt

Results: macrocorpus-bea/data/corpus/<Dataset>/
Check:   macrocorpus-bea/reports/coverage.md and macrocorpus-bea/reports/issues.json
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent / "macrocorpus-bea"
ENV_FILE = PROJECT / ".env"
LOG_FILE = PROJECT / "reports" / "run_log.txt"


def install() -> None:
    print("[1/3] Installing packages...")
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10 or newer is needed, this is {sys.version.split()[0]}.")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "-e", str(PROJECT), "pytest"]
    if subprocess.run(cmd).returncode != 0:
        sys.exit("\npip install failed. Try it in a fresh virtual environment:\n"
                 "  python3 -m venv .venv && source .venv/bin/activate && python run.py")


def ensure_key() -> None:
    print("[2/3] Checking for a BEA API key...")
    if os.environ.get("BEA_API_KEY"):
        return
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("BEA_API_KEY=") and len(line.split("=", 1)[1].strip()) == 36:
                return
    print("  No key found. Get a free one at https://apps.bea.gov/API/signup/ (it's emailed to you).")
    key = input("  Paste your 36-character BEA API key: ").strip()
    if len(key) != 36:
        sys.exit("  That doesn't look like a BEA key (should be 36 characters). Run again when you have it.")
    ENV_FILE.write_text(f"BEA_API_KEY={key}\n")
    print(f"  Saved to {ENV_FILE} (git ignores this file, so the key never gets pushed).")


def preflight() -> None:
    """One quick call so a bad key or no internet shows up now, not after minutes of retries."""
    print("  Checking the key works...")
    sys.path.insert(0, str(PROJECT / "src"))
    from dotenv import load_dotenv

    from bea_corpus.client import BEAClient, BEAError, is_transient
    load_dotenv(ENV_FILE)
    client = BEAClient(cache_dir=None, network_pause=2, max_retries=1)
    try:
        n = len(client.get_dataset_list())
    except BEAError as exc:
        text = str(exc)
        if is_transient(exc):
            sys.exit(f"  Can't reach BEA right now ({text[:200]}).\n"
                     "  Check your internet connection, or BEA may be busy. Wait a few minutes and run again.")
        sys.exit(f"  BEA rejected the request: {text[:200]}\n"
                 f"  If it's about the UserID, the key is wrong. Fix it in {ENV_FILE} and run again.")
    print(f"  OK, BEA lists {n} datasets.")


def download(extra_args: list[str]) -> int:
    print("[3/3] Downloading. The first full run takes a while (Regional alone is thousands of requests).")
    print("      If it stops, just run it again: finished requests are saved and get skipped.\n")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", "-m", "bea_corpus", "download", *extra_args]
    with LOG_FILE.open("w") as log:
        log.write(f"started {datetime.now():%Y-%m-%d %H:%M:%S}  command: {' '.join(cmd[2:])}\n\n")
        proc = subprocess.Popen(cmd, cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            print(line, end="")
            log.write(line)
            log.flush()
        code = proc.wait()
        log.write(f"\nfinished {datetime.now():%Y-%m-%d %H:%M:%S}  exit code {code}\n")
    return code


def main() -> None:
    p = argparse.ArgumentParser(description="Install and download all BEA datasets in one go.")
    p.add_argument("--sample", nargs="?", const="quick", metavar="N",
                   help="test run instead of everything: a few tables per dataset, or N tables per dataset")
    p.add_argument("--sample-size", dest="sample", help=argparse.SUPPRESS)  # old name, still works
    p.add_argument("--only", nargs="+", help="dataset names, e.g. NIPA Regional")
    p.add_argument("--refresh", action="store_true", help="ignore saved responses")
    p.add_argument("--skip-install", action="store_true", help="don't run pip install")
    args = p.parse_args()

    if not args.skip_install:
        install()
    ensure_key()
    preflight()
    extra = [] if args.sample is None else ["--sample", str(args.sample)]
    if args.only:
        extra += ["--only", *args.only]
    if args.refresh:
        extra.append("--refresh")
    code = download(extra)

    print(f"""
Done. Where to look:
  results       {PROJECT / 'data' / 'corpus'}/<Dataset>/
  summary       {PROJECT / 'reports' / 'coverage.md'}
  problems      {PROJECT / 'reports' / 'issues.json'}
  full log      {LOG_FILE}""")
    sys.exit(code)


if __name__ == "__main__":
    main()
