"""Tests for wb_api.py. No network: a fake session serves canned API responses."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ingest  # noqa: E402
import wb_api  # noqa: E402
from test_ingest import conn, counts  # noqa: E402,F401  (database fixture)

BASE = wb_api.BASE_URL


def country(code, name, region="East Asia & Pacific", income="Lower middle income"):
    return {"id": code, "name": name, "region": {"value": region}, "incomeLevel": {"value": income}}


COUNTRIES = [
    country("PHL", "Philippines"),
    country("ARM", "Armenia", "Europe & Central Asia ", "Upper middle income"),  # API pads some regions
    country("WLD", "World", "Aggregates", ""),
]


def obs(code, year, value, ind="TEST.IND"):
    return {"indicator": {"id": ind, "value": "Test indicator (annual %)"},
            "country": {"id": code[:2], "value": dict((c["id"], c["name"]) for c in COUNTRIES)[code]},
            "countryiso3code": code, "date": str(year), "value": value}


OBS = [obs("PHL", 2021, 2.5), obs("PHL", 2020, None), obs("PHL", 2019, 1.5),
       obs("ARM", 2021, None), obs("WLD", 2021, 5), obs("WLD", 2020, 4), obs("WLD", 2019, 3)]

INDICATOR = {"id": "TEST.IND", "name": "Test indicator (annual %)", "sourceNote": "A note.",
             "sourceOrganization": "Somebody", "source": {"value": "WDI"}}


def paged(rows, page, per_page, extra=None):
    chunk = rows[(page - 1) * per_page: page * per_page]
    pages = max(1, -(-len(rows) // per_page))
    return [{"page": page, "pages": pages, "per_page": per_page, "total": len(rows), **(extra or {})},
            chunk or None]


class FakeResponse:
    def __init__(self, url, body, status=200):
        self.url, self._body, self.status_code = url, body, status
        self.text = body if isinstance(body, str) else json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise wb_api.requests.HTTPError(f"{self.status_code}")

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Routes URLs to canned data; records every request."""

    def __init__(self, observations=OBS, overrides=None):
        self.observations = observations
        self.overrides = overrides or {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        assert params["format"] == "json"
        path = url.removeprefix(BASE + "/")
        if path in self.overrides:
            return FakeResponse(url, self.overrides[path])
        page, per_page = params["page"], params["per_page"]
        if path == "country":
            return FakeResponse(url, paged(COUNTRIES, page, per_page))
        if path.startswith("country/") and "/indicator/" in path:
            codes = path.split("/")[1].split(";")
            rows = [r for r in self.observations if r["countryiso3code"] in codes]
            return FakeResponse(url, paged(rows, page, per_page, {"lastupdated": "2026-11-11"}))
        if path == "indicator/TEST.IND":
            return FakeResponse(url, paged([INDICATOR], page, per_page))
        raise AssertionError(f"unexpected request {url}")


def run(tmp_path, targets=("PHL", "ARM", "WLD"), session=None):
    session = session or FakeSession()
    parsed, failures = wb_api.fetch(list(targets), tmp_path, ["TEST.IND"], session)
    return parsed, failures, session


def test_fetch_parses_like_csv_path(tmp_path):
    [p], failures, session = run(tmp_path)
    assert failures == 0
    assert list(p.observations.columns) == ["country_code", "country_name", "indicator_code", "year", "value"]
    phl = p.observations[p.observations.country_code == "PHL"].set_index("year")["value"].to_dict()
    assert phl == {2019: 1.5, 2021: 2.5}  # nulls dropped
    assert len(p.observations) == 5

    c = p.countries.set_index("country_code")
    assert c.loc["ARM", "region"] == "Europe & Central Asia"  # padding stripped
    assert c.loc["WLD", "is_aggregate"] and c.loc["WLD", "region"] is None
    assert not c.loc["PHL", "is_aggregate"]

    ind = p.indicators.iloc[0]
    assert (ind.unit, ind.source_org) == ("annual %", "Somebody")
    assert p.source.source_updated == date(2026, 11, 11)

    obs_url = next(u for u, _ in session.calls if "/indicator/" in u and u.startswith(f"{BASE}/country/"))
    assert obs_url == f"{BASE}/country/PHL;ARM;WLD/indicator/TEST.IND"


def test_snapshot_saved_once_per_content(tmp_path):
    [p1], _, _ = run(tmp_path)
    [p2], _, _ = run(tmp_path)
    assert p1.source.data == p2.source.data and p1.source.sha256 == p2.source.sha256
    assert len(list((tmp_path / "TEST.IND").iterdir())) == 1
    saved = json.loads(p1.source.data.read_text())
    assert saved["indicator_code"] == "TEST.IND" and len(saved["observations"]) == len(OBS)

    revised = [dict(r, value=1.7) if (r["countryiso3code"], r["date"]) == ("PHL", "2019") else r for r in OBS]
    [p3], _, _ = run(tmp_path, session=FakeSession(revised))
    assert p3.source.sha256 != p1.source.sha256
    assert len(list((tmp_path / "TEST.IND").iterdir())) == 2


def test_unknown_targets_are_skipped(tmp_path, caplog):
    [p], failures, session = run(tmp_path, targets=("PHL", "XXX"))
    assert failures == 0
    assert "XXX" in caplog.text
    assert any(u == f"{BASE}/country/PHL/indicator/TEST.IND" for u, _ in session.calls)
    with pytest.raises(SystemExit):
        run(tmp_path, targets=("XXX",))


def test_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(wb_api, "PER_PAGE", 2)
    [p], _, session = run(tmp_path)
    assert len(p.observations) == 5
    pages = [prm["page"] for u, prm in session.calls if u.endswith("/indicator/TEST.IND") and "/country/" in u]
    assert pages == [1, 2, 3, 4]


def test_api_error_message_counts_as_failure(tmp_path):
    err = [{"message": [{"id": "120", "key": "Invalid value", "value": "The provided parameter value is not valid"}]}]
    session = FakeSession(overrides={"indicator/TEST.IND": err})
    parsed, failures, _ = run(tmp_path, session=session)
    assert (parsed, failures) == ([], 1)


@pytest.mark.parametrize("body, match", [
    ("<html>oops</html>", "not JSON"),
    ({"unexpected": True}, "unexpected response shape"),
    ([{"page": 1, "pages": 1, "total": 9}, [INDICATOR]], "reported 9 rows"),
])
def test_bad_responses_raise(body, match):
    session = FakeSession(overrides={"indicator/TEST.IND": body})
    with pytest.raises(wb_api.WorldBankError, match=match):
        wb_api.get_all(session, "indicator/TEST.IND")


def test_non_annual_dates_rejected(tmp_path):
    quarterly = OBS + [dict(obs("PHL", 2021, 1.0), date="2021Q1")]
    parsed, failures, _ = run(tmp_path, session=FakeSession(quarterly))
    assert (parsed, failures) == ([], 1)


def test_load_api_snapshot_is_idempotent(conn, tmp_path):  # noqa: F811
    [p], _, _ = run(tmp_path)
    assert ingest.load(conn, p, ["PHL", "WLD"], 2019, 2021, force=False) == "loaded"
    first = counts(conn)
    assert first == {"raw.files": 1, "raw.observations": 5,
                     "core.observation": 5, "core.observation_history": 0}
    [p2], _, _ = run(tmp_path)
    assert ingest.load(conn, p2, ["PHL", "WLD"], 2019, 2021, force=False) == "skipped"
    assert counts(conn) == first
    region = conn.execute("SELECT region FROM core.country WHERE country_code = 'ARM'").fetchone()[0]
    assert region == "Europe & Central Asia"
