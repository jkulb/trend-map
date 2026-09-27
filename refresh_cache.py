"""
Prebuild the disk cache so the app has good data to show immediately -
useful before a demo/interview, and a safety net for when Google rate-limits
pytrends on a fresh cloud IP.

The cloud sandbox this project was built in cannot reach trends.google.com
or en.wikipedia.org/wikimedia.org (its outbound network is locked to package
registries only), so this script has not been run against the real APIs
yet. Run it yourself on a machine with normal internet access:

    python refresh_cache.py

It fetches the curated example pairs shown in the sidebar and reports any
failures with the real error message from each API - that is also where
you'd find out if a response shape has drifted from what data_sources.py
expects.
"""

import sys
import time

import data_sources as ds

# Reuses app.py's sidebar quick-example list (defined once, in data_sources.py)
# rather than keeping a separate copy here - a separate copy previously went
# stale after the examples changed, so this script kept warming the cache for
# pairs the app no longer showed.
CURATED_PAIRS = ds.QUICK_EXAMPLES

DEFAULT_TIMEFRAME = ds.TIMEFRAME_OPTIONS["Past 90 days"]


def _run(label: str, fn) -> None:
    try:
        result = fn()
        n = len(result.data) if hasattr(result.data, "__len__") else "?"
        print(f"  OK   {label} ({n} rows)")
    except ds.DataUnavailable as exc:
        print(f"  FAIL {label}: {exc}")
    except Exception as exc:  # noqa: BLE001 - report anything unexpected, don't stop the run
        print(f"  FAIL {label}: unexpected error: {exc}")
    time.sleep(1)  # be polite to pytrends' unauthenticated, rate-limited endpoint


def main() -> None:
    print("Refreshing Google Trends cache for curated pairs (US, by state)...")
    for w1, w2 in CURATED_PAIRS:
        _run(f"trends state   {w1} vs {w2}", lambda w1=w1, w2=w2: ds.get_trends_by_state(w1, w2, DEFAULT_TIMEFRAME))
        _run(f"trends time    {w1} vs {w2}", lambda w1=w1, w2=w2: ds.get_trends_over_time(w1, w2, DEFAULT_TIMEFRAME))
        _run(f"trends related {w1} vs {w2}", lambda w1=w1, w2=w2: ds.get_related_queries(w1, w2, DEFAULT_TIMEFRAME))

    print("\nRefreshing Wikipedia pageviews cache for curated pairs...")
    for w1, w2 in CURATED_PAIRS:
        _run(f"wiki views     {w1} vs {w2}", lambda w1=w1, w2=w2: ds.get_wikipedia_pageviews(w1, w2, "Past 90 days"))

    print("\nDone. Cached files are in cache/ - commit that folder if you want the deployed app to")
    print("start with working examples even before its first live refresh.")


if __name__ == "__main__":
    sys.exit(main())
