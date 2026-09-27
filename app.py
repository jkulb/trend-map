import pandas as pd
import plotly.express as px
import streamlit as st

import data_sources as ds

# ---- Fill these in to personalize the footer; left blank, they just don't render ----
AUTHOR_NAME = "Yevgen K"
GITHUB_URL = ""
LINKEDIN_URL = ""

st.set_page_config(page_title="Trend Atlas", page_icon="\U0001F30D", layout="wide")

# Streamlit auto-shows a "Press Enter to submit form" hint under every
# text_input inside an st.form - purely cosmetic, so it's hidden with a
# scoped CSS override rather than restructuring the form to avoid it.
st.markdown(
    "<style>div[data-testid='InputInstructions'] { display: none; }</style>",
    unsafe_allow_html=True,
)

# Defined in data_sources.py (not here) so refresh_cache.py can warm the
# cache for the exact same pairs shown below - see the comment on it there.
QUICK_EXAMPLES = ds.QUICK_EXAMPLES

# --------------------------------------------------------------------------
# Session state / sidebar controls
# --------------------------------------------------------------------------
#
# The two term boxes are bound to session_state via an explicit `key=` (and
# no `value=` argument) rather than the more obvious-looking
# `st.text_input(..., value=st.session_state["word1"])`. That obvious version
# has a real bug: Streamlit derives a widget's identity from a hash that
# includes its `value` argument when no `key` is given, so every time
# `word1` changed (i.e. after every "Compare" click), the box got a *new*
# identity and lost track of whatever the user had just typed into the *old*
# one - showing stale text, or applying an edit to the wrong term. Binding to
# a stable `key` instead makes session_state the single source of truth for
# the box's live content, so its identity never moves.

st.session_state.setdefault("word1_input", QUICK_EXAMPLES[0][0])
st.session_state.setdefault("word2_input", QUICK_EXAMPLES[0][1])
st.session_state.setdefault("word1", QUICK_EXAMPLES[0][0])
st.session_state.setdefault("word2", QUICK_EXAMPLES[0][1])
st.session_state.setdefault("timeframe_label", "Past 90 days")


def _apply_quick_example(a: str, b: str) -> None:
    """Button on_click callback: runs before the rerun it triggers, so it's
    safe to assign straight into the text inputs' own widget keys here -
    doing the same assignment further down the script (after those widgets
    have already been instantiated this run) would raise a
    StreamlitAPIException."""
    st.session_state["word1_input"] = a
    st.session_state["word2_input"] = b
    st.session_state["word1"] = a
    st.session_state["word2"] = b


with st.sidebar:
    st.header("Compare two terms")
    with st.form("compare_form"):
        st.text_input("Term 1", key="word1_input")
        st.text_input("Term 2", key="word2_input")
        timeframe_label = st.selectbox(
            "Time window",
            options=list(ds.TIMEFRAME_OPTIONS.keys()),
            index=list(ds.TIMEFRAME_OPTIONS.keys()).index(st.session_state["timeframe_label"]),
        )
        submitted = st.form_submit_button("Compare", use_container_width=True)
        if submitted:
            st.session_state["word1"] = st.session_state["word1_input"].strip() or QUICK_EXAMPLES[0][0]
            st.session_state["word2"] = st.session_state["word2_input"].strip() or QUICK_EXAMPLES[0][1]
            st.session_state["timeframe_label"] = timeframe_label

    st.caption("Quick examples (pre-cached, always work even if Google rate-limits live lookups):")
    cols = st.columns(2)
    for i, (a, b) in enumerate(QUICK_EXAMPLES):
        cols[i % 2].button(f"{a} vs {b}", use_container_width=True, on_click=_apply_quick_example, args=(a, b))

word1 = st.session_state["word1"]
word2 = st.session_state["word2"]
timeframe = ds.TIMEFRAME_OPTIONS[st.session_state["timeframe_label"]]

st.title("Search trends by state")
st.caption(f"**{word1}** vs **{word2}** — {st.session_state['timeframe_label'].lower()}")

with st.expander("About this project"):
    st.markdown(
        f"""
        The motication was to create a script that plots Google Trends 
        interest on a map. This version wraps that idea in a small
        Streamlit app, maps US state-level interest instead, and adds a
        second free data source so the comparison isn't just a map:

        - **Google Trends** (via `pytrends`) — relative search interest by
          US state and over time, plus related/rising queries.
        - **Wikipedia** (via Wikimedia's pageviews + opensearch APIs) — daily
          pageviews for each term's (auto-matched) Wikipedia article.

        This only took a couple hours to make, think of all the cool stuff I can build for you!
        """
    )

tab_map, tab_time, tab_wiki = st.tabs(
    ["\U0001F5FA Map comparison", "\U0001F4C8 Interest Over Time", "\U0001F4D6 Wikipedia Attention"]
)


def _cache_banner(result, label: str) -> None:
    """Show a small banner when data came from cache (and especially if stale)."""
    if result.stale:
        st.warning(
            f"Live {label} lookup failed ({result.error}); showing the last cached snapshot "
            f"from {result.fetched_at[:19].replace('T', ' ')} UTC."
        )
    elif result.from_cache:
        st.caption(f"Cached {label} snapshot from {result.fetched_at[:19].replace('T', ' ')} UTC.")


# --------------------------------------------------------------------------
# Tab 1: Trend Map
# --------------------------------------------------------------------------

with tab_map:
    try:
        state_result = ds.get_trends_by_state(word1, word2, timeframe)
    except ds.DataUnavailable as exc:
        st.error(f"Couldn't load state-level trends data: {exc}")
    else:
        _cache_banner(state_result, "state-level trends")
        df = state_result.data

        if df.empty:
            st.info("No US states had enough search volume for both terms in this window.")
        else:
            max_abs = max(abs(df["difference"].min()), abs(df["difference"].max()), 1)
            fig = px.choropleth(
                df,
                locations="state_abbr",
                locationmode="USA-states",
                scope="usa",
                color="difference",
                hover_name="state_name",
                hover_data={word1: True, word2: True, "state_abbr": False, "difference": ":.0f"},
                color_continuous_scale="RdBu",
                range_color=(-max_abs, max_abs),
                title=f"Where '{word1}' beats '{word2}' (red) vs. the reverse (blue), by US state",
            )
            fig.update_layout(coloraxis_colorbar=dict(title="Difference"), margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

            col1, col2 = st.columns(2)
            with col1:
                st.subheader(f"Strongest for '{word1}'")
                st.dataframe(
                    df.nlargest(5, "difference")[["state_name", word1, word2, "difference"]],
                    hide_index=True,
                    use_container_width=True,
                )
            with col2:
                st.subheader(f"Strongest for '{word2}'")
                st.dataframe(
                    df.nsmallest(5, "difference")[["state_name", word1, word2, "difference"]],
                    hide_index=True,
                    use_container_width=True,
                )

# --------------------------------------------------------------------------
# Tab 2: Interest Over Time + related queries
# --------------------------------------------------------------------------

with tab_time:
    try:
        time_result = ds.get_trends_over_time(word1, word2, timeframe)
    except ds.DataUnavailable as exc:
        st.error(f"Couldn't load the time series: {exc}")
    else:
        _cache_banner(time_result, "time series")
        df = time_result.data
        fig = px.line(df, x="date", y=[word1, word2], labels={"value": "Relative interest (0-100)", "date": ""})
        fig.update_layout(legend_title_text="", margin=dict(l=0, r=0, t=20, b=0))
        st.plotly_chart(fig, use_container_width=True)

    try:
        related_result = ds.get_related_queries(word1, word2, timeframe)
    except ds.DataUnavailable as exc:
        st.info(f"Related queries unavailable: {exc}")
    else:
        _cache_banner(related_result, "related queries")
        related = related_result.data
        col1, col2 = st.columns(2)
        for col, term in zip((col1, col2), (word1, word2)):
            with col:
                st.subheader(term)
                tables = related.get(term, {"top": [], "rising": []})
                st.markdown("**Top related**")
                top_df = pd.DataFrame(tables["top"])
                st.dataframe(top_df if not top_df.empty else pd.DataFrame({"query": ["(none)"]}), hide_index=True, use_container_width=True)
                st.markdown("**Rising**")
                rising_df = pd.DataFrame(tables["rising"])
                st.dataframe(rising_df if not rising_df.empty else pd.DataFrame({"query": ["(none)"]}), hide_index=True, use_container_width=True)

# --------------------------------------------------------------------------
# Tab 3: Wikipedia pageviews
# --------------------------------------------------------------------------

with tab_wiki:
    try:
        wiki_result = ds.get_wikipedia_pageviews(word1, word2, st.session_state["timeframe_label"])
    except ds.DataUnavailable as exc:
        st.error(f"Couldn't load Wikipedia pageviews: {exc}")
    else:
        _cache_banner(wiki_result, "Wikipedia pageviews")
        df = wiki_result.data
        title1 = df.attrs.get("title1", word1)
        title2 = df.attrs.get("title2", word2)
        if title1 != word1 or title2 != word2:
            st.caption(f"Matched to Wikipedia articles: **{title1}** and **{title2}**.")
        fig = px.line(
            df, x="date", y=[word1, word2],
            labels={"value": "Daily pageviews (English Wikipedia)", "date": ""},
            title="Wikipedia attention over time",
        )
        fig.update_layout(legend_title_text="", margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

# --------------------------------------------------------------------------
# Footer
# --------------------------------------------------------------------------

footer_bits = []
if AUTHOR_NAME:
    footer_bits.append(f"Built by {AUTHOR_NAME}")
if GITHUB_URL:
    footer_bits.append(f"[GitHub]({GITHUB_URL})")
if LINKEDIN_URL:
    footer_bits.append(f"[LinkedIn]({LINKEDIN_URL})")
if footer_bits:
    st.divider()
    st.caption(" · ".join(footer_bits))
