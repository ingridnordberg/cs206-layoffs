import streamlit as st
import pandas as pd
import re

# 1. Set the Title
st.title("🗞️ Enriched News Alerts: WARN Edition")

# 2. Define the Cleaning Function
def process_warn_data(df):
    # A. Filter out "Superseded" rows
    # This removes old versions of a notice so you only see the most recent update
    if 'is_superseded' in df.columns:
        df = df[df['is_superseded'] == False].copy()
    
    # B. Convert notice_date to a readable date format
    if 'notice_date' in df.columns:
        df['notice_date'] = pd.to_datetime(df['notice_date'], errors='coerce')
    
    # C. Clean company names for searching
    # This creates a "clean_company" column without Inc, LLC, etc., which helps Serper find news
    if 'company' in df.columns:
        regex_pattern = r'\b(Inc\.|LLC|Corp\.|L\.L\.C\.|Incorporated|Limited|Ltd\.)\b'
        df['clean_company'] = (df['company']
                               .fillna('')
                               .str.replace(regex_pattern, '', regex=True, flags=re.IGNORECASE)
                               .str.strip())
    
    # D. Handle Job Counts
    # Your CSV uses the column 'jobs'. We'll make sure it's a number and fill blanks with 0.
    if 'jobs' in df.columns:
        df['jobs'] = pd.to_numeric(df['jobs'], errors='coerce').fillna(0).astype(int)
    
    return df

# 3. File Uploader
uploaded_file = st.file_uploader("Upload your Big Local News WARN CSV", type="csv")

if uploaded_file is not None:
    # Read the raw data
    raw_df = pd.read_csv(uploaded_file)
    
    # Run the cleaning function
    df = process_warn_data(raw_df)
    
    # 4. Show success and a preview of the CLEANED data
    st.success(f"File uploaded and cleaned! Showing {len(df)} active alerts (filtered from {len(raw_df)} total records).")
    
    st.write("### Cleaned WARN Data Preview")
    # We'll display just the most relevant columns to keep it tidy
    cols_to_show = ['notice_date', 'company', 'location', 'jobs', 'postal_code']
    st.dataframe(df[cols_to_show].head(10)) 
    
    # 5. Data Stats
    st.write("### Quick Stats")
    col1, col2 = st.columns(2)
    
    col1.metric("Total Active Notices", len(df))
    
    # Use the 'jobs' column for the sum
    if 'jobs' in df.columns:
        total_jobs = df['jobs'].sum()
        col2.metric("Total Jobs Affected", f"{total_jobs:,}")
    else:
        col2.metric("Total Jobs Affected", "N/A")