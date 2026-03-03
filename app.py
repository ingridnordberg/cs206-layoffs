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

# --- BLS CONSTANTS ---
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

# --- COUNTY FIPS LOADER ---
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
def laus_series(area_code, measurement_code="03"):
    return f"LAU{area_code}{measurement_code}"

def get_area_code(state_abbr, county_fips=None):
    """
    Returns the 15-character LAUS Area Code.
    For broader 'City/Metro' area, we prioritize Metropolitan (MT) or County (CN).
    """
    if county_fips:
        # CN + 5-digit FIPS + 000000000 (standard county area)
        return f"CN{county_fips}00000000"
    fips = STATE_ABBR_TO_FIPS.get(state_abbr, "06")
    return f"ST{fips}00000000000"

# --- LOGIC FUNCTIONS ---
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

# --- BLS FETCH & PARSE ---
def bls_fetch(series_ids, startyear, endyear, api_key=FRIEND_API_KEY):
    payload = {
        "seriesid": series_ids,
        "startyear": str(startyear),
        "endyear": str(endyear),
        "registrationKey": api_key,
    }
    r = requests.post(BLS_URL, json=payload, timeout=25)
    return r.json()

def parse_bls_to_df(resp_json):
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

# --- UI MAIN ---
st.title("🕵️‍♂️ Enriched WARN Investigator")

st.warning("**Transparency Disclaimer:** WARN filings vary by state. City-level data covers the broader metropolitan area where available.")

uploaded_file = st.file_uploader("Upload integrated.csv", type="csv")

if uploaded_file:
    df_raw = pd.read_csv(uploaded_file)
    df_raw.columns = df_raw.columns.str.strip().str.lower()
    
    df_active = df_raw[df_raw['is_superseded'] == False].copy() if 'is_superseded' in df_raw.columns else df_raw.copy()
    df_active['notice_date'] = pd.to_datetime(df_active['notice_date'], errors='coerce')
    df_active['industry'] = df_active['company'].apply(guess_industry)
    
    st.sidebar.header("Investigation Filters")
    unique_states = sorted(df_active['postal_code'].dropna().unique())
    selected_states = st.sidebar.multiselect("Select States:", unique_states, default=unique_states[:1])
    
    date_range = st.sidebar.slider("Notice Date Range:", 
                                   df_active['notice_date'].min().to_pydatetime(), 
                                   df_active['notice_date'].max().to_pydatetime(), 
                                   (pd.Timestamp('2023-01-01').to_pydatetime(), df_active['notice_date'].max().to_pydatetime()))
    
    search_query = st.sidebar.text_input("Search Company Name:")

    filtered_df = df_active.copy()
    if selected_states: filtered_df = filtered_df[filtered_df['postal_code'].isin(selected_states)]
    filtered_df = filtered_df[(filtered_df['notice_date'] >= pd.Timestamp(date_range[0])) & (filtered_df['notice_date'] <= pd.Timestamp(date_range[1]))]
    if search_query: filtered_df = filtered_df[filtered_df['company'].str.contains(search_query, case=False, na=False)]

    st.subheader(f"📊 Filtered Layoff Records ({len(filtered_df)} entries)")
    st.dataframe(filtered_df[['notice_date', 'company', 'industry', 'location', 'jobs']], use_container_width=True)

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
            if 'current_results' in st.session_state:
                for res in st.session_state.current_results:
                    with st.expander(f"{res['score']}% Match - {res['title']}"):
                        st.write(f"[Read Article]({res['link']})")

    st.divider()
    st.header("📉 Impact Analysis & Context (City/Area Level)")
    
    bls_state = selected_states[0] if selected_states else "CA"
    county_map = load_county_fips()
    
    selected_row = filtered_df[filtered_df['company'] == to_investigate].iloc[0]
    comp_area = selected_row.get('location', 'Unknown') # Now treating this as the whole city/area
    comp_jobs = selected_row.get('jobs', 0)
    
    area_code = get_area_code(bls_state)
    if county_map and (bls_state, comp_area) in county_map:
        fips = county_map[(bls_state, comp_area)]
        area_code = get_area_code(bls_state, fips)

    rate_sid = laus_series(area_code, "03")
    emp_sid = laus_series(area_code, "05")

    try:
        current_year = dt.date.today().year
        resp = bls_fetch([rate_sid, emp_sid], current_year - 2, current_year)
        bls_df = parse_bls_to_df(resp)

        if not bls_df.empty:
            df_rate = bls_df[bls_df['seriesID'] == rate_sid]
            df_emp = bls_df[bls_df['seriesID'] == emp_sid]

            latest_rate = df_rate.iloc[-1]['value'] if not df_rate.empty else 0
            latest_emp = df_emp.iloc[-1]['value'] if not df_emp.empty else 1
            
            target_date = df_rate.iloc[-1]['date'] - pd.DateOffset(years=1)
            prev_rate_row = df_rate[df_rate['date'] == target_date]
            old_rate = prev_rate_row.iloc[0]['value'] if not prev_rate_row.empty else latest_rate

            impact_pct = (comp_jobs / latest_emp) * 100
            trend = "increased" if latest_rate > old_rate else "decreased" if latest_rate < old_rate else "stagnated"

            st.subheader(f"Analysis for {to_investigate} in {comp_area} Area")
            
            c1, c2 = st.columns(2)
            with c1:
                st.metric(f"Total {comp_area} Employment", f"{int(latest_emp):,}")
                st.write(f"The layoffs account for approximately **{impact_pct:.4f}%** of the **entire area's** employment.")
            with c2:
                st.metric("Area Trend", f"{latest_rate}% Rate")
                st.write(f"Unemployment in the **{comp_area} area** has **{trend}** from {old_rate}% year-over-year.")

            # Visualization with requested clear axis labels
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(df_rate['date'], df_rate['value'], marker='o', color='#E63946', linewidth=2)
            ax.set_title(f"Unemployment Rate: Entire {comp_area} Area", fontsize=14)
            ax.set_xlabel("Timeline (Month/Year)", fontsize=12)
            ax.set_ylabel("Unemployment Rate (%)", fontsize=12)
            ax.grid(True, linestyle='--', alpha=0.7)
            plt.xticks(rotation=45)
            st.pyplot(fig)
            
        else:
            st.warning(f"Could not aggregate data for the entire city of {comp_area}. Please check FIPS mapping.")
    except Exception as e:
        st.error(f"BLS Fetch Error: {e}")

st.caption("Data sources: BLS Public API v2 (LAUS), Serper News.")