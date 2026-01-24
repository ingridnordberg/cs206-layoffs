import streamlit as st
import pandas as pd
import requests
import json
import re
import time
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from dateutil.parser import parse as parse_date

# --- CONFIG ---
st.set_page_config(page_title="Enriched News Alerts", page_icon="🗞️", layout="wide")

# Initialize session state to store "Outlets Found" across searches
if 'outlets_cache' not in st.session_state:
    st.session_state.outlets_cache = {}

# --- SCRAPER LOGIC (From friend's coverage_finder.py) ---
def is_blocked_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host.endswith(s) for s in (".gov", ".mil"))

def fetch_article(url: str) -> dict:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        # Extract Date
        pub_date = None
        for attr in ["article:published_time", "og:published_time", "pubdate"]:
            meta = soup.find("meta", attrs={"property": attr}) or soup.find("meta", attrs={"name": attr})
            if meta:
                pub_date = meta.get("content", "")[:10]
                break
        return {"text": soup.get_text()[:10000], "date": pub_date}
    except: return {"text": "", "date": None}

def run_investigation(row, api_key):
    company = row['company']
    location = row['location']
    query = f'"{company}" layoffs {location} -site:.gov'
    
    r = requests.post("https://google.serper.dev/news", 
                      headers={'X-API-KEY': api_key}, 
                      json={"q": query})
    
    hits = r.json().get('news', [])
    scored_results = []
    outlets = []

    for hit in hits[:5]:
        url = hit.get('link')
        if not url or is_blocked_url(url): continue
        
        article = fetch_article(url)
        # Score Logic
        score = fuzz.partial_ratio(company.lower(), article['text'].lower())
        
        if score > 60: # Threshold for a "Match"
            outlets.append(hit.get('source', 'Unknown'))
            scored_results.append({
                "source": hit.get('source'),
                "title": hit.get('title'),
                "link": url,
                "score": score
            })
    
    # Save to cache to show in the main table
    st.session_state.outlets_cache[company] = ", ".join(list(set(outlets))) if outlets else "No coverage found"
    return scored_results

# --- DATA PROCESSING ---
def load_and_clean(file):
    df = pd.read_csv(file)
    df = df[df['is_superseded'] == False].copy()
    df['jobs'] = pd.to_numeric(df['jobs'], errors='coerce').fillna(0).astype(int)
    # Add a column for news outlets found
    df['News Coverage'] = df['company'].map(st.session_state.outlets_cache).fillna("Not Analyzed")
    return df

# --- UI LAYOUT ---
st.title("🕵️‍♂️ WARN Investigator")
st.markdown("Select a row to fetch background context and news coverage.")

uploaded_file = st.file_uploader("Upload integrated.csv", type="csv")

if uploaded_file:
    df = load_and_clean(uploaded_file)
    
    # 1. THE MAIN TABLE
    st.subheader("All Notices")
    st.dataframe(df[['notice_date', 'company', 'location', 'jobs', 'News Coverage']], 
                 use_container_width=True, height=400)

    st.divider()

    # 2. THE INVESTIGATION PANEL
    col1, col2 = st.columns([1, 2])

    with col1:
        st.write("### Investigation Console")
        to_investigate = st.selectbox("Pick a company to analyze:", df['company'].unique())
        
        if st.button("🚀 Start Investigative Search"):
            api_key = "57bb99cacfc8c06c15a4a046b909c95a6dd06248"
            if not api_key:
                st.error("Add your key to secrets.toml")
            else:
                selected_row = df[df['company'] == to_investigate].iloc[0]
                results = run_investigation(selected_row, api_key)
                st.session_state.current_results = results
                st.rerun() # Refresh to show news in the table

    with col2:
        # 3. DYNAMIC TITLE WITH OUTLET NAMES
        outlets_found = st.session_state.outlets_cache.get(to_investigate, "")
        if outlets_found and outlets_found != "No coverage found":
            st.success(f"### Reported by: {outlets_found}")
        elif outlets_found == "No coverage found":
            st.warning("### No News Coverage Detected")
        else:
            st.write("### Search Results will appear here")

        if 'current_results' in st.session_state:
            for res in st.session_state.current_results:
                with st.expander(f"{res['source']}: {res['title']}"):
                    st.write(f"Match Score: {res['score']}%")
                    st.write(f"[Link to Story]({res['link']})")