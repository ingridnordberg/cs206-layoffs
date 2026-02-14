import os
import datetime as dt
import requests
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

STATE_ABBR_TO_FIPS = {
    "AL":"01","AK":"02","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10","DC":"11",
    "FL":"12","GA":"13","HI":"15","ID":"16","IL":"17","IN":"18","IA":"19","KS":"20","KY":"21",
    "LA":"22","ME":"23","MD":"24","MA":"25","MI":"26","MN":"27","MS":"28","MO":"29","MT":"30",
    "NE":"31","NV":"32","NH":"33","NJ":"34","NM":"35","NY":"36","NC":"37","ND":"38","OH":"39",
    "OK":"40","OR":"41","PA":"42","RI":"44","SC":"45","SD":"46","TN":"47","TX":"48","UT":"49",
    "VT":"50","VA":"51","WA":"53","WV":"54","WI":"55","WY":"56","PR":"72"
}

def laus_state_unemp_rate_series_id(state_abbr: str, seasonal: str = "S") -> str:
    """
    LAUS statewide unemployment rate:
      LA + seasonal + area(15) + measure(03)
    Area code statewide:
      ST + stateFIPS + 11 zeros  (length 15)
    """
    fips = STATE_ABBR_TO_FIPS.get(state_abbr)
    if not fips:
        raise ValueError(f"Unknown state abbreviation: {state_abbr}")
    area = f"ST{fips}{'0'*11}"
    return f"LA{seasonal}{area}03"

def bls_fetch(series_id: str, startyear: int, endyear: int, api_key: str | None):
    payload = {
        "seriesid": [series_id],
        "startyear": str(startyear),
        "endyear": str(endyear),
    }
    if api_key:
        payload["registrationKey"] = api_key

    r = requests.post(BLS_URL, json=payload, timeout=25)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS request failed: {j}")
    return j

def parse_series_to_df(resp_json) -> pd.DataFrame:
    """Return DataFrame with columns: date (datetime), value (float)."""
    series_list = resp_json.get("Results", {}).get("series", [])
    if not series_list:
        return pd.DataFrame(columns=["date", "value"])

    data = series_list[0].get("data", [])
    rows = []
    for row in data:
        period = row.get("period", "")
        if not period.startswith("M"):
            continue
        try:
            year = int(row["year"])
            month = int(period[1:])
            value = float(row["value"])
        except Exception:
            continue
        rows.append((dt.date(year, month, 1), value))

    df = pd.DataFrame(rows, columns=["date", "value"]).sort_values("date")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(show_spinner=False)
def get_last_5y_state_unemp(state_abbr: str, api_key: str | None, seasonal: str) -> tuple[str, pd.DataFrame]:
    today = dt.date.today()
    endyear = today.year
    startyear = endyear - 6  # grab extra year so "last 5y" is always covered + smoother
    sid = laus_state_unemp_rate_series_id(state_abbr, seasonal=seasonal)

    resp = bls_fetch(sid, startyear=startyear, endyear=endyear, api_key=api_key)
    df = parse_series_to_df(resp)

    # Keep last 60-ish months
    cutoff = pd.Timestamp(today.replace(year=today.year - 5, day=1)).normalize()
    df = df[df["date"] >= cutoff].reset_index(drop=True)

    return sid, df

def fmt(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "N/A"
    return f"{x:.2f}"

# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="BLS Unemployment (5y)", layout="wide")

st.title("📈 Unemployment Rate by State (Last 5 Years)")
st.caption("Source: BLS LAUS (monthly).")

colA, colB, colC = st.columns([1, 1, 2])

with colA:
    state = st.selectbox("State", options=sorted(STATE_ABBR_TO_FIPS.keys()), index=sorted(STATE_ABBR_TO_FIPS.keys()).index("CA"))

with colB:
    seasonal = st.selectbox("Seasonal adjustment", options=["S (seasonally adjusted)", "U (not seasonally adjusted)"], index=0)
    seasonal_code = seasonal.split()[0]  # "S" or "U"

with colC:
    api_key = st.text_input("BLS API key (optional)", value=os.environ.get("BLS_API_KEY", ""), type="password")
    api_key = api_key.strip() or None
    st.write("Tip: set `BLS_API_KEY` as an env var so you don't paste it in the UI.")

try:
    series_id, df = get_last_5y_state_unemp(state, api_key, seasonal_code)

    if df.empty:
        st.warning("No data returned for that selection.")
        st.stop()

    latest = df.iloc[-1]["value"]
    start = df.iloc[0]["value"]
    delta_pp = latest - start

    m1, m2, m3 = st.columns(3)
    m1.metric("Latest unemployment rate", f"{fmt(latest)}%")
    m2.metric("5-year change", f"{fmt(delta_pp)} pp")
    m3.metric("Series ID", series_id)

    fig = plt.figure(figsize=(8, 4))  # width, height in inches
    plt.plot(df["date"], df["value"])
    plt.xlabel("Month")
    plt.ylabel("Unemployment rate (%)")
    plt.title(f"{state} unemployment rate (last 5 years)")
    plt.xticks(rotation=30, ha="right")
    st.pyplot(fig, clear_figure=True)

    with st.expander("Show data table"):
        st.dataframe(df.rename(columns={"value": "unemployment_rate_pct"}), use_container_width=True)

except Exception as e:
    st.error(f"Error: {e}")
