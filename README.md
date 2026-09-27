# Trend Atlas

Compare US search interest between two terms, by state: an interactive
choropleth, a time series with related/rising queries, and a Wikipedia
pageviews comparison. Built from an old script that plotted Google Trends
*country* data with matplotlib/geopandas; this version is a Streamlit app,
mapped by US state instead, with an interactive Plotly map.

(Two earlier features were tried and dropped, both for the same reason -
free/keyless API tiers that failed unpredictably often enough to undermine
a portfolio demo rather than add to it. See git history for either:
- A "Development Correlation" tab joining the map against US Census Bureau
  data - Census's anonymous tier returned a 200 with an empty body instead
  of a proper error often enough to be unusable without an API key.
- A "News Coverage" tab backed by GDELT's DOC API (news volume/tone) -
  GDELT's free tier 429'd hard enough, often enough, even with request
  pacing and retry/backoff, that uncached terms failed live more often than
  they worked. Wikipedia's pageviews API replaced it below - same idea, a
  notably more reliable free source, though even it still 429s occasionally
  in practice (see below) despite Wikimedia's documented limits sounding
  generous.)

## What changed from the original script

- **No more `gpd.datasets`** — removed in geopandas 1.0, which broke the
  original map loading. This version doesn't use geopandas at all: Plotly's
  built-in USA-states map (`locationmode="USA-states"`) draws the choropleth
  directly.
- **No more hand-built name-mapping dict** — the original matched Google
  Trends' country names against geopandas' names via a manual dictionary,
  which silently drops any entry whose spelling differs and wasn't in the
  dict. This version joins on state postal codes instead: `pytrends`'
  `inc_geo_code=True` returns them (e.g. `"US-CA"`), and `pycountry`'s
  offline ISO 3166-2 subdivision data resolves the abbreviation <-> full
  name in both directions - no maintenance needed.
- **Static map -> interactive** — Plotly choropleth with hover tooltips
  instead of `plt.show()`.
- **Caching with fallback** — `pytrends` is an unofficial, unauthenticated
  client that Google rate-limits, especially from shared/cloud IPs. Every
  fetch is cached to disk with a TTL; if a live call fails, the app falls
  back to the last good snapshot and says so, rather than crashing.
- **A second free data source** — Wikipedia pageviews, keyless. A term is
  first resolved to its canonical Wikipedia article via the same
  `opensearch` endpoint that powers Wikipedia's own search-suggestion
  dropdown (so "solana" correctly matches "Solana (blockchain platform)"),
  then Wikimedia's pageviews API returns a daily view-count series for it.
  Both calls go through the same per-host pacing + 429 retry-with-backoff
  helper originally built for GDELT (`_get_with_retry` in
  `data_sources.py`) — Wikimedia's limits are documented as generous, but a
  live 429 showed up in practice anyway, so this gets the same defensive
  treatment. The resolved title is also cached separately from the
  pageviews data, with a much longer TTL (a week), since a term's canonical
  article rarely changes but was otherwise being re-looked-up on every
  timeframe switch.

## Project layout

```
app.py             Streamlit app (three tabs: map, time series, Wikipedia)
data_sources.py    All API calls + the disk-cache-with-fallback logic
refresh_cache.py   Prebuilds the cache for the sidebar's quick-example pairs
tests/             Unit tests (data layer) + a full-app smoke test, all mocked
cache/             JSON snapshots, created on first run (safe to delete)
```

## Setup

Recommended: give this project its own virtual environment rather than
installing into a shared/anaconda base one - keeps its dependencies from
colliding with whatever else is installed there.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt       # to run the app
pip install -r requirements-dev.txt   # to also run tests/ (adds pytest)
```

Run everything through the interpreter (`python3 -m pytest`, `python3 -m
streamlit run app.py`) rather than bare `pytest`/`streamlit` commands if
your shell was already open before you activated `.venv` - some shells
cache a command's resolved path from before `PATH` changed and will keep
running the old (base-env) copy otherwise.

### Troubleshooting: `pytest` fails with a `jinja2`/`flask`/`dash` ImportError

Only relevant if you skip the venv above and run tests from a shared
anaconda base environment. It's an environment conflict, not a bug in this
project: some other package installed there (commonly `dash`, which
registers a `pytest11` entry point for its own testing utilities) pulls in
an old `flask` that does `from jinja2 import escape` - removed from
Jinja2's top-level API in newer versions. It happens during pytest's plugin
auto-discovery, before any test in this repo even runs.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/ -q
```

### Troubleshooting: the app still shows an old version after you relaunch it

Streamlit doesn't replace an already-running server when you run `streamlit
run app.py` again - if a previous instance is still up (another terminal
tab, or one you thought you'd closed), it's usually still bound to port
8501, and the new command either refuses to start or moves itself to 8502.
If your browser tab is pointed at a stale bookmarked `localhost:8501` while
the process serving fresh code is actually on 8502 (or vice versa), you'll
keep seeing old text/behavior no matter how many times you edit and
relaunch. Fully stop every running instance first (Ctrl+C in each terminal
that has one, or `pkill -f streamlit`), then start one fresh copy and open
the exact URL it prints.

## Important: run `refresh_cache.py` before your first demo

**This project was built in a sandbox with no outbound access to
`trends.google.com` or `en.wikipedia.org`/`wikimedia.org` — only package
registries were reachable, so neither data source has been exercised
against the real, live APIs yet.** The code is written against each API's
documented/observed response shape and is covered by unit tests with
realistic mocked payloads (`pytest` — 20 tests, all passing), but a live
check on your machine is worth doing before you show this to anyone:

```bash
python refresh_cache.py
```

This hits both APIs for the curated example pairs and prints `OK` or `FAIL
<reason>` per call. If a response shape has changed since this was written,
this is where it will surface, with the real error message — check the
corresponding function in `data_sources.py` against the API's current
behavior if so. It also warms the cache so the sidebar's "quick example"
buttons work instantly and survive Google/Wikimedia rate-limiting your IP
mid-demo.

## Run

```bash
streamlit run app.py
```

## Deploying for job applications

[Streamlit Community Cloud](https://streamlit.io/cloud) is free and gives
you a public URL: push this folder to a GitHub repo (commit the `cache/`
folder too, so the app has good data from the first visit), connect the
repo on Streamlit Cloud, and point it at `app.py`. Add your GitHub/LinkedIn
links at the top of `app.py` (`AUTHOR_NAME`, `GITHUB_URL`, `LINKEDIN_URL`)
before deploying.

## Extending it

- `pytrends` also exposes `trending_searches()` and `suggestions()`, which
  could seed an autocomplete for the term inputs.
- Google Trends also supports city/DMA-level breakdown within the US
  (`resolution="CITY"` / `"DMA"` with `geo="US"`) if state-level isn't
  granular enough.
- Wikipedia's pageviews API also has a `top` endpoint (most-viewed articles
  for a given day/month) - could seed a "trending on Wikipedia right now"
  sidebar panel using data already being fetched for a different purpose.
- The opensearch-based title resolution is a simple heuristic (first match
  wins) - genuinely ambiguous terms (e.g. "Apple") will sometimes resolve to
  a less-obvious article than intended; showing the matched title in the
  tab (already done) is the mitigation rather than a perfect disambiguator.
