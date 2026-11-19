"""Client rate-limit behaviour, offline."""
import json

from bea_corpus import client as C


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def test_waits_when_last_minute_is_near_the_data_limit(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(C.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(C.time, "sleep", clock.sleep)
    c = C.BEAClient(api_key="k", cache_dir=None, max_mb_per_minute=50)
    c._recent.extend([(0.0, 30 * C.MB), (10.0, 30 * C.MB)])  # 60 MB in the last minute
    clock.now = 20.0
    c._respect_data_limit()
    assert clock.now > 60  # waited for the first download to age out
    clock.sleeps.clear()
    c._respect_data_limit()
    assert clock.sleeps == []  # back under budget, no wait


def test_pauses_after_api_error_and_does_not_cache(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(C.time, "sleep", slept.append)
    c = C.BEAClient(api_key="k", cache_dir=tmp_path, error_pause=2.5)
    monkeypatch.setattr(c, "_request", lambda q: {"BEAAPI": {"Results": {"Error": {"APIErrorDescription": "bad"}}}})
    try:
        c.get_data("NIPA", TableName="X")
    except C.BEAError:
        pass
    assert slept == [2.5] and not list(tmp_path.iterdir())


def test_network_errors_retry_then_become_bea_errors(monkeypatch, tmp_path):
    import requests

    from bea_corpus.client import BEAClient, BEAError
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("https://apps.bea.gov/api/data?UserID=secret-key-123&method=X failed")

    c = BEAClient(api_key="secret-key-123", cache_dir=tmp_path, min_interval=0, network_pause=0, max_retries=2)
    monkeypatch.setattr(c._session, "get", boom)
    try:
        c.get_parameter_list("NIPA")
        raise AssertionError("should have raised")
    except BEAError as exc:
        assert "secret-key-123" not in str(exc) and "<key>" in str(exc)
    assert len(calls) == 3  # first try + 2 retries


def test_no_answer_and_no_data_are_different_errors(monkeypatch, tmp_path):
    import requests

    from bea_corpus.client import BEAClient, BEAError, BEATransientError

    class Resp:
        def __init__(self, status, body=b"{}"):
            self.status_code, self.content, self.headers = status, body, {}

    def client_answering(resp):
        c = BEAClient(api_key="k", cache_dir=tmp_path, min_interval=0, network_pause=0, max_retries=1,
                      error_pause=0)
        monkeypatch.setattr(c._session, "get", resp)
        return c

    def raised(c):
        try:
            c.get_data("MNE", SeriesID="16")
        except BEAError as exc:
            return exc
        raise AssertionError("should have raised")

    def dropped(*a, **k):
        raise requests.ConnectionError("reset")

    assert isinstance(raised(client_answering(dropped)), BEATransientError)
    assert isinstance(raised(client_answering(lambda *a, **k: Resp(503))), BEATransientError)
    assert isinstance(raised(client_answering(lambda *a, **k: Resp(200, b"<html>busy</html>"))), BEATransientError)
    not_found = raised(client_answering(lambda *a, **k: Resp(400)))
    assert not isinstance(not_found, BEATransientError)
    refused = raised(client_answering(lambda *a, **k: Resp(200, b'{"BEAAPI": {"Results": {"Error": '
                                                             b'{"APIErrorDescription": "Invalid combination"}}}}')))
    assert not isinstance(refused, BEATransientError) and "Invalid combination" in str(refused)


def test_blocked_timeout_and_throttle_messages_are_no_answer(monkeypatch, tmp_path):
    from bea_corpus.client import BEAClient, BEAError, is_transient

    class Resp:
        def __init__(self, status, body=b"{}", headers=None):
            self.status_code, self.content, self.headers = status, body, headers or {}

    calls = []

    def answering(*responses):
        it = iter(responses)

        def get(*a, **k):
            calls.append(1)
            return next(it)
        c = BEAClient(api_key="k", cache_dir=tmp_path, min_interval=0, network_pause=0, max_retries=1, error_pause=0)
        monkeypatch.setattr(c._session, "get", get)
        return c

    def raised(c):
        try:
            c.get_data("MNE", SeriesID="4")
        except BEAError as exc:
            return exc
        raise AssertionError("should have raised")

    for status in (403, 408):
        assert is_transient(raised(answering(Resp(status), Resp(status))))
    busy = b'{"BEAAPI": {"Results": {"Error": {"APIErrorDescription": "Too many requests, please try again later"}}}}'
    assert is_transient(raised(answering(Resp(200, busy))))
    no_data = b'{"BEAAPI": {"Results": {"Error": {"APIErrorDescription": "No data available for this selection"}}}}'
    assert not is_transient(raised(answering(Resp(200, no_data))))

    calls.clear()  # 429 waits Retry-After, then succeeds
    c = answering(Resp(429, headers={"Retry-After": "0"}), Resp(200, b'{"BEAAPI": {"Results": {"Data": [{"a": 1}]}}}'))
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    assert c.get_data("MNE", SeriesID="4")["Data"] == [{"a": 1}] and len(calls) == 2


def test_damaged_cache_file_is_fetched_again(monkeypatch, tmp_path):
    from bea_corpus.client import BEAClient

    class Resp:
        status_code, headers = 200, {}
        content = b'{"BEAAPI": {"Results": {"Data": [{"a": 1}]}}}'

    c = BEAClient(api_key="k", cache_dir=tmp_path, min_interval=0, error_pause=0)
    monkeypatch.setattr(c._session, "get", lambda *a, **k: Resp())
    c.get_data("MNE", SeriesID="4")
    cached = next(tmp_path.glob("*.json"))
    cached.write_text('{"BEAAPI": {"Res')                      # cut off mid-write
    assert c.get_data("MNE", SeriesID="4")["Data"] == [{"a": 1}]
    assert json.loads(cached.read_text())                      # and the file is whole again


def test_bea_throttle_code_is_no_answer():
    from bea_corpus.client import BEAError, is_transient
    assert is_transient(BEAError("[7] Request Denied - exceeded Requests per minute quota."))
    assert not is_transient(BEAError("[1] no data found"))
    assert not is_transient(BEAError("[40] The dataset requested requires parameters that were missing"))
