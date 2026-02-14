import os
import streamlit as st
import pandas as pd
import requests
import json
import re
import datetime as dt
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
import matplotlib.pyplot as plt

# --- CONFIG ---
st.set_page_config(page_title="Enriched News Alerts & Economic Data", page_icon="🕵️", layout="wide")

# --- BLS CONSTANTS ---
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
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

# --- LOGIC FUNCTIONS: INVESTIGATION ---
def find_company_website(company, location, api_key):
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', company).lower()
    query = f'"{company}" official corporate homepage'
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "num": 10})
    headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
    BLACKLIST = ['portfolio', 'investor', 'bbb.org', 'wikipedia', 'linkedin', 'facebook', 'zoominfo', 'glassdoor']
    try:
        response = requests.post(url, headers=headers, data=payload)
        results = response.json().get('organic', [])
        candidates = []
        for hit in results:
            link = hit.get('link', '').lower()
            parsed = urlparse(link)
            domain = parsed.netloc.lower()
            if any(b in domain for b in BLACKLIST): continue
            score = len(parsed.path)
            if clean_name in domain: score -= 200 
            candidates.append((score, hit.get('link')))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
    except: pass
    return None

def guess_industry(company_name):
    name = str(company_name).lower()
    mapping = {
        'AgTech & Farming': ['farming', 'agri', 'farm', 'aero', 'vertical', 'greenhouse'],
        'Food & Beverage': ['baking', 'bakery', 'food', 'bread', 'meat', 'dairy'],
        'Tech': ['space', 'systems', 'tech', 'software', 'data', 'digital'],
        'Manufacturing': ['mfg', 'factory', 'industrial', 'steel', 'parts', 'machining']
    }
    for industry, keywords in mapping.items():
        if any(word in name for word in keywords): return industry
    return "General Business"

def fetch_article(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        return BeautifulSoup(r.text, "html.parser").get_text()[:5000]
    except: return ""

def run_investigation(row, api_key):
    company, location = row['company'], row['location']
    st.session_state.website_cache[company] = find_company_website(company, location, api_key)
    query = f'"{company}" layoffs {location} -site:.gov'
    r = requests.post("https://google.serper.dev/news", headers={'X-API-KEY': api_key}, json={"q": query})
    hits = r.json().get('news', [])
    scored_results, outlets = [], []
    for hit in hits[:5]:
        text = fetch_article(hit['link'])
        score = fuzz.partial_ratio(company.lower(), text.lower())
        if score > 60:
            outlets.append(hit.get('source', 'Unknown'))
            scored_results.append({"source": hit.get('source'), "title": hit.get('title'), "link": hit['link'], "score": score})
    st.session_state.outlets_cache[company] = ", ".join(list(set(outlets))) if outlets else "No news found"
    return scored_results

# --- LOGIC FUNCTIONS: BLS ECONOMIC DATA ---
def laus_state_unemp_rate_series_id(state_abbr: str, seasonal: str = "S") -> str:
    fips = STATE_ABBR_TO_FIPS.get(state_abbr)
    if not fips: raise ValueError(f"Unknown state: {state_abbr}")
    return f"LA{seasonal}ST{fips}{'0'*11}03"

def bls_fetch(series_id: str, startyear: int, endyear: int, api_key: str | None):
    payload = {"seriesid": [series_id], "startyear": str(startyear), "endyear": str(endyear)}
    if api_key: payload["registrationKey"] = api_key
    r = requests.post(BLS_URL, json=payload, timeout=25)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "REQUEST_SUCCEEDED": raise RuntimeError(f"BLS request failed: {j}")
    return j

def parse_series_to_df(resp_json) -> pd.DataFrame:
    series_list = resp_json.get("Results", {}).get("series", [])
    if not series_list: return pd.DataFrame(columns=["date", "value"])
    rows = []
    for row in series_list[0].get("data", []):
        period = row.get("period", "")
        if not period.startswith("M"): continue
        try:
            rows.append((dt.date(int(row["year"]), int(period[1:]), 1), float(row["value"])))
        except: continue
    df = pd.DataFrame(rows, columns=["date", "value"]).sort_values("date")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(show_spinner=False)
def get_last_5y_state_unemp(state_abbr: str, api_key: str | None, seasonal: str) -> tuple[str, pd.DataFrame]:
    today = dt.date.today()
    endyear, startyear = today.year, today.year - 6
    sid = laus_state_unemp_rate_series_id(state_abbr, seasonal=seasonal)
    resp = bls_fetch(sid, startyear=startyear, endyear=endyear, api_key=api_key)
    df = parse_series_to_df(resp)
    cutoff = pd.Timestamp(today.replace(year=today.year - 5, day=1)).normalize()
    return sid, df[df["date"] >= cutoff].reset_index(drop=True)

def fmt(x):
    return "N/A" if x is None or pd.isna(x) else f"{x:.2f}"

# --- UI MAIN ---
st.title("🕵️‍♂️ Enriched WARN Investigator")
uploaded_file = st.file_uploader("Upload integrated.csv", type="csv")

if uploaded_file:
    # 1. LOAD & GLOBAL FILTERS
    df_raw = pd.read_csv(uploaded_file)
    df_active = df_raw[df_raw['is_superseded'] == False].copy()
    df_active['notice_date'] = pd.to_datetime(df_active['notice_date'], errors='coerce')
    df_active['Industry'] = df_active['company'].apply(guess_industry)
    
    st.sidebar.header("Investigation Filters")
    unique_states = sorted(df_active['postal_code'].dropna().unique())
    selected_states = st.sidebar.multiselect("Select States:", unique_states, default=["NY", "CA"] if "NY" in unique_states else unique_states[:1])
    
    date_range = st.sidebar.slider("Notice Date Range:", 
                                   df_active['notice_date'].min().to_pydatetime(), 
                                   df_active['notice_date'].max().to_pydatetime(), 
                                   (pd.Timestamp('2023-01-01').to_pydatetime(), df_active['notice_date'].max().to_pydatetime()))
    
    search_query = st.sidebar.text_input("Search Company Name:")

    # 2. APPLY FILTERS
    filtered_df = df_active.copy()
    if selected_states: filtered_df = filtered_df[filtered_df['postal_code'].isin(selected_states)]
    filtered_df = filtered_df[(filtered_df['notice_date'] >= pd.Timestamp(date_range[0])) & (filtered_df['notice_date'] <= pd.Timestamp(date_range[1]))]
    if search_query: filtered_df = filtered_df[filtered_df['company'].str.contains(search_query, case=False, na=False)]

    # 3. SPREADSHEET & TRENDS
    st.subheader(f"📊 Filtered Layoff Records ({len(filtered_df)} entries)")
    st.dataframe(filtered_df[['notice_date', 'company', 'Industry', 'location', 'jobs']], use_container_width=True)

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

    # 5. NEW: BLS ECONOMIC CONTEXT SECTION (Appears Underneath)
    st.divider()
    st.header("📉 Macroeconomic Context (BLS Data)")
    st.caption("View general economic trends for the selected region.")

    # Get state for BLS from sidebar; default to first selected or CA
    bls_state = selected_states[0] if selected_states else "CA"
    
    st.sidebar.divider()
    st.sidebar.header("BLS API Settings")
    seasonal_choice = st.sidebar.selectbox("BLS Seasonal adjustment", options=["S (Seasonally Adjusted)", "U (Not Adjusted)"], index=0)
    bls_api_key = st.sidebar.text_input("BLS API Key (Optional)", value=os.environ.get("BLS_API_KEY", ""), type="password")

    try:
        series_id, bls_df = get_last_5y_state_unemp(bls_state, bls_api_key.strip() or None, seasonal_choice[0])

        if not bls_df.empty:
            latest, start = bls_df.iloc[-1]["value"], bls_df.iloc[0]["value"]
            
            m1, m2, m3 = st.columns(3)
            m1.metric(f"Latest Unemp. Rate ({bls_state})", f"{latest}%")
            m2.metric("5-Year Change", f"{fmt(latest - start)} pp")
            m3.metric("BLS Series ID", series_id)

            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(bls_df["date"], bls_df["value"], color="#1f77b4", linewidth=2)
            ax.set_title(f"Historical Unemployment Rate: {bls_state} (Last 5 Years)")
            ax.set_ylabel("Percentage (%)")
            ax.grid(True, alpha=0.3)
            st.pyplot(fig)
            
            with st.expander("View Raw BLS Data Table"):
                st.dataframe(bls_df.rename(columns={"value": "unemployment_rate_pct"}), use_container_width=True)
        else:
            st.warning("No economic data available for the selected state.")
    except Exception as e:
        st.error(f"BLS Error: {e}")