"""Tests for fetch.py. No network: fetch.download is replaced with canned zips."""

import io
import sys
import zipfile
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch  # noqa: E402
import ingest  # noqa: E402
from test_ingest import COUNTRY_CSV, DATA_CSV, INDICATOR_CSV  # noqa: E402

CODE = "TEST.IND"
DATA = f"API_{CODE}_DS2_en_csv_v2_2.csv"


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def good_files(data_name: str = DATA) -> dict[str, str]:
    return {
        data_name: DATA_CSV,
        f"Metadata_Country_{data_name}": COUNTRY_CSV,
        f"Metadata_Indicator_{data_name}": INDICATOR_CSV,
    }


@pytest.fixture
def served(monkeypatch):
    """Make fetch.download return whatever blob the test puts in served['blob']."""
    box = {"blob": make_zip(good_files()), "urls": []}

    def fake_download(url, **_):
        box["urls"].append(url)
        return box["blob"]

    monkeypatch.setattr(fetch, "download", fake_download)
    monkeypatch.setattr(fetch, "INDICATORS", {"test": CODE})
    return box


def test_fetch_writes_files_ingest_can_read(served, tmp_path):
    path = fetch.fetch("test", CODE, tmp_path)
    assert served["urls"] == [f"https://api.worldbank.org/v2/en/indicator/{CODE}?downloadformat=csv"]
    assert path == tmp_path / "test" / DATA
    [src] = ingest.discover(tmp_path)
    assert src.data == path
    assert len(ingest.parse(src).observations) == 5


def test_fetch_replaces_old_download(served, tmp_path):
    old = "API_TEST.IND_DS2_en_csv_v2_1.csv"
    served["blob"] = make_zip(good_files(old))
    fetch.fetch("test", CODE, tmp_path)
    served["blob"] = make_zip(good_files(DATA))
    fetch.fetch("test", CODE, tmp_path)
    assert sorted(p.name for p in (tmp_path / "test").iterdir()) == sorted(good_files(DATA))
    assert [p.name for p in tmp_path.iterdir()] == ["test"]  # no temp folders left


@pytest.mark.parametrize(
    "files, match",
    [
        ({**good_files(), "../evil.csv": "x"}, "unexpected file"),
        ({**good_files(), "readme.txt": "x"}, "unexpected file"),
        ({DATA: DATA_CSV}, "expected"),
        (good_files("API_OTHER.CODE_DS2_en_csv_v2_1.csv"), "unexpected file"),
    ],
)
def test_bad_zip_keeps_existing_files(served, tmp_path, files, match):
    fetch.fetch("test", CODE, tmp_path)
    before = {p.name: p.read_bytes() for p in (tmp_path / "test").iterdir()}
    served["blob"] = make_zip(files)
    with pytest.raises(ValueError, match=match):
        fetch.fetch("test", CODE, tmp_path)
    assert {p.name: p.read_bytes() for p in (tmp_path / "test").iterdir()} == before


def test_not_a_zip(served, tmp_path):
    served["blob"] = b'<?xml version="1.0"?><wb:error>Invalid value</wb:error>'
    with pytest.raises(ValueError, match="not a zip"):
        fetch.fetch("test", CODE, tmp_path)


def test_fetch_all_counts_failures(served, tmp_path):
    served["blob"] = b"garbage"
    assert fetch.fetch_all(tmp_path) == 1
    with pytest.raises(SystemExit):
        fetch.fetch_all(tmp_path, ["nope"])


def test_discover_ignores_dot_folders(served, tmp_path):
    fetch.fetch("test", CODE, tmp_path)
    (tmp_path / ".test.new").mkdir()
    for p in (tmp_path / "test").iterdir():
        (tmp_path / ".test.new" / p.name).write_bytes(p.read_bytes())
    assert len(ingest.discover(tmp_path)) == 1


def response(status: int, body: bytes = b"") -> requests.Response:
    r = requests.Response()
    r.status_code, r.reason, r.url = status, "reason", "http://x"
    r._content, r._content_consumed = body, True
    return r


class FakeSession:
    """Returns (or raises) the queued items in order; records each call."""

    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)


def test_download_retries_server_errors_and_network_failures():
    session = FakeSession(response(503), requests.ConnectionError("reset"), requests.Timeout("slow"),
                          response(200, b"ok"))
    assert fetch.download("http://x", session=session) == b"ok"
    assert len(session.calls) == 4
    assert session.calls[0]["headers"]["User-Agent"] == fetch.USER_AGENT


def test_download_gives_up_after_attempts():
    session = FakeSession(*[response(503) for _ in range(4)])
    with pytest.raises(requests.HTTPError, match="503"):
        fetch.download("http://x", session=session)
    assert len(session.calls) == 4


@pytest.mark.parametrize("item, exc", [
    (response(404), requests.HTTPError),
    (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"), requests.exceptions.SSLError),
])
def test_download_does_not_retry_permanent_errors(item, exc):
    session = FakeSession(item, response(200, b"ok"))
    with pytest.raises(exc):
        fetch.download("http://x", session=session)
    assert len(session.calls) == 1


def test_download_caps_size(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_ZIP_BYTES", 10)
    with pytest.raises(ValueError, match="larger than 10 bytes"):
        fetch.download("http://x", session=FakeSession(response(200, b"x" * 11)))
