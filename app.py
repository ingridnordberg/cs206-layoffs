import os
import streamlit as st
import pandas as pd
import requests
import json
import re
import csv
import datetime as dt
from urllib.parse import urlparse, quote
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
import matplotlib.pyplot as plt

# --- CONFIG ---
st.set_page_config(page_title="Enriched WARN & Economic Intelligence", page_icon="🕵️", layout="wide")

# --- CONSTANTS ---
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FRIEND_API_KEY = "1451ccabe4de49a4af9119039e91376e"
CENSUS_API_BASE = "https://api.census.gov/data"

STATE_ABBR_TO_FIPS = {
    "AL":"01","AK":"02","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10","DC":"11",
    "FL":"12","GA":"13","HI":"15","ID":"16","IL":"17","IN":"18","IA":"19","KS":"20","KY":"21",
    "LA":"22","ME":"23","MD":"24","MA":"25","MI":"26","MN":"27","MS":"28","MO":"29","MT":"30",
    "NE":"31","NV":"32","NH":"33","NJ":"34","NM":"35","NY":"36","NC":"37","ND":"38","OH":"39",
    "OK":"40","OR":"41","PA":"42","RI":"44","SC":"45","SD":"46","TN":"47","TX":"48","UT":"49",
    "VT":"50","VA":"51","WA":"53","WV":"54","WI":"55","WY":"56","PR":"72"
}

# --- INITIALIZATION ---
if 'website_cache' not in st.session_state:
    st.session_state.website_cache = {}
if 'outlets_cache' not in st.session_state:
    st.session_state.outlets_cache = {}
if 'current_results' not in st.session_state:
    st.session_state.current_results = []

# --- HELPERS ---
def is_valid_loc(loc):
    if pd.isna(loc) or str(loc).lower().strip() in ['nan', 'unknown', 'n/a', '']:
        return False
    return True

@st.cache_data
def load_county_fips(path="county_fips.csv"):
    m = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row["state_abbr"].upper(), row["county_name"])
                m[key] = row["county_fips"].zfill(5)
    except FileNotFoundError:
        return None
    return m

# --- SERIES BUILDERS ---
def laus_state_series(state_abbr, seasonal="S"):
    fips = STATE_ABBR_TO_FIPS[state_abbr]
    area = f"ST{fips}{'0'*11}"
    return f"LA{seasonal}{area}03"

def laus_county_series(county_fips5):
    area = f"CN{county_fips5}{'0'*8}"
    return f"LAU{area}03"

# --- TEXT HELPERS ---
def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()

def looks_like_full_address(loc: str) -> bool:
    return bool(re.search(r"\b\d{1,6}\b", loc or ""))

def clean_location_for_query(loc: str, state: str) -> str:
    if not is_valid_loc(loc):
        return state
    loc = str(loc).strip()
    if len(loc) > 60 or looks_like_full_address(loc):
        return state
    if re.search(r"\b[A-Z]{2}\b", loc) or "," in loc:
        return loc
    return f"{loc}, {state}"

def generate_macro_madlib(view_type, state, county, bls_df, pop_df, work_df):
    if view_type == "County" and county:
        geography = f"{county}, {state}"
    else:
        geography = state

    unemployment_text = "unemployment data is currently unavailable"
    if bls_df is not None and not bls_df.empty:
        latest = bls_df.iloc[-1]["value"]
        old = bls_df.iloc[-6]["value"] if len(bls_df) >= 6 else bls_df.iloc[0]["value"]

        if latest > old:
            trend = "increased"
        elif latest < old:
            trend = "decreased"
        else:
            trend = "remained stable"

        unemployment_text = f"the unemployment rate is **{latest:.1f}%**, which has **{trend}** over the past six months"

    population_text = "population data is currently unavailable"
    if pop_df is not None and not pop_df.empty:
        population = int(pop_df.iloc[-1]["population"])
        population_text = f"the region has a population of **{population:,}**"

    workforce_text = "labor force data is currently unavailable"
    if work_df is not None and not work_df.empty:
        workforce = int(work_df.iloc[-1]["workforce"])
        workforce_text = f"with roughly **{workforce:,}** people in the labor force"

    return f"""
**Regional Economic Snapshot**

For **{geography}**, the available macroeconomic indicators suggest the following:

• **Labor Market:** Currently, {unemployment_text}.  
• **Population:** According to recent Census estimates, {population_text}.  
• **Labor Force:** The region is estimated to have {workforce_text}.

These indicators provide context for understanding broader labor market conditions in this area.
"""

LAYOFF_KEYWORDS = [
    "layoff", "layoffs", "job cut", "job cuts", "cut jobs",
    "reduction in force", "rif", "redundant", "redundancies",
    "downsizing", "workforce reduction", "termination", "terminations",
    "warn notice", "warn", "pink slips"
]

BAD_DOMAINS = [
    "linkedin.com", "repvue.com", "glassdoor.com", "indeed.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
]

def contains_layoff_signal(title: str, snippet: str) -> bool:
    t = normalize_text(title)
    s = normalize_text(snippet)
    blob = f"{t} {s}"
    return any(k in blob for k in LAYOFF_KEYWORDS)

def domain_is_bad(url: str) -> bool:
    u = (url or "").lower()
    return any(d in u for d in BAD_DOMAINS)

# --- DATA FETCHERS ---
@st.cache_data
def fetch_census_workforce_trend(geography_type, state_abbr, county_fips5=None, start_year=2018, end_year=2023):
    data = []
    state_fips = STATE_ABBR_TO_FIPS.get(state_abbr)
    if not state_fips:
        return pd.DataFrame()

    for year in range(start_year, end_year + 1):
        try:
            url = f"{CENSUS_API_BASE}/{year}/acs/acs5"
            if geography_type == "State":
                params = {"get": "B23025_002E", "for": f"state:{state_fips}"}
            else:
                if not county_fips5:
                    continue
                s_fips2, c_fips3 = county_fips5[:2], county_fips5[2:]
                params = {"get": "B23025_002E", "for": f"county:{c_fips3}", "in": f"state:{s_fips2}"}

            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                res = r.json()
                if len(res) > 1:
                    data.append({"year": year, "workforce": int(res[1][0])})
        except:
            continue

    return pd.DataFrame(data)

@st.cache_data
def fetch_census_population_trend(geography_type, state_abbr, county_fips5=None, start_year=2018, end_year=2023):
    data = []
    state_fips = STATE_ABBR_TO_FIPS.get(state_abbr)
    if not state_fips:
        return pd.DataFrame()

    for year in range(start_year, end_year + 1):
        try:
            url = f"{CENSUS_API_BASE}/{year}/acs/acs5"
            if geography_type == "State":
                params = {"get": "B01003_001E", "for": f"state:{state_fips}"}
            else:
                if not county_fips5:
                    continue
                s_fips2, c_fips3 = county_fips5[:2], county_fips5[2:]
                params = {"get": "B01003_001E", "for": f"county:{c_fips3}", "in": f"state:{s_fips2}"}

            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                res = r.json()
                if len(res) > 1:
                    data.append({"year": year, "population": int(res[1][0])})
        except:
            continue

    return pd.DataFrame(data)

# --- LOGIC FUNCTIONS ---
def generate_narrative(company_row, full_df, bls_df):
    company = company_row['company']
    location = company_row['location']
    jobs = company_row['jobs']
    date = company_row['notice_date']
    state = company_row['postal_code']
    loc_display = location if is_valid_loc(location) else f"the state of {state}"

    bls_text = "Local economic trend data is currently unavailable."
    if bls_df is not None and not bls_df.empty:
        latest_rate = bls_df.iloc[-1]['value']
        old_idx = -6 if len(bls_df) >= 6 else 0
        old_rate = bls_df.iloc[old_idx]['value']
        trend = "increased" if latest_rate > old_rate else "decreased" if latest_rate < old_rate else "stagnated"
        bls_text = f"Over the past 6 months, unemployment in this region has {trend}, moving from {old_rate}% to {latest_rate}%."

    recurring_text = ""
    if is_valid_loc(location):
        ninety_days_ago = date - pd.Timedelta(days=90)
        recent_warns = full_df[
            (full_df['location'] == location) &
            (full_df['notice_date'] >= ninety_days_ago) &
            (full_df['notice_date'] <= date)
        ]
        count_90 = len(recent_warns)
        total_90 = recent_warns['jobs'].sum()
        recurring_text = f"\n\n**Recurring Activity:** This is the **{count_90}th** WARN notice filed in **{location}** in the past 90 days, totaling **{int(total_90) if not pd.isna(total_90) else 'Unknown'}** layoffs."

    job_str = f"{int(jobs)}" if not pd.isna(jobs) else "an unspecified number of"
    return f"**Context:** In {date.strftime('%B %Y')}, **{company}** announced **{job_str}** layoffs in **{loc_display}**. {bls_text}{recurring_text}"

def find_company_website(company, location, api_key):
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', company).lower()
    loc_query = location if is_valid_loc(location) else ""
    query = f'"{company}" {loc_query} official corporate homepage'
    url = "https://google.serper.dev/search"
    headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}

    try:
        r = requests.post(url, headers=headers, data=json.dumps({"q": query, "num": 10}))
        results = r.json().get('organic', [])
        candidates = []

        for hit in results:
            link = hit.get('link', '').lower()
            if any(b in link for b in ['linkedin', 'facebook', 'wikipedia']):
                continue
            score = len(urlparse(link).path)
            if clean_name in link:
                score -= 200
            candidates.append((score, hit.get('link')))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
    except:
        pass

    return None

def guess_industry(company_name):
    name = str(company_name).lower()
    mapping = {
        'AgTech & Farming': ['farming', 'agri', 'farm', 'aero', 'vertical'],
        'Food & Beverage': ['baking', 'bakery', 'food', 'bread'],
        'Tech': ['space', 'systems', 'tech', 'software', 'data'],
        'Manufacturing': ['mfg', 'factory', 'industrial', 'steel']
    }
    for industry, keywords in mapping.items():
        if any(word in name for word in keywords):
            return industry
    return "General Business"

def run_investigation(row, api_key):
    company = row["company"]
    state = (row.get("postal_code") or "").strip().upper()
    raw_loc = row.get("location")

    st.session_state.website_cache[company] = find_company_website(company, raw_loc, api_key)

    loc_for_query = clean_location_for_query(raw_loc, state)
    layoff_terms = 'layoffs OR "job cuts" OR "WARN notice"'
    query = f'"{company}" ({layoff_terms}) {loc_for_query} -site:.gov'

    r = requests.post(
        "https://google.serper.dev/news",
        headers={"X-API-KEY": api_key},
        json={"q": query, "num": 20},
        timeout=20
    )
    hits = r.json().get("news", []) or []
    scored_results = []
    outlets = set()

    for hit in hits:
        title = hit.get("title", "")
        snippet = hit.get("snippet", "")
        link = hit.get("link", "")
        source = hit.get("source", "Unknown")

        if not link or domain_is_bad(link):
            continue

        score = fuzz.partial_ratio(company.lower(), title.lower())
        if not contains_layoff_signal(title, snippet) and score < 90:
            continue

        if score >= 55:
            outlets.add(source)
            scored_results.append({
                "source": source,
                "title": title,
                "link": link,
                "score": score
            })

        if len(scored_results) >= 8:
            break

    if not scored_results:
        fallback_query = f'"{company}" (layoffs OR "job cuts" OR "WARN notice") -site:.gov'
        r2 = requests.post(
            "https://google.serper.dev/news",
            headers={"X-API-KEY": api_key},
            json={"q": fallback_query, "num": 20},
            timeout=20
        )
        hits2 = r2.json().get("news", []) or []

        for hit in hits2:
            title = hit.get("title", "")
            snippet = hit.get("snippet", "")
            link = hit.get("link", "")
            source = hit.get("source", "Unknown")

            if not link or domain_is_bad(link):
                continue

            score = fuzz.partial_ratio(company.lower(), title.lower())
            if not contains_layoff_signal(title, snippet) and score < 90:
                continue

            if score >= 55:
                outlets.add(source)
                scored_results.append({
                    "source": source,
                    "title": title,
                    "link": link,
                    "score": score
                })

            if len(scored_results) >= 8:
                break

    st.session_state.outlets_cache[company] = ", ".join(sorted(outlets)) if outlets else "No news found"
    return scored_results

def bls_fetch(series_ids, startyear, endyear, api_key=FRIEND_API_KEY):
    r = requests.post(
        BLS_URL,
        json={
            "seriesid": series_ids,
            "startyear": str(startyear),
            "endyear": str(endyear),
            "registrationKey": api_key
        },
        timeout=25
    )
    return r.json()

def parse_monthly_to_df(resp_json):
    out = []
    for series in resp_json.get("Results", {}).get("series", []):
        sid = series["seriesID"]
        for row in series["data"]:
            if not row["period"].startswith("M"):
                continue
            year, month = int(row["year"]), int(row["period"][1:])
            v = row["value"]
            if v == "-" or v == "":
                continue
            out.append({"date": dt.date(year, month, 1), "value": float(v), "seriesID": sid})

    df = pd.DataFrame(out)
    if not df.empty:
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
    return df

def fmt_val(x, suffix=""):
    return "N/A" if x is None or pd.isna(x) else f"{x:.2f}{suffix}"

# --- UI MAIN ---
st.title("Enriched WARN Investigator")
uploaded_file = st.file_uploader("Upload integrated.csv", type="csv")
bls_df = pd.DataFrame()

if uploaded_file:
    df_raw = pd.read_csv(uploaded_file)
    df_raw.columns = df_raw.columns.str.strip().str.lower()
    df_active = df_raw[df_raw['is_superseded'] == False].copy() if 'is_superseded' in df_raw.columns else df_raw.copy()
    df_active['notice_date'] = pd.to_datetime(df_active['notice_date'], errors='coerce')
    df_active['industry'] = df_active['company'].apply(guess_industry)

    st.sidebar.header("Investigation Filters")
    unique_states = sorted(df_active['postal_code'].dropna().unique())
    selected_states = st.sidebar.multiselect("Select States:", unique_states, default=unique_states[:1])
    date_range = st.sidebar.slider(
        "Notice Date Range:",
        df_active['notice_date'].min().to_pydatetime(),
        df_active['notice_date'].max().to_pydatetime(),
        (pd.Timestamp('2023-01-01').to_pydatetime(), df_active['notice_date'].max().to_pydatetime())
    )
    search_query = st.sidebar.text_input("Search Company Name:")

    filtered_df = df_active.copy()
    if selected_states:
        filtered_df = filtered_df[filtered_df['postal_code'].isin(selected_states)]
    filtered_df = filtered_df[
        (filtered_df['notice_date'] >= pd.Timestamp(date_range[0])) &
        (filtered_df['notice_date'] <= pd.Timestamp(date_range[1]))
    ]
    if search_query:
        filtered_df = filtered_df[filtered_df['company'].str.contains(search_query, case=False, na=False)]

    st.subheader(f"Filtered Layoff Records ({len(filtered_df)} entries)")
    st.dataframe(filtered_df[['notice_date', 'company', 'industry', 'location', 'jobs']], use_container_width=True)

    st.divider()
    st.header("Company Investigation & Narrative")
    col1, col2 = st.columns([1, 2])

    with col1:
        to_investigate = st.selectbox("Investigate a Company:", sorted(filtered_df['company'].dropna().unique()))
        if to_investigate:
            selected_row = filtered_df[filtered_df['company'] == to_investigate].iloc[0]
            display_loc = selected_row['location'] if is_valid_loc(selected_row['location']) else "Unknown Location"
            st.write(f"📍 **Location:** {display_loc}")

            if bls_df.empty:
                try:
                    state_code = selected_row['postal_code']
                    resp = bls_fetch([laus_state_series(state_code)], dt.date.today().year - 1, dt.date.today().year)
                    bls_df = parse_monthly_to_df(resp)
                except:
                    pass

            st.info(generate_narrative(selected_row, df_active, bls_df))

            # --- NEW: LinkedIn Source Finder ---
            st.markdown("---")
            st.subheader("🕵️ Source Hunting")
            # Build LinkedIn Search URL
            safe_company = quote(f'"{selected_row["company"]}"')
            safe_city = quote(f'"{selected_row["location"]}"') if is_valid_loc(selected_row["location"]) else ""
            li_url = f"https://www.linkedin.com/search/results/people/?keywords={safe_company}%20%22open%20to%20work%22%20{safe_city}"
            
            st.link_button("🤝 Find Interview Subjects on LinkedIn", li_url, use_container_width=True, help="Search for people recently at this company who are 'Open to Work'.")
    
    with col2:
        if st.button("Run Agentic Search ✨", help="Search the web and show relevant source links."):
            api_key = "57bb99cacfc8c06c15a4a046b909c95a6dd06248"
            with st.spinner("Searching..."):
                st.session_state.current_results = run_investigation(selected_row, api_key)
            st.rerun()

        if not st.session_state.get("current_results"):
            st.caption("When you run Agentic Search, the tool searches the web for recent news coverage about the selected company/layoff.")

        if st.session_state.get("current_results") is not None:
            outlets = st.session_state.outlets_cache.get(to_investigate, "")
            if outlets == "No news found":
                st.warning("No matching layoff/WARN news found for this query.")
            elif outlets:
                st.success(f"### Reported by: {outlets}")

            for res in st.session_state.current_results:
                with st.expander(f"{res['title']}"):
                    st.write(f"[Read Article]({res['link']})")

    st.divider()
    st.subheader("📈 Layoff Frequency (Filtered View)")
    chart_data = filtered_df.dropna(subset=['notice_date', 'jobs'])
    if not chart_data.empty:
        st.bar_chart(
            data=chart_data.set_index('notice_date').resample('MS')['jobs'].sum().reset_index(),
            x='notice_date',
            y='jobs',
            color="#ff4b4b"
        )

    st.divider()
    st.header("Macroeconomic Context (BLS & Census Data)")

    bls_state = selected_states[0] if selected_states else "CA"
    county_map = load_county_fips()

    colA, colB = st.columns(2)
    with colA:
        view_type = st.radio("View regional data by:", ["State", "County"], horizontal=True)

    with colB:
        curr_county_fips = None
        sel_county = None
        if view_type == "County" and county_map:
            available_counties = [k[1] for k in county_map.keys() if k[0] == bls_state]
            sel_county = st.selectbox("Select County:", sorted(available_counties))
            curr_county_fips = county_map[(bls_state, sel_county)]
        elif view_type == "County":
            st.error("⚠️ `county_fips.csv` not found.")
            view_type = "State"

    app_background_color, text_color, grid_color = "#0e1117", "#fafafa", "#444444"

    # Fetch all macro data first
    bls_fetch_df = pd.DataFrame()
    pop_df = pd.DataFrame()
    work_df = pd.DataFrame()

    series_id = laus_county_series(curr_county_fips) if view_type == "County" and curr_county_fips else laus_state_series(bls_state)

    with st.spinner("Loading macroeconomic data..."):
        try:
            bls_fetch_df = parse_monthly_to_df(
                bls_fetch([series_id], dt.date.today().year - 5, dt.date.today().year)
            )
        except Exception as e:
            st.error(f"BLS Error: {e}")

        try:
            pop_df = fetch_census_population_trend(view_type, bls_state, curr_county_fips)
        except Exception as e:
            st.error(f"Census Pop Error: {e}")

        try:
            work_df = fetch_census_workforce_trend(view_type, bls_state, curr_county_fips)
        except Exception as e:
            st.error(f"Census Workforce Error: {e}")

    # MadLib now renders after all data is fetched
    madlib_text = generate_macro_madlib(
        view_type=view_type,
        state=bls_state,
        county=sel_county,
        bls_df=bls_fetch_df,
        pop_df=pop_df,
        work_df=work_df
    )
    st.info(madlib_text)

    if not bls_fetch_df.empty:
        m1, m2, m3 = st.columns(3)
        m1.metric("Latest Unemployment", f"{fmt_val(bls_fetch_df.iloc[-1]['value'])}%")
        m3.metric("BLS Series ID", series_id)

        fig, ax = plt.subplots(figsize=(10, 3))
        fig.patch.set_facecolor(app_background_color)
        ax.set_facecolor(app_background_color)
        ax.bar(bls_fetch_df["date"], bls_fetch_df["value"], color="#1f77b4", width=20)
        ax.set_title(f"Unemployment Rate (%) — {sel_county if view_type == 'County' else bls_state}", color=text_color)
        ax.set_xlabel("Notice Month", color=text_color)
        ax.set_ylabel("Unemployment Rate (%)", color=text_color)
        ax.tick_params(axis='x', colors=text_color)
        ax.tick_params(axis='y', colors=text_color)
        ax.grid(True, alpha=0.3, color=grid_color)
        plt.tight_layout()
        st.pyplot(fig)

    st.divider()
    st.subheader(f"Population Context — {sel_county if view_type == 'County' else bls_state}")
    st.caption("Source: [U.S. Census Bureau - ACS 5-Year Estimates](https://www.census.gov/data.html)")
    if not pop_df.empty:
        st.metric("Latest Population", f"{pop_df.iloc[-1]['population']:,}")

        fig2, ax2 = plt.subplots(figsize=(10, 4))
        fig2.patch.set_facecolor(app_background_color)
        ax2.set_facecolor(app_background_color)
        ax2.plot(pop_df["year"], pop_df["population"], color="#2ca02c", marker='o', markersize=8, linewidth=2, linestyle='-')

        for _, row in pop_df.iterrows():
            ax2.text(
                row["year"],
                row["population"],
                f'{int(row["population"]):,}',
                color=text_color,
                ha='center',
                va='bottom',
                fontsize=9,
                fontweight='bold',
                bbox=dict(facecolor=app_background_color, alpha=0.6, edgecolor='none', pad=1)
            )

        ax2.set_title("Total Population Trend (ACS 5-year)", color=text_color)
        ax2.set_xlabel("Year", color=text_color)
        ax2.set_ylabel("Total Population", color=text_color)
        ax2.tick_params(axis='x', colors=text_color)
        ax2.tick_params(axis='y', colors=text_color)
        ax2.grid(True, alpha=0.3, color=grid_color)
        ax2.set_xticks(pop_df["year"])

        ymin, ymax = pop_df["population"].min(), pop_df["population"].max()
        padding = (ymax - ymin) * 0.5 if ymax != ymin else ymin * 0.05
        ax2.set_ylim(ymin - padding, ymax + padding)
        plt.tight_layout()
        st.pyplot(fig2)

    st.divider()
    st.subheader(f"Total Workforce Size — {sel_county if view_type == 'County' else bls_state}")
    st.caption("Source: [U.S. Census Bureau - Economic Characteristics](https://www.census.gov/data.html)")
    if not work_df.empty:
        st.metric("Latest Labor Force Size", f"{work_df.iloc[-1]['workforce']:,}")

        fig3, ax3 = plt.subplots(figsize=(10, 4))
        fig3.patch.set_facecolor(app_background_color)
        ax3.set_facecolor(app_background_color)
        ax3.plot(work_df["year"], work_df["workforce"], color="#9467bd", marker='o', markersize=8, linewidth=2, linestyle='-')

        for _, row in work_df.iterrows():
            ax3.text(
                row["year"],
                row["workforce"],
                f'{int(row["workforce"]):,}',
                color=text_color,
                ha='center',
                va='bottom',
                fontsize=9,
                fontweight='bold',
                bbox=dict(facecolor=app_background_color, alpha=0.6, edgecolor='none', pad=1)
            )

        ax3.set_title("Total Labor Force Trend (ACS 5-year)", color=text_color)
        ax3.set_xlabel("Year", color=text_color)
        ax3.set_ylabel("Labor Force Count", color=text_color)
        ax3.tick_params(axis='x', colors=text_color)
        ax3.tick_params(axis='y', colors=text_color)
        ax3.grid(True, alpha=0.3, color=grid_color)
        ax3.set_xticks(work_df["year"])

        ymin, ymax = work_df["workforce"].min(), work_df["workforce"].max()
        padding = (ymax - ymin) * 0.5 if ymax != ymin else ymin * 0.05
        ax3.set_ylim(ymin - padding, ymax + padding)
        plt.tight_layout()
        st.pyplot(fig3)