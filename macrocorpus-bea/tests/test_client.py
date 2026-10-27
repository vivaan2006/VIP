"""Client rate-limit behaviour, offline."""

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
