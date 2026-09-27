"""
Unit tests for data_sources.py.

Note: the sandbox this app was written in has no route to trends.google.com
or en.wikipedia.org/wikimedia.org (outbound network is locked to package
registries only), so these tests mock every network call rather than
hitting the real APIs. The response shapes below are taken from each API's
documented/observed schema. Before deploying, run `python refresh_cache.py`
on a machine with normal internet access - if a shape has drifted, that is
where it will surface, with the real error message from the API.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import data_sources as ds


def _fake_response(payload, status_code=200):
    def raise_for_status():
        if status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"{status_code} error")

    return SimpleNamespace(status_code=status_code, raise_for_status=raise_for_status, json=lambda: payload, text="")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the module's disk cache at a throwaway directory per test."""
    monkeypatch.setattr(ds, "CACHE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """_last_request_at is module-level state shared across every test in
    this file - without clearing it, a test running soon after another one
    that hit the same host sees a tiny "elapsed" gap and actually sleeps for
    real (up to _MIN_REQUEST_INTERVAL), making the suite slow and order-
    dependent for no reason (nothing here is testing real wall-clock pacing
    across tests)."""
    ds._last_request_at.clear()


# --------------------------------------------------------------------------
# US state code helpers
# --------------------------------------------------------------------------


def test_state_abbr_to_name_covers_50_states_plus_dc():
    assert len(ds.STATE_ABBR_TO_NAME) == 51
    assert ds.STATE_ABBR_TO_NAME["CA"] == "California"
    assert ds.STATE_ABBR_TO_NAME["DC"] == "District of Columbia"


def test_state_name_to_abbr_is_the_reverse_mapping():
    assert ds.STATE_NAME_TO_ABBR["California"] == "CA"


def test_is_plottable_us_state_excludes_territories():
    assert ds.is_plottable_us_state("CA") is True
    assert ds.is_plottable_us_state("PR") is False  # Puerto Rico - no shape in Plotly's USA-states map
    assert ds.is_plottable_us_state("ZZ") is False


# --------------------------------------------------------------------------
# Generic cache: fresh hit, fallback-on-error, and hard failure
# --------------------------------------------------------------------------


def test_fetch_with_cache_uses_live_data_on_first_call():
    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        return {"x": 1}

    result = ds.fetch_with_cache("k", fetch_fn, ttl_hours=1, to_frame=lambda p: pd.DataFrame([p]))
    assert calls["n"] == 1
    assert not result.from_cache
    assert result.data.iloc[0]["x"] == 1


def test_fetch_with_cache_serves_fresh_cache_without_calling_fetch_fn():
    ds.fetch_with_cache("k", lambda: {"x": 1}, ttl_hours=1, to_frame=lambda p: pd.DataFrame([p]))

    def should_not_run():
        raise AssertionError("fetch_fn should not run while cache is fresh")

    result = ds.fetch_with_cache("k", should_not_run, ttl_hours=1, to_frame=lambda p: pd.DataFrame([p]))
    assert result.from_cache
    assert not result.stale


def test_fetch_with_cache_falls_back_to_stale_cache_on_error():
    ds.fetch_with_cache("k", lambda: {"x": 1}, ttl_hours=0, to_frame=lambda p: pd.DataFrame([p]))

    def failing():
        raise RuntimeError("429 Too Many Requests")

    result = ds.fetch_with_cache("k", failing, ttl_hours=0, to_frame=lambda p: pd.DataFrame([p]))
    assert result.from_cache
    assert result.stale
    assert "429" in result.error
    assert result.data.iloc[0]["x"] == 1


def test_fetch_with_cache_raises_when_no_cache_and_fetch_fails():
    def failing():
        raise RuntimeError("network unreachable")

    with pytest.raises(ds.DataUnavailable):
        ds.fetch_with_cache("k", failing, ttl_hours=1, to_frame=lambda p: pd.DataFrame([p]))


# --------------------------------------------------------------------------
# Google Trends by US state - mock the pytrends client, not the network
# --------------------------------------------------------------------------


class _FakeTrendReq:
    """Stands in for pytrends.request.TrendReq with a canned response."""

    def __init__(self, region_df):
        self._region_df = region_df
        self.build_payload_calls = []

    def build_payload(self, **kwargs):
        self.build_payload_calls.append(kwargs)

    def interest_by_region(self, **kwargs):
        return self._region_df


def test_get_trends_by_state_joins_on_geo_code_and_computes_difference(monkeypatch):
    state_df = pd.DataFrame(
        {
            "python": [80, 40],
            "javascript": [20, 90],
            "geoCode": ["US-CA", "US-TX"],
        },
        index=pd.Index(["California", "Texas"], name="geoName"),
    )
    fake_client = _FakeTrendReq(state_df)
    monkeypatch.setattr(ds, "_pytrends_client", lambda: fake_client)

    result = ds.get_trends_by_state("python", "javascript", "today 3-m")
    df = result.data.set_index("state_abbr")

    assert not result.from_cache
    assert df.loc["CA", "python"] == 80
    assert df.loc["CA", "difference"] == 60
    assert df.loc["TX", "difference"] == -50
    assert df.loc["CA", "state_name"] == "California"
    # geo="US" is what makes interest_by_region(resolution="REGION") return
    # states instead of a single national number - assert it was actually set.
    assert fake_client.build_payload_calls[0]["geo"] == "US"


def test_get_trends_by_state_drops_non_plottable_geo_codes(monkeypatch):
    # Google Trends can include territories (e.g. Puerto Rico) that have no
    # shape in Plotly's USA-states map; these should be dropped, not crash.
    state_df = pd.DataFrame(
        {"a": [10], "b": [5], "geoCode": ["US-PR"]},
        index=pd.Index(["Puerto Rico"], name="geoName"),
    )
    monkeypatch.setattr(ds, "_pytrends_client", lambda: _FakeTrendReq(state_df))

    result = ds.get_trends_by_state("a", "b", "today 3-m")
    assert result.data.empty


def test_get_trends_by_state_empty_response_raises(monkeypatch):
    monkeypatch.setattr(ds, "_pytrends_client", lambda: _FakeTrendReq(pd.DataFrame()))
    with pytest.raises(ds.DataUnavailable):
        ds.get_trends_by_state("a", "b", "today 3-m")


# --------------------------------------------------------------------------
# Wikipedia - opensearch title resolution + pageviews
# --------------------------------------------------------------------------


def test_wikipedia_resolve_title_returns_first_opensearch_match(monkeypatch):
    # opensearch's real response shape is a 4-element array: [query, titles,
    # descriptions, urls] - not an object, so this is worth pinning down.
    opensearch_response = _fake_response(["solana", ["Solana (blockchain platform)"], ["..."], ["https://..."]])
    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: opensearch_response)

    assert ds._wikipedia_resolve_title("solana") == "Solana (blockchain platform)"


def test_wikipedia_resolve_title_raises_when_no_article_found(monkeypatch):
    empty_response = _fake_response(["asdfqwerty12345", [], [], []])
    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: empty_response)

    with pytest.raises(ds.DataUnavailable, match="No Wikipedia article"):
        ds._wikipedia_resolve_title("asdfqwerty12345")


def test_wikipedia_pageviews_parses_items_into_dataframe(monkeypatch):
    payload = {
        "items": [
            {"timestamp": "2026090100", "views": 120},
            {"timestamp": "2026090200", "views": 150},
        ]
    }
    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: _fake_response(payload))

    df = ds._wikipedia_pageviews("Solana (blockchain platform)", days=7)
    assert list(df["views"]) == [120, 150]
    assert list(df["date"]) == ["2026090100", "2026090200"]


def test_wikipedia_pageviews_404_raises_data_unavailable(monkeypatch):
    # Wikimedia's pageviews API returns 404 (not a 200 with an empty list)
    # when there's no data for an article in the requested range.
    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: _fake_response({}, status_code=404))

    with pytest.raises(ds.DataUnavailable, match="no pageview data"):
        ds._wikipedia_pageviews("Some Obscure Article", days=7)


def test_get_wikipedia_pageviews_merges_both_terms_and_exposes_resolved_titles(monkeypatch):
    def fake_resolve(term):
        return {"solana": "Solana (blockchain platform)", "ethereum": "Ethereum"}[term]

    def fake_pageviews(title, days):
        base = {"Solana (blockchain platform)": 100, "Ethereum": 200}[title]
        return pd.DataFrame({"timestamp": ["2026090100", "2026090200"], "views": [base, base + 10]}).rename(
            columns={"timestamp": "date"}
        )

    monkeypatch.setattr(ds, "_wikipedia_resolve_title", fake_resolve)
    monkeypatch.setattr(ds, "_wikipedia_pageviews", fake_pageviews)

    result = ds.get_wikipedia_pageviews("solana", "ethereum", "Past 90 days")
    df = result.data

    assert list(df.columns) == ["date", "solana", "ethereum"]
    assert df["solana"].tolist() == [100, 110]
    assert pd.api.types.is_datetime64_any_dtype(df["date"])
    assert df.attrs["title1"] == "Solana (blockchain platform)"
    assert df.attrs["title2"] == "Ethereum"


def test_get_wikipedia_pageviews_propagates_failure_with_no_cache(monkeypatch):
    def failing(term):
        raise ds.DataUnavailable(f"No Wikipedia article found for '{term}'.")

    monkeypatch.setattr(ds, "_wikipedia_resolve_title", failing)
    with pytest.raises(ds.DataUnavailable):
        ds.get_wikipedia_pageviews("asdfqwerty12345", "ethereum", "Past 90 days")
