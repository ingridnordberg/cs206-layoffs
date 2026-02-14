import os
import streamlit as st
import pandas as pd
import requests
import json
import re
import csv
import datetime as dt
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
import matplotlib.pyplot as plt

# --- CONFIG ---
st.set_page_config(page_title="Enriched WARN & Economic Intelligence", page_icon="🕵️", layout="wide")

# --- BLS CONSTANTS (Updated from Friend's Code) ---
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FRIEND_API_KEY = "1451ccabe4de49a4af9119039e91376e"

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

# --- COUNTY FIPS LOADER (Integrated from Friend's Code) ---
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
        # If the file isn't there yet, we won't crash, but we'll notify the user
        return None
    return m

# --- SERIES BUILDERS (Integrated from Friend's Code) ---
def laus_state_series(state_abbr, seasonal="S"):
    fips = STATE_ABBR_TO_FIPS[state_abbr]
    area = f"ST{fips}{'0'*11}"
    return f"LA{seasonal}{area}03"

def laus_county_series(county_fips5):
    area = f"CN{county_fips5}{'0'*8}"
    return f"LAU{area}03" # Counties typically NOT seasonally adjusted

# --- LOGIC FUNCTIONS: INVESTIGATION ---
def find_company_website(company, location, api_key):
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', company).lower()
    query = f'"{company}" official corporate homepage'
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "num": 10})
    headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
    try:
        r = requests.post(url, headers=headers, data=payload)
        results = r.json().get('organic', [])
        candidates = []
        for hit in results:
            link = hit.get('link', '').lower()
            if any(b in link for b in ['linkedin', 'facebook', 'wikipedia']): continue
            score = len(urlparse(link).path)
            if clean_name in link: score -= 200
            candidates.append((score, hit.get('link')))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
    except: pass
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
        if any(word in name for word in keywords): return industry
    return "General Business"

def run_investigation(row, api_key):
    company, location = row['company'], row['location']
    st.session_state.website_cache[company] = find_company_website(company, location, api_key)
    query = f'"{company}" layoffs {location} -site:.gov'
    r = requests.post("https://google.serper.dev/news", headers={'X-API-KEY': api_key}, json={"q": query})
    hits = r.json().get('news', [])
    scored_results, outlets = [], []
    for hit in hits[:5]:
        score = fuzz.partial_ratio(company.lower(), hit.get('title', '').lower())
        if score > 60:
            outlets.append(hit.get('source', 'Unknown'))
            scored_results.append({"source": hit.get('source'), "title": hit.get('title'), "link": hit['link'], "score": score})
    st.session_state.outlets_cache[company] = ", ".join(list(set(outlets))) if outlets else "No news found"
    return scored_results

# --- BLS FETCH & PARSE (Updated with Friend's Code) ---
def bls_fetch(series_ids, startyear, endyear, api_key=FRIEND_API_KEY):
    payload = {
        "seriesid": series_ids,
        "startyear": str(startyear),
        "endyear": str(endyear),
        "registrationKey": api_key,
    }
    r = requests.post(BLS_URL, json=payload, timeout=25)
    return r.json()

def parse_monthly_to_df(resp_json):
    # Converts friend's parse logic into a DataFrame for Streamlit
    out = []
    for series in resp_json.get("Results", {}).get("series", []):
        sid = series["seriesID"]
        for row in series["data"]:
            if not row["period"].startswith("M"): continue
            year, month = int(row["year"]), int(row["period"][1:])
            v = row["value"]
            if v == "-" or v == "": continue
            out.append({"date": dt.date(year, month, 1), "value": float(v), "seriesID": sid})
    df = pd.DataFrame(out)
    if not df.empty:
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
    return df

def fmt_val(x, suffix=""):
    return "N/A" if x is None or pd.isna(x) else f"{x:.2f}{suffix}"

# --- UI MAIN ---
st.title("🕵️‍♂️ Enriched WARN Investigator")
uploaded_file = st.file_uploader("Upload integrated.csv", type="csv")

if uploaded_file:
    # 1. LOAD & CLEAN
    df_raw = pd.read_csv(uploaded_file)
    df_raw.columns = df_raw.columns.str.strip().str.lower()
    
    if 'is_superseded' in df_raw.columns:
        df_active = df_raw[df_raw['is_superseded'] == False].copy()
    else:
        df_active = df_raw.copy()

    df_active['notice_date'] = pd.to_datetime(df_active['notice_date'], errors='coerce')
    df_active['industry'] = df_active['company'].apply(guess_industry)
    
    # 2. FILTERS
    st.sidebar.header("Investigation Filters")
    unique_states = sorted(df_active['postal_code'].dropna().unique())
    selected_states = st.sidebar.multiselect("Select States:", unique_states, default=["NY", "CA"] if "NY" in unique_states else unique_states[:1])
    
    date_range = st.sidebar.slider("Notice Date Range:", 
                                   df_active['notice_date'].min().to_pydatetime(), 
                                   df_active['notice_date'].max().to_pydatetime(), 
                                   (pd.Timestamp('2023-01-01').to_pydatetime(), df_active['notice_date'].max().to_pydatetime()))
    
    search_query = st.sidebar.text_input("Search Company Name:")

    # Apply Filters
    filtered_df = df_active.copy()
    if selected_states: filtered_df = filtered_df[filtered_df['postal_code'].isin(selected_states)]
    filtered_df = filtered_df[(filtered_df['notice_date'] >= pd.Timestamp(date_range[0])) & (filtered_df['notice_date'] <= pd.Timestamp(date_range[1]))]
    if search_query: filtered_df = filtered_df[filtered_df['company'].str.contains(search_query, case=False, na=False)]

    # 3. SPREADSHEET & TRENDS
    st.subheader(f"📊 Filtered Layoff Records ({len(filtered_df)} entries)")
    st.dataframe(filtered_df[['notice_date', 'company', 'industry', 'location', 'jobs']], use_container_width=True)

    st.divider()
    st.subheader("📈 Layoff Frequency (Filtered View)")
    chart_data = filtered_df.dropna(subset=['notice_date', 'jobs'])
    if not chart_data.empty:
        monthly_trend = chart_data.set_index('notice_date').resample('MS')['jobs'].sum().reset_index()
        st.area_chart(data=monthly_trend, x='notice_date', y='jobs', color="#ff4b4b")

    # 4. INVESTIGATION SEARCH
    st.divider()
    col1, col2 = st.columns([1, 2])
    with col1:
        to_investigate = st.selectbox("Investigate a Company:", sorted(filtered_df['company'].dropna().unique()))
        if st.button("🚀 Run Agentic Search"):
            api_key = "57bb99cacfc8c06c15a4a046b909c95a6dd06248"
            selected_row = filtered_df[filtered_df['company'] == to_investigate].iloc[0]
            st.session_state.current_results = run_investigation(selected_row, api_key)
            st.rerun()

    with col2:
        if to_investigate in st.session_state.website_cache:
            site = st.session_state.website_cache[to_investigate]
            if site: st.info(f"🌐 **Official Website:** [{site}]({site})")
            outlets = st.session_state.outlets_cache.get(to_investigate, "")
            if outlets and outlets != "No news found": st.success(f"### Reported by: {outlets}")
            if 'current_results' in st.session_state:
                for res in st.session_state.current_results:
                    with st.expander(f"{res['score']}% Match - {res['title']}"):
                        st.write(f"[Read Article]({res['link']})")

    # 5. MACROECONOMIC CONTEXT (Replaced/Upgraded with Friend's Code)
    st.divider()
    st.header("📉 Macroeconomic Context (BLS Data)")
    
    # Setup BLS selections
    bls_state = selected_states[0] if selected_states else "CA"
    county_map = load_county_fips()
    
    colA, colB = st.columns(2)
    with colA:
        view_type = st.radio("View BLS data by:", ["State", "County"], horizontal=True)
    with colB:
        if view_type == "County":
            if county_map:
                available_counties = [k[1] for k in county_map.keys() if k[0] == bls_state]
                sel_county = st.selectbox("Select County:", sorted(available_counties))
            else:
                st.error("⚠️ `county_fips.csv` not found. Showing State data instead.")
                view_type = "State"
    
    # Determine Series ID using friend's builders
    if view_type == "County" and county_map:
        fips = county_map[(bls_state, sel_county)]
        series_id = laus_county_series(fips)
        label = f"Unemployment Rate (%) — {sel_county}, {bls_state}"
    else:
        series_id = laus_state_series(bls_state)
        label = f"Unemployment Rate (%) — {bls_state}"

    # Fetch and Display
    try:
        current_year = dt.date.today().year
        resp = bls_fetch([series_id], current_year - 5, current_year)
        bls_df = parse_monthly_to_df(resp)

        if not bls_df.empty:
            # YoY Logic from Friend's code
            latest_row = bls_df.iloc[-1]
            cur_val = latest_row['value']
            
            # Find value from 1 year ago
            target_date = latest_row['date'] - pd.DateOffset(years=1)
            prev_row = bls_df[bls_df['date'] == target_date]
            yoy_val = cur_val - prev_row.iloc[0]['value'] if not prev_row.empty else None

            # Metrics
            m1, m2, m3 = st.columns(3)
            m1.metric("Latest Rate", f"{fmt_val(cur_val)}%")
            m2.metric("YoY Change", fmt_val(yoy_val, " pp"))
            m3.metric("Series ID", series_id)

            # Chart
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(bls_df["date"], bls_df["value"], color="#1f77b4", linewidth=2)
            ax.set_title(label)
            ax.set_ylabel("Percentage (%)")
            ax.grid(True, alpha=0.3)
            st.pyplot(fig)
        else:
            st.warning("No economic data returned for that selection.")
    except Exception as e:
        st.error(f"BLS Fetch Error: {e}")