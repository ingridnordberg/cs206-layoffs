import streamlit as st
import pandas as pd
import requests
import json
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

# --- CONFIG ---
st.set_page_config(page_title="Enriched News Alerts", page_icon="🕵️", layout="wide")

# --- INITIALIZATION ---
if 'website_cache' not in st.session_state:
    st.session_state.website_cache = {}
if 'outlets_cache' not in st.session_state:
    st.session_state.outlets_cache = {}
if 'current_results' not in st.session_state:
    st.session_state.current_results = []

# --- REINFORCED WEBSITE FINDER ---
def find_company_website(company, location, api_key):
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', company).lower()
    query = f'"{company}" official corporate homepage'
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "num": 10})
    headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
    
    BLACKLIST = ['portfolio', 'investor', 'mppgrp', 'bbb.org', 'thelayoff', 'wikipedia', 
                 'linkedin', 'facebook', 'yelp', 'yellowpages', 'dandb.com', 'zoominfo', 
                 'glassdoor', 'indeed', 'blade', 'news', 'pressrelease', '.gov']

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
        return None
    except: return None

# --- INDUSTRY GUESSER ---
def guess_industry(company_name):
    name = str(company_name).lower()
    mapping = {
        'AgTech & Farming': ['farming', 'agri', 'farm', 'aero', 'vertical', 'greenhouse'],
        'Food & Beverage': ['baking', 'bakery', 'food', 'bread'],
        'Tech': ['space', 'systems', 'tech', 'software', 'data'],
        'Manufacturing': ['mfg', 'factory', 'industrial', 'steel', 'parts']
    }
    for industry, keywords in mapping.items():
        if any(word in name for word in keywords):
            return industry
    return "General Business"

# --- INVESTIGATION LOGIC ---
def fetch_article(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        return BeautifulSoup(r.text, "html.parser").get_text()[:5000]
    except: return ""

def run_investigation(row, api_key):
    company, location = row['company'], row['location']
    website = find_company_website(company, location, api_key)
    st.session_state.website_cache[company] = website

    query = f'"{company}" layoffs {location} -site:.gov'
    r = requests.post("https://google.serper.dev/news", headers={'X-API-KEY': api_key}, json={"q": query})
    hits = r.json().get('news', [])
    
    scored_results = []
    outlets = []
    for hit in hits[:5]:
        text = fetch_article(hit['link'])
        score = fuzz.partial_ratio(company.lower(), text.lower())
        if score > 60:
            outlets.append(hit.get('source', 'Unknown'))
            scored_results.append({"source": hit.get('source'), "title": hit.get('title'), "link": hit['link'], "score": score})
    
    st.session_state.outlets_cache[company] = ", ".join(list(set(outlets))) if outlets else "No news found"
    return scored_results

# --- UI MAIN ---
st.title("🕵️‍♂️ Enriched WARN Investigator")
uploaded_file = st.file_uploader("Upload integrated.csv", type="csv")

if uploaded_file:
    # 1. Load Data
    df_raw = pd.read_csv(uploaded_file)
    df_active = df_raw[df_raw['is_superseded'] == False].copy()
    df_active['notice_date'] = pd.to_datetime(df_active['notice_date'], errors='coerce')
    df_active['Industry'] = df_active['company'].apply(guess_industry)
    
    # --- SIDEBAR FILTERS ---
    st.sidebar.header("Filter Options")
    
    # State Filter
    unique_states = sorted(df_active['postal_code'].dropna().unique())
    selected_states = st.sidebar.multiselect("Select States:", unique_states, default=["NY", "CA", "TX"] if "NY" in unique_states else unique_states[:3])
    
    # Date Range Filter
    min_date = df_active['notice_date'].min().to_pydatetime()
    max_date = df_active['notice_date'].max().to_pydatetime()
    date_range = st.sidebar.slider("Select Date Range:", min_date, max_date, (pd.Timestamp('2023-01-01').to_pydatetime(), max_date))
    
    # Text Search Filter
    search_query = st.sidebar.text_input("Search Company Name:")

    # --- APPLY FILTERS TO DATASET ---
    filtered_df = df_active.copy()
    
    if selected_states:
        filtered_df = filtered_df[filtered_df['postal_code'].isin(selected_states)]
    
    filtered_df = filtered_df[
        (filtered_df['notice_date'] >= pd.Timestamp(date_range[0])) & 
        (filtered_df['notice_date'] <= pd.Timestamp(date_range[1]))
    ]
    
    if search_query:
        filtered_df = filtered_df[filtered_df['company'].str.contains(search_query, case=False, na=False)]

    # --- SPREADSHEET (Now synchronized with visualization) ---
    st.subheader(f"📊 Filtered Data Records ({len(filtered_df)} entries found)")
    # We display the full filtered dataframe here
    st.dataframe(filtered_df[['notice_date', 'company', 'Industry', 'location', 'jobs']], use_container_width=True)

    # --- VISUALIZATION (Now synchronized with spreadsheet) ---
    st.divider()
    st.subheader("📈 Layoff Trends (Filtered View)")
    
    chart_data = filtered_df.dropna(subset=['notice_date', 'jobs'])
    if not chart_data.empty:
        # Resample to month and sum jobs
        monthly_trend = chart_data.set_index('notice_date').resample('MS')['jobs'].sum().reset_index()
        st.area_chart(data=monthly_trend, x='notice_date', y='jobs', color="#ff4b4b")
    else:
        st.warning("Not enough data to generate a trend for this selection.")

    # --- INVESTIGATION SECTION ---
    st.divider()
    col1, col2 = st.columns([1, 2])

    with col1:
        # Dropdown only shows companies that pass the current filters
        company_list = sorted(filtered_df['company'].dropna().unique())
        to_investigate = st.selectbox("Investigate a Company from this view:", company_list)
        
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