"""Download World Bank bulk CSVs into datasources/, ready for ingest.py.

For each indicator this fetches the same zip as the "Download CSV" button on
data.worldbank.org:

    https://api.worldbank.org/v2/en/indicator/<code>?downloadformat=csv

and replaces the contents of datasources/<folder>/ with its three CSV files
(API_*.csv plus the Metadata_Country_ and Metadata_Indicator_ files).
The swap happens only after the whole zip has been downloaded and checked, so a
failed download leaves the previous files untouched.

Usage:
    python fetch.py                    # all indicators
    python fetch.py GDP inflation      # only these folders
    python ingest.py                   # ingest without args fetches by default
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "datasources"
URL = "https://api.worldbank.org/v2/en/indicator/{code}?downloadformat=csv"
USER_AGENT = "geip-fetch/1.0"

# datasources/<folder> -> World Bank indicator code
INDICATORS = {
    "GDP": "NY.GDP.MKTP.CD",
    "inflation": "FP.CPI.TOTL.ZG",
    "population": "SP.POP.TOTL",
    "unemployment": "SL.UEM.TOTL.ZS",
}

MAX_ZIP_BYTES = 50 * 1024 * 1024  # these zips are ~150 KB; anything huge is wrong
MAX_CSV_BYTES = 50 * 1024 * 1024

log = logging.getLogger("fetch")


def download(url: str, attempts: int = 4, timeout: float = 60,
             session: requests.Session | None = None) -> bytes:
    """GET url, retrying connection errors, timeouts and 429/5xx responses with backoff."""
    session = session or requests.Session()
    for attempt in range(1, attempts + 1):
        try:
            with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True) as resp:
                if resp.status_code == 429 or resp.status_code >= 500:
                    err: Exception = requests.HTTPError(f"{resp.status_code} {resp.reason}", response=resp)
                else:
                    resp.raise_for_status()  # other 4xx won't fix itself
                    return _read_capped(resp, url)
        except requests.exceptions.SSLError:
            raise  # a certificate problem won't fix itself either
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            err = exc
        if attempt == attempts:
            raise err
        wait = 2 ** attempt
        log.warning("%s: %s, retrying in %ds (%d/%d)", url, err, wait, attempt, attempts)
        time.sleep(wait)
    raise AssertionError("unreachable")


def _read_capped(resp: requests.Response, url: str) -> bytes:
    """Read the body, stopping as soon as it passes MAX_ZIP_BYTES."""
    chunks, size = [], 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        size += len(chunk)
        if size > MAX_ZIP_BYTES:
            raise ValueError(f"{url}: response larger than {MAX_ZIP_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def extract(blob: bytes, code: str) -> dict[str, bytes]:
    """Return {filename: contents} for the three expected CSVs, or raise ValueError."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{code}: response is not a zip ({blob[:80]!r})") from exc

    data_re = re.compile(rf"API_{re.escape(code)}_DS2_[A-Za-z0-9_.]+\.csv")
    files: dict[str, bytes] = {}
    for info in zf.infolist():
        name = info.filename
        # Only flat, expected file names: no directories, no path tricks.
        base = name.removeprefix("Metadata_Country_").removeprefix("Metadata_Indicator_")
        if "/" in name or "\\" in name or not data_re.fullmatch(base):
            raise ValueError(f"{code}: unexpected file in zip: {name!r}")
        if info.file_size > MAX_CSV_BYTES:
            raise ValueError(f"{code}: {name} is {info.file_size} bytes, too large")
        files[name] = zf.read(info)

    data = [n for n in files if n.startswith("API_")]
    if len(data) != 1:
        raise ValueError(f"{code}: expected one API_*.csv in zip, found {data}")
    expected = {data[0], f"Metadata_Country_{data[0]}", f"Metadata_Indicator_{data[0]}"}
    if set(files) != expected:
        raise ValueError(f"{code}: zip has {sorted(files)}, expected {sorted(expected)}")
    return files


def replace_folder(folder: Path, files: dict[str, bytes]) -> None:
    """Swap folder's contents for files, leaving the old folder intact until the end."""
    tmp = folder.with_name(f".{folder.name}.new")
    old = folder.with_name(f".{folder.name}.old")
    for p in (tmp, old):
        if p.exists():
            shutil.rmtree(p)
    tmp.mkdir(parents=True)
    for name, content in files.items():
        (tmp / name).write_bytes(content)
    if folder.exists():
        folder.rename(old)
    tmp.rename(folder)
    if old.exists():
        shutil.rmtree(old)


def fetch(folder_name: str, code: str, sources: Path) -> Path:
    url = URL.format(code=code)
    log.info("%s: downloading %s", folder_name, url)
    files = extract(download(url), code)
    folder = sources / folder_name
    replace_folder(folder, files)
    data = next(n for n in files if n.startswith("API_"))
    log.info("%s: saved %s (%d bytes)", folder_name, data, len(files[data]))
    return folder / data


def fetch_all(sources: Path = DEFAULT_SOURCES, only: list[str] | None = None) -> int:
    """Fetch the selected indicators; returns the number that failed."""
    names = only or list(INDICATORS)
    unknown = [n for n in names if n not in INDICATORS]
    if unknown:
        raise SystemExit(f"unknown indicator folder(s) {unknown}; choose from {list(INDICATORS)}")
    failures = 0
    for name in names:
        try:
            fetch(name, INDICATORS[name], sources)
        except Exception as exc:
            failures += 1
            log.error("%s: fetch failed, keeping existing files: %s", name, exc)
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="*", help=f"subset of {list(INDICATORS)} (default: all)")
    ap.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return 1 if fetch_all(args.sources, args.folders) else 0


if __name__ == "__main__":
    sys.exit(main())
