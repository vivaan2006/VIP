"""Thin, polite client for the BEA Data API (https://apps.bea.gov/api/data).

- One endpoint, five methods (GetDataSetList, GetParameterList,
  GetParameterValues, GetParameterValuesFiltered, GetData).
- Stays under BEA's limits (100 requests/min, 100 MB/min, 30 errors/min):
  spaces requests, pauses when the last minute's downloads near 100 MB,
  pauses after each API error, and backs off on HTTP 429 using Retry-After.
- Caches raw JSON responses on disk so re-runs don't re-download.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://apps.bea.gov/api/data"
MB = 1024 * 1024


class BEAError(RuntimeError):
    """Raised when the API returns an error object instead of data."""


class BEATransientError(BEAError):
    """BEA didn't really answer: the connection dropped, it kept throttling us (429), it timed out or
    was blocked (403, 408), it had a server error (5xx), it sent a page that isn't JSON, or its error
    message says it is busy or over a limit. Unlike a normal BEAError this says nothing about whether
    the data exists, so callers should try again later rather than treat it as "no data".
    """


TRANSIENT_HTTP = {403, 408, 425, 429}  # plus every 5xx
# BEA usually answers a bad request with a JSON error at HTTP 200. These phrases mean "not now"
# rather than "no such data", so the request is worth trying again later.
# Kept narrow on purpose: a word like "unavailable" can also mean "no data for this selection".
TRANSIENT_WORDS = ("too many requests", "rate limit", "requests per minute", "try again", "temporarily",
                   "timed out", "timeout", "throttl", "server busy", "locked out")
THROTTLE_CODE = "[7] "  # BEA's "Request Denied - exceeded Requests per minute quota."


def is_transient(exc: Exception) -> bool:
    """True if a failed call says nothing about whether the data exists (worth retrying later)."""
    if isinstance(exc, (BEATransientError, requests.RequestException, ConnectionError, TimeoutError)):
        return True
    text = str(exc)
    return isinstance(exc, BEAError) and (text.startswith(THROTTLE_CODE)
                                          or any(w in text.lower() for w in TRANSIENT_WORDS))


class BEAClient:
    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = "data/raw/cache",
        min_interval: float = 0.75,  # ~80 requests/min, under the 100/min limit
        max_retries: int = 3,
        timeout: float = 180.0,  # big tables (InputOutput, ~40 MB) take a while to prepare
        max_mb_per_minute: float = 80.0,  # BEA's limit is 100
        error_pause: float = 2.5,  # seconds after an API error, keeps us under 30 errors/min
        network_pause: float = 10.0,  # seconds before retrying a dropped connection (x attempt number)
    ) -> None:
        self.api_key = api_key or os.environ.get("BEA_API_KEY")
        if not self.api_key:
            raise ValueError(
                "No BEA API key. Set BEA_API_KEY in your environment or .env "
                "(register at https://apps.bea.gov/API/signup/)."
            )
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_bytes_per_minute = max_mb_per_minute * MB
        self.error_pause = error_pause
        self.network_pause = network_pause
        self._last_request = 0.0
        self._recent: deque[tuple[float, int]] = deque()  # (time, bytes) of downloads in the last minute
        self._session = requests.Session()
        # Stats about the most recent call, used by the download report.
        self.last_bytes = 0
        self.last_seconds = 0.0
        self.last_from_cache = False
        self.refresh = False  # True = ignore cached responses (still saves new ones)

    # ---------- public methods ----------

    def get_dataset_list(self) -> list[dict]:
        return self._results("GetDataSetList")["Dataset"]

    def get_parameter_list(self, dataset: str) -> list[dict]:
        return self._results("GetParameterList", DatasetName=dataset)["Parameter"]

    def get_parameter_values(self, dataset: str, parameter: str) -> list[dict]:
        res = self._results("GetParameterValues", DatasetName=dataset, ParameterName=parameter)
        return res.get("ParamValue", [])

    def get_parameter_values_filtered(self, dataset: str, target: str, **filters: Any) -> list[dict]:
        res = self._results(
            "GetParameterValuesFiltered", DatasetName=dataset, TargetParameter=target, **filters
        )
        return res.get("ParamValue", [])

    def get_data(self, dataset: str, use_cache: bool = True, **params: Any) -> dict:
        """Return the raw `Results` object for a GetData call."""
        return self._results("GetData", use_cache=use_cache, DatasetName=dataset, **params)

    # ---------- internals ----------

    def _results(self, method: str, use_cache: bool = True, **params: Any) -> dict:
        query = {"method": method, "ResultFormat": "JSON", **{k: _fmt(v) for k, v in params.items()}}
        cache_file = self._cache_path(query)
        payload = self._read_cache(cache_file) if use_cache and not self.refresh else None
        if payload is None:
            payload = self._request(query)
            self.last_from_cache = False
            # BEA echoes the request, including your UserID, back in the
            # response. Drop it so the key never lands in cache files.
            if isinstance(payload.get("BEAAPI"), dict):
                payload["BEAAPI"].pop("Request", None)
            try:
                results = extract_results(payload)  # raises on API errors, so errors are never cached
            except BEAError:
                time.sleep(self.error_pause)
                raise
            if cache_file:
                tmp = cache_file.with_name(cache_file.name + ".tmp")  # never leave half a file
                tmp.write_text(json.dumps(payload))
                tmp.replace(cache_file)
            return results
        return extract_results(payload)

    def _read_cache(self, cache_file: Path | None) -> dict | None:
        """A saved response, or None. A damaged file (e.g. from a crash mid-write) is deleted and fetched again."""
        if not cache_file or not cache_file.exists():
            return None
        try:
            text = cache_file.read_text()
            payload = json.loads(text)
        except (OSError, ValueError):
            cache_file.unlink(missing_ok=True)
            return None
        self.last_bytes, self.last_seconds, self.last_from_cache = len(text.encode()), 0.0, True
        return payload

    def _request(self, query: dict) -> dict:
        params = {"UserID": self.api_key, **query}
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._respect_data_limit()
            self._last_request = time.monotonic()
            started = time.monotonic()
            try:
                resp = self._session.get(BASE_URL, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # Wi-Fi drop, timeout, DNS: wait and try again
                if attempt < self.max_retries:
                    time.sleep(self.network_pause * (attempt + 1))
                    continue
                raise BEATransientError(f"Network problem, gave up after {attempt + 1} tries: "
                                        f"{self._hide_key(exc)}") from None
            self.last_seconds = time.monotonic() - started
            self.last_bytes = len(resp.content)
            self._recent.append((time.monotonic(), self.last_bytes))
            if resp.status_code == 429 and attempt < self.max_retries:
                time.sleep(float(resp.headers.get("Retry-After", 60)) + 1)
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:  # BEA having a bad moment
                time.sleep(self.network_pause * (attempt + 1))
                continue
            if resp.status_code in TRANSIENT_HTTP or resp.status_code >= 500:
                raise BEATransientError(f"HTTP {resp.status_code} from BEA, gave up after {attempt + 1} tries")
            if resp.status_code >= 400:
                raise BEAError(f"HTTP {resp.status_code} from BEA")
            # BEA sends UTF-8; a few old footnotes aren't valid UTF-8, so fall back to Latin-1.
            try:
                text = resp.content.decode("utf-8")
            except UnicodeDecodeError:
                text = resp.content.decode("iso-8859-1")
            try:
                return json.loads(text)
            except ValueError:
                raise BEATransientError(f"BEA sent something that isn't JSON: {text[:150]!r}") from None
        raise AssertionError("unreachable: the last attempt always returns or raises")

    def _hide_key(self, exc: Exception) -> str:
        """Error text with the API key taken out (requests puts the whole URL in its messages)."""
        return str(exc).replace(self.api_key, "<key>")[:300]

    def _respect_data_limit(self) -> None:
        """Wait until the bytes downloaded in the last 60 s are under the per-minute budget."""
        while True:
            now = time.monotonic()
            while self._recent and now - self._recent[0][0] > 60:
                self._recent.popleft()
            if sum(b for _, b in self._recent) < self.max_bytes_per_minute:
                return
            time.sleep(61 - (now - self._recent[0][0]))

    def _cache_path(self, query: dict) -> Path | None:
        if not self.cache_dir:
            return None
        key = json.dumps(query, sort_keys=True)  # API key deliberately excluded
        return self.cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"


def extract_results(payload: dict) -> dict:
    """Unwrap BEAAPI -> Results, smoothing over the shapes different datasets use.

    Quirks handled (all seen in the official `beaapi` package):
    - GDPbyIndustry, IIP, InputOutput, UnderlyingGDPbyIndustry wrap Results in a 1-item list.
    - IIP puts `Data` next to `Results` instead of inside it.
    - A single data row comes back as a dict instead of a list.
    - Errors can sit in BEAAPI or in Results, and can be a list of several errors.
    """
    try:
        api = payload["BEAAPI"]
    except (KeyError, TypeError) as exc:
        raise BEAError(f"Unexpected response shape: {str(payload)[:300]}") from exc
    if api.get("Error"):
        raise BEAError(_error_text(api["Error"]))
    results = api.get("Results", {})
    if isinstance(results, list):  # usually 1 item; if BEA ever sends more, keep every item's rows
        parts = [r for r in results if isinstance(r, dict)]
        results = dict(parts[0]) if parts else {}
        if len(parts) > 1:
            rows = []
            for part in parts:
                data = part.get("Data") or []
                rows += [data] if isinstance(data, dict) else list(data)
            results["Data"] = rows
    if not isinstance(results, dict):
        raise BEAError(f"Unexpected Results shape: {str(results)[:300]}")
    if results.get("Error"):
        raise BEAError(_error_text(results["Error"]))
    if "Data" in api and "Data" not in results:  # IIP
        results = {**results, "Data": api["Data"]}
    if isinstance(results.get("Data"), dict):  # single observation
        results = {**results, "Data": [results["Data"]]}
    return results


def _error_text(error) -> str:
    if isinstance(error, list):
        return "; ".join(_error_text(e) for e in error)
    if not isinstance(error, dict):
        return str(error)
    desc = error.get("APIErrorDescription") or error.get("error") or ""
    detail = error.get("ErrorDetail", {})
    detail = detail.get("Description", "") if isinstance(detail, dict) else detail
    code = error.get("APIErrorCode")
    return f"{f'[{code}] ' if code else ''}{desc} {detail}".strip()


def _fmt(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)
