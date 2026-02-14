import argparse
import datetime as dt
import csv
import requests

# ================================
# CONFIG
# ================================
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
API_KEY = "1451ccabe4de49a4af9119039e91376e"

STATE_ABBR_TO_FIPS = {
    "AL":"01","AK":"02","AZ":"04","AR":"05","CA":"06","CO":"08","CT":"09","DE":"10","DC":"11",
    "FL":"12","GA":"13","HI":"15","ID":"16","IL":"17","IN":"18","IA":"19","KS":"20","KY":"21",
    "LA":"22","ME":"23","MD":"24","MA":"25","MI":"26","MN":"27","MS":"28","MO":"29","MT":"30",
    "NE":"31","NV":"32","NH":"33","NJ":"34","NM":"35","NY":"36","NC":"37","ND":"38","OH":"39",
    "OK":"40","OR":"41","PA":"42","RI":"44","SC":"45","SD":"46","TN":"47","TX":"48","UT":"49",
    "VT":"50","VA":"51","WA":"53","WV":"54","WI":"55","WY":"56","PR":"72"
}

# ================================
# COUNTY FIPS LOADER
# ================================
def load_county_fips(path="county_fips.csv"):
    m = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["state_abbr"].upper(), row["county_name"])
            m[key] = row["county_fips"].zfill(5)
    return m


# ================================
# SERIES BUILDERS
# ================================
def laus_state_series(state_abbr):
    fips = STATE_ABBR_TO_FIPS[state_abbr]
    area = f"ST{fips}{'0'*11}"
    return f"LAS{area}03"


def laus_county_series(county_fips5):
    # Counties are typically NOT seasonally adjusted => LAU (not LAS)
    area = f"CN{county_fips5}{'0'*8}"
    return f"LAU{area}03"


# ================================
# BLS FETCH
# ================================
def bls_fetch(series_ids, startyear, endyear):
    payload = {
        "seriesid": series_ids,
        "startyear": str(startyear),
        "endyear": str(endyear),
        "registrationKey": API_KEY,
    }
    r = requests.post(BLS_URL, json=payload, timeout=25)
    return r.json()


def parse_monthly(resp_json):
    out = {}
    for series in resp_json["Results"]["series"]:
        sid = series["seriesID"]
        data = {}
        for row in series["data"]:
            if not row["period"].startswith("M"):
                continue
            year = int(row["year"])
            month = int(row["period"][1:])

            v = row["value"]
            if v == "-" or v == "":
                continue

            val = float(v)
            data[(year, month)] = val
        out[sid] = data
    return out

# def parse_monthly(resp_json):
#     out = {}
#     for series in resp_json["Results"]["series"]:
#         sid = series["seriesID"]
#         data = {}
#         for row in series["data"]:
#             if not row["period"].startswith("M"):
#                 continue
#             year = int(row["year"])
#             month = int(row["period"][1:])
#             val = float(row["value"])
#             data[(year, month)] = val
#         out[sid] = data
#     return out


def latest_at_or_before(data, year, month):
    keys = sorted(data.keys())
    best = None
    for k in keys:
        if k <= (year, month):
            best = k
        else:
            break
    if best is None:
        return None
    return best[0], best[1], data[best]


def fmt(x, suffix=""):
    if x is None:
        return "N/A"
    return f"{x:.2f}{suffix}"


# ================================
# MAIN
# ================================
def main():
    ap = argparse.ArgumentParser(description="BLS local tester")
    ap.add_argument("--state", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--county", help="County name exactly as in county_fips.csv")
    args = ap.parse_args()

    state = args.state.upper()
    d = dt.date.fromisoformat(args.date)
    year, month = d.year, d.month

    startyear = year - 6
    endyear = year

    if args.county:
        county_map = load_county_fips()
        county_fips = county_map[(state, args.county)]
        series_id = laus_county_series(county_fips)
        label = f"County unemployment rate (%) — {args.county}, {state}"
    else:
        series_id = laus_state_series(state)
        label = f"State unemployment rate (%) — {state}"

    resp = bls_fetch([series_id], startyear, endyear)
    series_map = parse_monthly(resp)

    data = series_map.get(series_id, {})
    got = latest_at_or_before(data, year, month)

    print(f"\nTarget month: {year}-{month:02d}")
    print("-" * 50)
    print(label)
    print(f"seriesID: {series_id}")

    if got is None:
        print("value: N/A")
        print("YoY: N/A")
        return

    y2, m2, cur = got
    prev = data.get((y2 - 1, m2))

    print(f"value: {fmt(cur)} (used {y2}-{m2:02d})")

    if prev is None:
        print("YoY: N/A")
    else:
        print(f"YoY: {fmt(cur - prev, ' pp')}")


if __name__ == "__main__":
    main()
