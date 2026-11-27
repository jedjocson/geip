"""Fetch World Bank indicators from the Indicators API (v2) for the target countries.

    https://api.worldbank.org/v2/country/ARM;PHL/indicator/NY.GDP.MKTP.CD?format=json

For each indicator this makes one snapshot holding everything the API returned for it:
the observations for the target countries (all years), the indicator metadata and the
target countries' metadata. The snapshot is saved as JSON under responses/ (the raw
response, kept for auditing) and parsed into the same ingest.Parsed structure the CSV
path produces, so ingest.load() handles dedupe, revisions and reconciliation as before.

Snapshots are named by content hash, so fetching unchanged data writes no new file and
ingest skips it as already loaded.

Usage:
    python ingest.py --api              # fetch from the API, then load
    python ingest.py --api --dry-run    # fetch + validate only
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import ingest

BASE_URL = "https://api.worldbank.org/v2"
INDICATORS = list(ingest.UNITS)  # GDP, inflation, unemployment, population
DEFAULT_RESPONSES = ingest.ROOT / "responses"
PER_PAGE = 1000
TIMEOUT = 60
USER_AGENT = "geip-ingest/1.0"

log = logging.getLogger("wb_api")


class WorldBankError(RuntimeError):
    """The API answered, but not with data (bad code, error message, non-JSON)."""


# --------------------------------------------------------------------------- HTTP


def make_session() -> requests.Session:
    """A session that retries timeouts, connection errors and 429/5xx with backoff."""
    retry = Retry(
        total=4,
        backoff_factor=1,  # 1s, 2s, 4s, 8s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def get_page(session: requests.Session, path: str, params: dict) -> list:
    """One API call. Returns the decoded [meta, rows] list."""
    url = f"{BASE_URL}/{path}"
    resp = session.get(url, params={**params, "format": "json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError as exc:
        raise WorldBankError(f"{resp.url}: response is not JSON: {resp.text[:200]!r}") from exc
    # Errors come back as HTTP 200 with [{"message": [...]}].
    if isinstance(body, list) and body and isinstance(body[0], dict) and "message" in body[0]:
        msgs = "; ".join(f"{m.get('key')}: {m.get('value')}" for m in body[0]["message"])
        raise WorldBankError(f"{resp.url}: {msgs}")
    if not (isinstance(body, list) and len(body) == 2 and isinstance(body[0], dict)):
        raise WorldBankError(f"{resp.url}: unexpected response shape")
    return body


def get_all(session: requests.Session, path: str, params: dict | None = None) -> tuple[dict, list]:
    """Follow pagination; returns (metadata of the first page, all rows)."""
    params = {**(params or {}), "per_page": PER_PAGE}
    meta, rows = get_page(session, path, {**params, "page": 1})
    rows = list(rows or [])  # an empty result has rows = null
    for page in range(2, int(meta.get("pages") or 1) + 1):
        _, more = get_page(session, path, {**params, "page": page})
        rows.extend(more or [])
    total = int(meta.get("total") or 0)
    if len(rows) != total:
        raise WorldBankError(f"{path}: API reported {total} rows, received {len(rows)}")
    return meta, rows


# --------------------------------------------------------------------------- fetching


def fetch_countries(session: requests.Session, targets: list[str]) -> list[dict]:
    """Metadata for the target countries that the API knows; warns about the rest.

    One unknown code makes the whole observations request fail, so the full
    country list is fetched first and unknown targets are dropped.
    """
    _, rows = get_all(session, "country")
    by_code = {r["id"]: r for r in rows}
    unknown = [c for c in targets if c not in by_code]
    if unknown:
        log.warning("target countries unknown to the World Bank API, skipped: %s", unknown)
    return [by_code[c] for c in targets if c in by_code]


def fetch_snapshot(session: requests.Session, code: str, countries: list[dict]) -> dict:
    codes = [c["id"] for c in countries]
    meta, obs = get_all(session, f"country/{';'.join(codes)}/indicator/{code}")
    _, [indicator] = get_all(session, f"indicator/{code}")
    return {
        "indicator_code": code,
        "lastupdated": meta.get("lastupdated"),
        "indicator": indicator,
        "countries": countries,
        "observations": obs,
    }


def save_snapshot(snapshot: dict, out_dir: Path) -> tuple[Path, str]:
    """Write the snapshot as <out_dir>/<indicator>/<lastupdated>_<sha12>.json."""
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(blob).hexdigest()
    folder = out_dir / snapshot["indicator_code"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{snapshot['lastupdated'] or 'unknown'}_{sha[:12]}.json"
    if not path.exists():
        path.write_bytes(blob)
    return path, sha


# --------------------------------------------------------------------------- parsing


def parse_snapshot(path: Path) -> ingest.Parsed:
    """Turn a saved snapshot into the same Parsed structure ingest.parse() returns."""
    blob = path.read_bytes()
    snap = json.loads(blob)

    rows = [
        {
            "country_code": r["countryiso3code"],
            "country_name": r["country"]["value"],
            "indicator_code": r["indicator"]["id"],
            "year": r["date"],
            "value": r["value"],
        }
        for r in snap["observations"]
        if r["value"] is not None
    ]
    obs = pd.DataFrame(rows, columns=["country_code", "country_name", "indicator_code", "year", "value"])
    bad_years = obs.loc[~obs.year.astype(str).str.fullmatch(r"\d{4}"), "year"].unique()
    if len(bad_years):  # e.g. quarterly "2023Q1" if a non-annual indicator is added
        raise ValueError(f"{path.name}: non-annual dates {list(bad_years)[:5]}")
    obs["year"] = obs["year"].astype(int)
    obs["value"] = obs["value"].astype(float)

    countries = pd.DataFrame(
        {
            "country_code": [c["id"] for c in snap["countries"]],
            "country_name": [c["name"].strip() for c in snap["countries"]],
            "region": [c["region"]["value"].strip() or None for c in snap["countries"]],
            "income_group": [c["incomeLevel"]["value"].strip() or None for c in snap["countries"]],
        }
    )
    countries["is_aggregate"] = countries["region"] == "Aggregates"
    countries.loc[countries.is_aggregate, ["region", "income_group"]] = None
    countries = countries.astype(object).where(countries.notna(), None)

    ind = snap["indicator"]
    indicators = pd.DataFrame(
        [
            {
                "indicator_code": ind["id"],
                "indicator_name": ind["name"].strip(),
                "unit": ingest.unit_for(ind["id"], ind["name"]),
                "source_note": ind.get("sourceNote") or None,
                "source_org": ind.get("sourceOrganization") or None,
            }
        ]
    )

    updated = snap.get("lastupdated")
    src = ingest.SourceFile(
        data=path,
        country_meta=path,  # everything lives in the one snapshot file
        indicator_meta=path,
        sha256=hashlib.sha256(blob).hexdigest(),
        source_updated=date.fromisoformat(updated) if updated else None,
    )
    parsed = ingest.Parsed(src, obs, countries, indicators)
    ingest.validate(parsed)
    return parsed


def fetch(targets: list[str], out_dir: Path = DEFAULT_RESPONSES,
          indicators: list[str] | None = None, session: requests.Session | None = None
          ) -> tuple[list[ingest.Parsed], int]:
    """Fetch, save and parse every indicator. Returns (parsed, number of failures)."""
    session = session or make_session()
    countries = fetch_countries(session, targets)
    if not countries:
        raise SystemExit("none of the target countries are known to the World Bank API")

    parsed, failures = [], 0
    for code in indicators or INDICATORS:
        try:
            snapshot = fetch_snapshot(session, code, countries)
            path, _ = save_snapshot(snapshot, out_dir)
            parsed.append(parse_snapshot(path))
            log.info("%s: %d observations, updated %s -> %s",
                     code, len(parsed[-1].observations), snapshot["lastupdated"],
                     path.relative_to(ingest.ROOT) if path.is_relative_to(ingest.ROOT) else path)
        except Exception as exc:
            failures += 1
            log.error("%s: fetch failed: %s", code, exc)
    return parsed, failures
