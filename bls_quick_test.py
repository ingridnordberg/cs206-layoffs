#!/usr/bin/env python3

import argparse
import datetime as dt
import os
import sys
import requests

# ================================
# CONFIG
# ================================
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

STATE_ABBR_TO_FIPS = {
    "AL":"01","AK":"02","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10","DC":"11",
    "FL":"12","GA":"13","HI":"15","ID":"16","IL":"17","IN":"18","IA":"19","KS":"20","KY":"21",
    "LA":"22","ME":"23","MD":"24","MA":"25","MI":"26","MN":"27","MS":"28","MO":"29","MT":"30",
    "NE":"31","NV":"32","NH":"33","NJ":"34","NM":"35","NY":"36","NC":"37","ND":"38","OH":"39",
    "OK":"40","OR":"41","PA":"42","RI":"44","SC":"45","SD":"46","TN":"47","TX":"48","UT":"49",
    "VT":"50","VA":"51","WA":"53","WV":"54","WI":"55","WY":"56","PR":"72"
}

# ================================
# SERIES ID BUILDERS
# ================================
def laus_state_unemp_rate_series_id(state_abbr: str, seasonal: str = "S") -> str:
    """
    LAUS statewide unemployment rate
    seasonal: S = seasonally adjusted, U = not seasonally adjusted

    Format:
      LA + seasonal + area(15) + measure(03)

    Area code (statewide):
      ST + stateFIPS + 11 zeros
    """
    fips = STATE_ABBR_TO_FIPS.get(state_abbr)
    if not fips:
        raise ValueError(f"Unknown state abbreviation: {state_abbr}")
    area = f"ST{fips}{'0'*11}"
    return f"LA{seasonal}{area}03"


def smu_state_total_nonfarm_emp_series_id(state_abbr: str, seasonal: str = "S") -> str:
    """
    SMU statewide total nonfarm employment (all employees, thousands)

    Format:
      SM + seasonal + state(2) + area(5) + industry(8) + datatype(01)

    Statewide area: 00000
    Industry (total nonfarm): 00000000
    Datatype: 01
    """
    fips = STATE_ABBR_TO_FIPS.get(state_abbr)
    if not fips:
        raise ValueError(f"Unknown state abbreviation: {state_abbr}")
    return f"SM{seasonal}{fips}00000{'0'*8}01"

# ================================
# BLS API
# ================================
def bls_fetch(series_ids, startyear, endyear, api_key):
    payload = {
        "seriesid": series_ids,
        "startyear": str(startyear),
        "endyear": str(endyear),
        "registrationKey": api_key,
    }
    r = requests.post(BLS_URL, json=payload, timeout=25)
    r.raise_for_status()
    return r.json()


def parse_monthly_series(resp_json):
    """
    Returns:
      dict: seriesID -> {(year, month): value}
    """
    out = {}
    for series in resp_json.get("Results", {}).get("series", []):
        sid = series.get("seriesID")
        data = {}
        for row in series.get("data", []):
            period = row.get("period", "")
            if not period.startswith("M"):
                continue
            try:
                year = int(row["year"])
                month = int(period[1:])
                val = float(row["value"])
            except Exception:
                continue
            data[(year, month)] = val
        out[sid] = data
    return out


def latest_at_or_before(data_dict, year, month):
    """
    Returns (y, m, value) for the most recent date <= (year, month)
    """
    keys = sorted(data_dict.keys())
    best = None
    for k in keys:
        if k <= (year, month):
            best = k
        else:
            break
    if best is None:
        return None
    return best[0], best[1], data_dict[best]


def fmt(x, suffix=""):
    if x is None:
        return "N/A"
    return f"{x:.2f}{suffix}"

# ================================
# MAIN
# ================================
def main():
    ap = argparse.ArgumentParser(
        description="Quick BLS CLI tester (state unemployment + total nonfarm employment)"
    )
    ap.add_argument("--state", required=True, help="State abbreviation (e.g. CA, NY, SC)")
    ap.add_argument("--date", required=True, help="Date YYYY-MM-DD (month used)")
    ap.add_argument("--no-laus", action="store_true", help="Skip unemployment rate")
    ap.add_argument("--no-smu", action="store_true", help="Skip total nonfarm employment")
    args = ap.parse_args()

    state = args.state.upper().strip()
    try:
        d = dt.date.fromisoformat(args.date)
    except ValueError:
        print("Error: --date must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(2)

    api_key = "1451ccabe4de49a4af9119039e91376e"

    year, month = d.year, d.month
    startyear, endyear = year - 2, year

    series_ids = []
    labels = {}

    if not args.no_laus:
        sid = laus_state_unemp_rate_series_id(state, seasonal="S")
        series_ids.append(sid)
        labels[sid] = "Local Area unemployment rate (%)"

    if not args.no_smu:
        sid = smu_state_total_nonfarm_emp_series_id(state, seasonal="S")
        series_ids.append(sid)
        labels[sid] = "State & Metro Area total nonfarm employment (thousands)"

    if not series_ids:
        print("Nothing to fetch (both --no-laus and --no-smu set)", file=sys.stderr)
        sys.exit(2)

    resp = bls_fetch(series_ids, startyear, endyear, api_key)

    if resp.get("status") != "REQUEST_SUCCEEDED":
        print("BLS API error:", resp, file=sys.stderr)
        sys.exit(1)

    series_map = parse_monthly_series(resp)

    print(f"\nState: {state} | Target month: {year}-{month:02d}")
    print("-" * 50)

    for sid in series_ids:
        data = series_map.get(sid, {})
        got = latest_at_or_before(data, year, month)

        print(labels.get(sid, sid))
        print(f"  seriesID: {sid}")

        if got is None:
            print("  value:    N/A (no data returned)")
            print("  YoY:      N/A\n")
            continue

        y2, m2, cur = got
        prev = data.get((y2 - 1, m2))

        print(f"  value:    {fmt(cur)}  (used {y2}-{m2:02d})")

        if prev is None:
            print("  YoY:      N/A\n")
        else:
            if "unemployment" in labels[sid].lower():
                print(f"  YoY:      {fmt(cur - prev, ' pp')}\n")
            else:
                if prev == 0:
                    print("  YoY:      N/A\n")
                else:
                    print(f"  YoY:      {fmt((cur - prev) / prev * 100.0, '%')}\n")


if __name__ == "__main__":
    main()
