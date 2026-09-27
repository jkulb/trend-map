"""
Data access layer for Trend Atlas.

Wraps two free, no-API-key data sources behind a single caching pattern:
  - Google Trends (via pytrends)     - relative search interest by US state and over time
  - Wikimedia Pageviews + opensearch - daily pageviews for each term's Wikipedia article

Every fetch function follows the same contract: try a live call, fall back to
the most recent cached snapshot if the live call fails (rate limit, network
error, etc.), and raise DataUnavailable only if neither works. This matters
because pytrends is an unofficial, unauthenticated client that Google
rate-limits aggressively, especially from shared/cloud IPs - a public demo
app calling it live on every visit will fail intermittently without this.

(This module used to also wrap the GDELT 2.0 DOC API for a News Coverage
tab. It was dropped - GDELT's free tier throttled hard enough, often enough,
that it undermined the demo more than it added to it. Wikimedia's pageviews
API replaced it: same idea (compare two terms via a second free source), and
in practice it turned out to need the same request pacing/retry treatment
GDELT did - its documented anonymous-tier limits are generous, but a handful
of back-to-back calls with zero pacing (title-resolve x2 + pageviews x2 per
comparison) was still enough to trip a live 429.)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import pycountry
import requests

# Wikimedia asks API callers to identify themselves (app + a way to reach the
# maintainer) in the User-Agent rather than authenticating with a key - see
# https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
USER_AGENT = "trend-atlas-portfolio-app/1.0 (https://github.com/; personal project, not for resale)"
REQUEST_TIMEOUT = 15  # seconds

# Generic per-host pacing + retry-with-backoff, applied to every HTTP data
# source (currently just Wikipedia). Google Trends doesn't go through this -
# pytrends makes its own requests internally.
_MIN_REQUEST_INTERVAL = {"en.wikipedia.org": 0.5, "wikimedia.org": 0.5}
_last_request_at: dict[str, float] = {}


def _get_with_retry(url: str, params: Optional[dict] = None, max_retries: int = 2) -> requests.Response:
    """requests.get with per-host pacing and retry-with-backoff on 429s.

    A handful of back-to-back calls to the same host with zero pacing is
    enough to trip a 429 in practice, even against a source (Wikimedia) whose
    documented anonymous-tier limits are generous - so every HTTP source
    gets this for free rather than relying on the docs being the whole story.
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    min_interval = _MIN_REQUEST_INTERVAL.get(host, 0.2)

    for attempt in range(max_retries + 1):
        elapsed = time.monotonic() - _last_request_at.get(host, 0.0)
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        _last_request_at[host] = time.monotonic()

        if resp.status_code != 429:
            return resp

        if attempt == max_retries:
            resp.raise_for_status()  # out of retries - raise the 429 as a normal HTTPError

        # Respect Retry-After if the server sent one; otherwise back off
        # exponentially (2s, 4s, ...) since 429 responses rarely include it.
        wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
        time.sleep(wait)

    raise AssertionError("unreachable")  # loop always returns or raises above

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# Single source of truth for the sidebar's quick-example buttons in app.py -
# refresh_cache.py also reads this list, so the two can never drift apart
# (they used to be two separate hardcoded lists; refresh_cache.py's copy
# went stale after the examples changed, so it was warming the cache for
# pairs the app didn't even show anymore, leaving the real ones to hit
# GDELT's rate limit live, every time, on the very first page load).
QUICK_EXAMPLES = [
    ("Solana", "Ethereum"),
    ("pandas", "numpy"),
    ("Macbook Air", "Macbook Pro"),
    ("Venmo", "Paypal"),
    ("Coke", "Pepsi"),
]

class DataUnavailable(Exception):
    """Raised when a live fetch fails and no cached fallback exists."""


def _safe_json(resp: requests.Response, source_name: str) -> Any:
    """resp.json() that raises a diagnosable DataUnavailable instead of a bare
    json.JSONDecodeError, in case a source ever returns a 200 with a
    malformed/empty body instead of a proper error status.
    """
    try:
        return resp.json()
    except ValueError as exc:
        snippet = resp.text[:200].strip() or "(empty body)"
        raise DataUnavailable(f"{source_name} returned a non-JSON response (HTTP {resp.status_code}): {snippet!r}.") from exc


@dataclass
class FetchResult:
    """A dataframe plus metadata about where it came from."""

    data: pd.DataFrame
    from_cache: bool
    stale: bool
    fetched_at: Optional[str]
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Generic disk cache: one JSON file per cache key, storing a timestamp and a
# JSON-serializable payload. Kept deliberately simple (no DB) since the app
# is a single-user portfolio piece, not a production service.
# --------------------------------------------------------------------------


def _cache_path(key: str) -> Path:
    # Hash long/unsafe keys (arbitrary search terms) into a stable filename.
    safe = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{key[:40].replace('/', '_').replace(' ', '_')}_{safe}.json"


def _load_cache(key: str) -> Optional[dict]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(key: str, payload: Any) -> None:
    path = _cache_path(key)
    record = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(record, f)
    except OSError:
        pass  # cache is best-effort; never let a write failure break the app


def _cache_age_hours(record: dict) -> float:
    fetched_at = datetime.fromisoformat(record["fetched_at"])
    return (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600


def fetch_with_cache(
    key: str,
    fetch_fn: Callable[[], Any],
    ttl_hours: float,
    to_frame: Callable[[Any], pd.DataFrame],
) -> FetchResult:
    """Shared fetch-then-fallback-to-cache logic used by every data source.

    fetch_fn returns a JSON-serializable payload (list/dict of plain values).
    to_frame converts that payload into the DataFrame callers want.
    """
    cached = _load_cache(key)
    if cached is not None and _cache_age_hours(cached) < ttl_hours:
        return FetchResult(
            data=to_frame(cached["payload"]),
            from_cache=True,
            stale=False,
            fetched_at=cached["fetched_at"],
        )

    try:
        payload = fetch_fn()
        _save_cache(key, payload)
        return FetchResult(data=to_frame(payload), from_cache=False, stale=False, fetched_at=None)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure falls back to cache
        if cached is not None:
            return FetchResult(
                data=to_frame(cached["payload"]),
                from_cache=True,
                stale=True,
                fetched_at=cached["fetched_at"],
                error=str(exc),
            )
        raise DataUnavailable(str(exc)) from exc


# --------------------------------------------------------------------------
# US state code helpers (offline - pycountry ships ISO 3166-2 subdivision
# data, so both directions of the state name <-> postal-abbreviation lookup
# come from the same source instead of a hand-built dict).
# --------------------------------------------------------------------------

# code is "US-CA" -> abbr "CA"; keep only the 50 states + DC, since those are
# the only ones Plotly's built-in "USA-states" choropleth can draw (it has no
# shapes for territories like Puerto Rico or Guam).
_US_SUBDIVISIONS = [s for s in pycountry.subdivisions if s.country_code == "US"]
STATE_ABBR_TO_NAME = {
    s.code.replace("US-", ""): s.name for s in _US_SUBDIVISIONS if s.type in ("State", "District")
}
STATE_NAME_TO_ABBR = {name: abbr for abbr, name in STATE_ABBR_TO_NAME.items()}


def is_plottable_us_state(abbr: str) -> bool:
    return abbr in STATE_ABBR_TO_NAME


# --------------------------------------------------------------------------
# Google Trends (pytrends)
# --------------------------------------------------------------------------

TIMEFRAME_OPTIONS = {
    "Past 7 days": "now 7-d",
    "Past 30 days": "today 1-m",
    "Past 90 days": "today 3-m",
    "Past 12 months": "today 12-m",
    "Past 5 years": "today 5-y",
}


def _pytrends_client():
    from pytrends.request import TrendReq

    return TrendReq(hl="en-US", tz=360)


def get_trends_by_state(word1: str, word2: str, timeframe: str) -> FetchResult:
    """Search interest by US state for two terms.

    Setting geo="US" on the payload scopes the whole comparison to US
    searchers; resolution="REGION" (pytrends' term for state-level, once
    geo="US") is what breaks it down by state rather than returning one
    national number. Returns columns: state_abbr, state_name, <word1>,
    <word2>, difference.
    """
    key = f"trends_state_{word1}_{word2}_{timeframe}"

    def fetch_fn() -> Any:
        pytrends = _pytrends_client()
        pytrends.build_payload(kw_list=[word1, word2], timeframe=timeframe, geo="US")
        df = pytrends.interest_by_region(resolution="REGION", inc_low_vol=True, inc_geo_code=True)
        if df.empty:
            raise DataUnavailable(f"Google Trends returned no state-level data for '{word1}' / '{word2}'.")
        df = df.reset_index().rename(columns={"geoName": "state_name_google"})
        return df.to_dict(orient="records")

    def to_frame(payload: Any) -> pd.DataFrame:
        df = pd.DataFrame(payload)
        # geoCode looks like "US-CA"; keep only well-formed, plottable codes
        # rather than trusting every row (defends against Google occasionally
        # including a territory or an unexpected code in this field).
        df["state_abbr"] = df["geoCode"].str.replace("US-", "", regex=False)
        df = df[df["state_abbr"].apply(is_plottable_us_state)].copy()
        df["state_name"] = df["state_abbr"].map(STATE_ABBR_TO_NAME)
        df["difference"] = df[word1] - df[word2]
        return df[["state_abbr", "state_name", word1, word2, "difference"]]

    return fetch_with_cache(key, fetch_fn, ttl_hours=24, to_frame=to_frame)


def get_trends_over_time(word1: str, word2: str, timeframe: str) -> FetchResult:
    """Relative search interest over time for two terms (0-100 scale each)."""
    key = f"trends_time_{word1}_{word2}_{timeframe}"

    def fetch_fn() -> Any:
        pytrends = _pytrends_client()
        pytrends.build_payload(kw_list=[word1, word2], timeframe=timeframe)
        df = pytrends.interest_over_time()
        if df.empty:
            raise DataUnavailable(f"Google Trends returned no time series for '{word1}' / '{word2}'.")
        df = df.drop(columns=["isPartial"], errors="ignore").reset_index()
        df["date"] = df["date"].astype(str)
        return df.to_dict(orient="records")

    def to_frame(payload: Any) -> pd.DataFrame:
        df = pd.DataFrame(payload)
        df["date"] = pd.to_datetime(df["date"])
        return df

    return fetch_with_cache(key, fetch_fn, ttl_hours=24, to_frame=to_frame)


def get_related_queries(word1: str, word2: str, timeframe: str) -> FetchResult:
    """Top and rising related queries for each term."""
    key = f"trends_related_{word1}_{word2}_{timeframe}"

    def fetch_fn() -> Any:
        pytrends = _pytrends_client()
        pytrends.build_payload(kw_list=[word1, word2], timeframe=timeframe)
        raw = pytrends.related_queries()
        out = {}
        for kw, tables in raw.items():
            out[kw] = {
                "top": tables["top"].to_dict(orient="records") if tables["top"] is not None else [],
                "rising": tables["rising"].to_dict(orient="records") if tables["rising"] is not None else [],
            }
        return out

    def to_frame(payload: Any) -> pd.DataFrame:
        # Not tabular by nature; caller uses the dict directly. Wrap so the
        # generic fetch_with_cache signature still applies.
        return payload  # type: ignore[return-value]

    return fetch_with_cache(key, fetch_fn, ttl_hours=24, to_frame=to_frame)


# --------------------------------------------------------------------------
# Wikipedia pageviews - a second free, keyless data source, comparing how
# much attention each term's Wikipedia article gets over time.
# --------------------------------------------------------------------------

# Actual day counts, not GDELT-style capped/approximate windows - Wikimedia's
# pageviews API has full daily data back to mid-2015, so even "Past 5 years"
# is well within range and just means more points on the line chart.
WIKI_TIMEFRAME_DAYS = {
    "Past 7 days": 7,
    "Past 30 days": 30,
    "Past 90 days": 90,
    "Past 12 months": 365,
    "Past 5 years": 5 * 365,
}


def _wikipedia_resolve_title(term: str) -> str:
    """Resolve a free-text search term to its canonical Wikipedia article
    title, using the "opensearch" endpoint - the same one that powers
    Wikipedia's own search-suggestion dropdown (e.g. "solana" resolves to
    "Solana (blockchain platform)", not a literal page called "Solana").
    """
    resp = _get_with_retry(
        "https://en.wikipedia.org/w/api.php",
        {"action": "opensearch", "search": term, "limit": 1, "namespace": 0, "format": "json"},
    )
    resp.raise_for_status()
    _query, titles, _descriptions, _urls = _safe_json(resp, "Wikipedia search")
    if not titles:
        raise DataUnavailable(f"No Wikipedia article found for '{term}'.")
    return titles[0]


def _resolve_title_cached(term: str) -> str:
    """Wraps _wikipedia_resolve_title with its own long-lived cache entry,
    independent of the pageviews cache key (which also varies by timeframe).
    A term's canonical Wikipedia title essentially never changes, so there's
    no reason to spend another request re-resolving it just because the user
    switched the time window on a term that's already been looked up."""
    cache_key = f"wiki_title_{term}"
    cached = _load_cache(cache_key)
    if cached is not None and _cache_age_hours(cached) < 24 * 7:
        return cached["payload"]
    title = _wikipedia_resolve_title(term)
    _save_cache(cache_key, title)
    return title


def _wikipedia_pageviews(title: str, days: int) -> pd.DataFrame:
    """Daily pageviews for one Wikipedia article over the last `days` days."""
    end = datetime.now(timezone.utc).date() - timedelta(days=1)  # yesterday - today's count isn't final yet
    start = end - timedelta(days=days)
    article = title.replace(" ", "_")
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"en.wikipedia/all-access/user/{article}/daily/{start:%Y%m%d}/{end:%Y%m%d}"
    )
    resp = _get_with_retry(url)
    if resp.status_code == 404:
        # Wikimedia uses 404 (not a 200 with an empty list) for "no data in
        # this range" - a brand-new article, or one with no traffic at all.
        raise DataUnavailable(f"Wikimedia has no pageview data for '{title}' in this window.")
    resp.raise_for_status()
    items = _safe_json(resp, "Wikipedia pageviews").get("items", [])
    if not items:
        raise DataUnavailable(f"Wikimedia returned no pageview data for '{title}'.")
    return pd.DataFrame({"date": [it["timestamp"] for it in items], "views": [it["views"] for it in items]})


def get_wikipedia_pageviews(word1: str, word2: str, timeframe_label: str) -> FetchResult:
    """Daily Wikipedia pageviews for the two terms' (auto-resolved) articles.

    Returns a DataFrame with columns date, <word1>, <word2>, plus the
    resolved article titles on .attrs["title1"] / .attrs["title2"] (set fresh
    on every call, live or cached, so this survives the disk-cache round-trip
    even though DataFrame.attrs itself isn't part of what gets cached).
    """
    days = WIKI_TIMEFRAME_DAYS.get(timeframe_label, 90)
    key = f"wiki_views_{word1}_{word2}_{days}"

    def fetch_fn() -> Any:
        title1 = _resolve_title_cached(word1)
        title2 = _resolve_title_cached(word2)
        return {
            "title1": title1,
            "title2": title2,
            "df1": _wikipedia_pageviews(title1, days).to_dict(orient="records"),
            "df2": _wikipedia_pageviews(title2, days).to_dict(orient="records"),
        }

    def to_frame(payload: Any) -> pd.DataFrame:
        d1 = pd.DataFrame(payload["df1"]).rename(columns={"views": word1})
        d2 = pd.DataFrame(payload["df2"]).rename(columns={"views": word2})
        merged = pd.merge(d1, d2, on="date", how="outer").sort_values("date")
        merged["date"] = pd.to_datetime(merged["date"], format="%Y%m%d%H")
        merged.attrs["title1"] = payload["title1"]
        merged.attrs["title2"] = payload["title2"]
        return merged

    return fetch_with_cache(key, fetch_fn, ttl_hours=24, to_frame=to_frame)
