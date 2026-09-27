"""
Smoke test: run the whole Streamlit app end-to-end with every network call
mocked, and assert it renders without raising. Streamlit tabs all execute
their body on every script run (only the display is client-side), so one
at.run() exercises all three tabs.

This is the only check that the UI code (column layouts, dataframe/plotly
calls, etc.) actually works against the shapes data_sources.py produces -
the sandbox this was built in can't reach the real APIs to test app.py
against live data.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).parent.parent))
import data_sources as ds


def _fr(data, from_cache=False, stale=False, fetched_at=None, error=None):
    return ds.FetchResult(data=data, from_cache=from_cache, stale=stale, fetched_at=fetched_at, error=error)


@pytest.fixture(autouse=True)
def mock_data_sources(monkeypatch):
    state_df = pd.DataFrame(
        {
            "state_abbr": ["CA", "TX", "NY"],
            "state_name": ["California", "Texas", "New York"],
            "word1": [80, 40, 60],
            "word2": [20, 90, 55],
            "difference": [60, -50, 5],
        }
    ).rename(columns={"word1": "Python", "word2": "JavaScript"})

    time_df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=5, freq="D"),
            "Python": [10, 20, 30, 40, 50],
            "JavaScript": [50, 40, 30, 20, 10],
        }
    )

    related = {
        "Python": {"top": [{"query": "python tutorial", "value": 100}], "rising": [{"query": "python 3.13", "value": 250}]},
        "JavaScript": {"top": [{"query": "javascript array", "value": 100}], "rising": []},
    }

    wiki_df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=5, freq="D"),
            "Python": [1000, 1100, 1050, 1200, 1150],
            "JavaScript": [900, 950, 1000, 980, 1020],
        }
    )

    def fake_wiki(w1, w2, timeframe_label):
        df = wiki_df.rename(columns={"Python": w1, "JavaScript": w2})
        df.attrs["title1"] = w1
        df.attrs["title2"] = w2
        return _fr(df)

    monkeypatch.setattr(ds, "get_trends_by_state", lambda w1, w2, tf: _fr(state_df.rename(columns={"Python": w1, "JavaScript": w2})))
    monkeypatch.setattr(ds, "get_trends_over_time", lambda w1, w2, tf: _fr(time_df.rename(columns={"Python": w1, "JavaScript": w2})))
    monkeypatch.setattr(ds, "get_related_queries", lambda w1, w2, tf: _fr({w1: related["Python"], w2: related["JavaScript"]}))
    monkeypatch.setattr(ds, "get_wikipedia_pageviews", fake_wiki)


def test_app_runs_without_exception_on_default_terms():
    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"))
    at.run(timeout=30)
    assert not at.exception


def test_app_runs_after_switching_time_window():
    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"))
    at.run(timeout=30)
    assert not at.exception

    # Switch the time window and re-submit - exercises a fresh cache key /
    # fetch path for every tab.
    time_window = next(sb for sb in at.selectbox if sb.label == "Time window")
    time_window.set_value("Past 30 days")
    next(b for b in at.button if b.label == "Compare").click().run(timeout=30)
    assert not at.exception


def test_submitting_new_terms_replaces_both_words_not_just_one():
    """Regression test for a real bug: text_input's auto-generated widget
    identity depended on its `value=` argument (see the comment above
    word1_input/word2_input in app.py), so after the first "Compare" click,
    typing a second new pair and clicking "Compare" again silently kept
    showing the *first* submitted pair instead of the second. Binding the
    boxes to a stable `key=` instead of a changing `value=` fixes it - this
    test submits twice in a row and checks the second submission actually
    takes effect for both terms."""
    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"))
    at.run(timeout=30)

    at.text_input(key="word1_input").set_value("Rust")
    at.text_input(key="word2_input").set_value("Go")
    next(b for b in at.button if b.label == "Compare").click().run(timeout=30)
    assert at.session_state["word1"] == "Rust"
    assert at.session_state["word2"] == "Go"

    at.text_input(key="word1_input").set_value("Kotlin")
    at.text_input(key="word2_input").set_value("Swift")
    next(b for b in at.button if b.label == "Compare").click().run(timeout=30)
    assert at.session_state["word1"] == "Kotlin"
    assert at.session_state["word2"] == "Swift"


def test_app_shows_error_banner_when_a_source_is_unavailable(monkeypatch):
    def raise_unavailable(*a, **k):
        raise ds.DataUnavailable("Google Trends returned no state-level data for 'x' / 'y'.")

    monkeypatch.setattr(ds, "get_trends_by_state", raise_unavailable)

    at = AppTest.from_file(str(Path(__file__).parent.parent / "app.py"))
    at.run(timeout=30)
    assert not at.exception  # app must not crash - it should show st.error instead
    errors = [e.value for e in at.error]
    assert any("state-level trends data" in e for e in errors)
