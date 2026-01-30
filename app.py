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

# --- REINFORCED WEBSITE FINDER (v5: Domain Priority) ---
def find_company_website(company, location, api_key):
    # Clean name for matching (e.g., "AeroFarms" -> "aerofarms")
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', company).lower()
    
    query = f'"{company}" official corporate homepage'
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "num": 10})
    headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
    
    # Block portfolio sites, aggregators, and news hubs
    BLACKLIST = [
        'portfolio', 'investor', 'mppgrp', 'bbb.org', 'thelayoff', 'wikipedia', 
        'linkedin', 'facebook', 'yelp', 'yellowpages', 'dandb.com', 'zoominfo', 
        'glassdoor', 'indeed', 'blade', 'news', 'pressrelease', '.gov'
    ]

    try:
        response = requests.post(url, headers=headers, data=payload)
        results = response.json().get('organic', [])
        
        candidates = []
        for hit in results:
            link = hit.get('link', '').lower()
            parsed = urlparse(link)
            domain = parsed.netloc.lower()
            path = parsed.path.lower().strip('/')
            
            # 1. Immediate rejection for blacklisted domains or "portfolio" paths
            if any(b in domain for b in BLACKLIST) or any(b in path for b in BLACKLIST):
                continue
            
            # 2. Strict Intent Check: Skip if the title suggests it's just a news article
            title = hit.get('title', '').lower()
            if any(word in title for word in ['layoff', 'closing', 'shutdown', 'portfolio']):
                continue
            
            # 3. DOMAIN MATCHING BONUS (The AeroFarms Fix)
            # If the clean company name is the main part of the domain, give it a huge boost
            score = len(path) # Shorter paths are better
            if clean_name in domain.replace('www.', ''):
                score -= 200 # Massive boost for exact brand domains
            
            candidates.append((score, hit.get('link')))

        if candidates:
            # Lowest score (highest boost) wins
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
        return None
    except:
        return None

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

# --- NEWS SCRAPER ---
def fetch_article(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        soup = BeautifulSoup(r.text, "html.parser")
        return soup.get_text()[:5000]
    except: return ""

def run_investigation(row, api_key):
    company = row['company']
    location = row['location']
    
    # 1. FIND WEBSITE (Using Domain Priority Logic)
    website = find_company_website(company, location, api_key)
    st.session_state.website_cache[company] = website

    # 2. FIND NEWS
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

# --- UI ---
st.title("🕵️‍♂️ Enriched WARN Investigator")
uploaded_file = st.file_uploader("Upload integrated.csv", type="csv")

if uploaded_file:
    df = pd.read_csv(uploaded_file)
    df = df[df['is_superseded'] == False].copy()
    df['Industry'] = df['company'].apply(guess_industry)
    
    st.dataframe(df[['notice_date', 'company', 'Industry', 'location', 'jobs']].head(20), use_container_width=True)

    st.divider()
    col1, col2 = st.columns([1, 2])

    with col1:
        to_investigate = st.selectbox("Select Company:", df['company'].unique())
        if st.button("🚀 Run Agentic Search"):
            api_key = "57bb99cacfc8c06c15a4a046b909c95a6dd06248"
            selected_row = df[df['company'] == to_investigate].iloc[0]
            st.session_state.current_results = run_investigation(selected_row, api_key)
            st.rerun()

    with col2:
        if to_investigate in st.session_state.website_cache:
            site = st.session_state.website_cache[to_investigate]
            if site:
                st.info(f"🌐 **Official Website:** [{site}]({site})")
            else:
                st.warning("🌐 **Website:** Not Found (Prioritized brand domains)")
            
            outlets = st.session_state.outlets_cache.get(to_investigate, "")
            if outlets and outlets != "No news found":
                st.success(f"### Reported by: {outlets}")

            if 'current_results' in st.session_state:
                for res in st.session_state.current_results:
                    with st.expander(f"{res['score']}% Match - {res['title']}"):
                        st.write(f"[Read Article]({res['link']})")