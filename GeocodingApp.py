import streamlit as st
import pandas as pd
import requests
import time
import math
import pydeck as pdk

st.set_page_config(page_title="Batch Geocoder", page_icon="🌍", layout="wide")
st.title("🌍 Batch Geocoder")
st.markdown("Upload a file, geocode addresses using Google's Geocoding API, and download the results.")

# Sidebar
st.sidebar.header("Configuration")
api_key = st.sidebar.text_input("Google Geocoding API Key", type="password",
                                 help="Paste your API key here. It is not stored anywhere.")
if not api_key:
    st.sidebar.warning("Please enter your API key to enable geocoding.")

st.sidebar.header("Settings")
delay = st.sidebar.slider("Delay between API calls (seconds)", 0.0, 2.0, 0.1, 0.05)
skip_existing = st.sidebar.checkbox("Skip rows that already have coordinates", value=True)

st.sidebar.header("Cache")
if "_geocode_cache" not in st.session_state:
    st.session_state["_geocode_cache"] = {}
cache_display = st.sidebar.empty()
cache_display.caption(f"**{len(st.session_state['_geocode_cache'])}** addresses cached this session.")
if st.sidebar.button("🗑️ Clear cache"):
    st.session_state["_geocode_cache"] = {}
    cache_display.caption("**0** addresses cached this session.")
    st.sidebar.success("Cache cleared.")
    st.rerun()

CORE_FIELDS = ["AddressID", "StreetAddress", "Latitude", "Longitude"]
LOCATION_FIELDS = ["CityName", "Admin2Name", "Admin1Name", "PostalCode", "CountryCode"]
ALL_FIELDS = CORE_FIELDS + LOCATION_FIELDS
UNMAPPED = "-- Not mapped --"

# Australian postcode -> state mapping for validation
AU_POSTCODE_STATE = {
    "1": "New South Wales", "2": "New South Wales", "3": "Victoria",
    "4": "Queensland", "5": "South Australia", "6": "Western Australia",
    "7": "Tasmania", "0": "Northern Territory",
}
US_STATE_ZIPS = {
    "AL": (35, 36), "AK": (99, 99), "AZ": (85, 86), "AR": (71, 72), "CA": (90, 96),
    "CO": (80, 81), "CT": (6, 6), "DE": (19, 19), "FL": (32, 34), "GA": (30, 31),
    "HI": (96, 96), "ID": (83, 83), "IL": (60, 62), "IN": (46, 47), "IA": (50, 52),
    "KS": (66, 67), "KY": (40, 42), "LA": (70, 71), "ME": (3, 4), "MD": (20, 21),
    "MA": (1, 2), "MI": (48, 49), "MN": (55, 56), "MS": (38, 39), "MO": (63, 65),
    "MT": (59, 59), "NE": (68, 69), "NV": (88, 89), "NH": (3, 3), "NJ": (7, 8),
    "NM": (87, 88), "NY": (10, 14), "NC": (27, 28), "ND": (58, 58), "OH": (43, 45),
    "OK": (73, 74), "OR": (97, 97), "PA": (15, 19), "RI": (2, 2), "SC": (29, 29),
    "SD": (57, 57), "TN": (37, 38), "TX": (75, 79), "UT": (84, 84), "VT": (5, 5),
    "VA": (20, 24), "WA": (98, 99), "WV": (24, 26), "WI": (53, 54), "WY": (82, 83),
}


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def score_result_against_constraints(result, country_code=None, admin1=None, admin2=None, city=None, postal_code=None):
    """Score how well a Google result matches the original constraint data. Higher = better match."""
    score = 0
    components = result.get("address_components", [])

    def get_component(comp_type):
        for c in components:
            if comp_type in c.get("types", []):
                return c.get("long_name", "").lower(), c.get("short_name", "").lower()
        return "", ""

    if country_code:
        _, short = get_component("country")
        if short == country_code.lower():
            score += 10  # Country match is most important

    if admin1:
        long_name, short_name = get_component("administrative_area_level_1")
        a1 = admin1.lower()
        if a1 == long_name or a1 == short_name or long_name in a1 or a1 in long_name:
            score += 5

    if admin2:
        long_name, short_name = get_component("administrative_area_level_2")
        a2 = admin2.lower()
        if a2 == long_name or a2 == short_name or long_name in a2 or a2 in long_name:
            score += 3

    if city:
        long_name, short_name = get_component("locality")
        ct = city.lower()
        if ct == long_name or ct == short_name or long_name in ct or ct in long_name:
            score += 3

    if postal_code:
        long_name, short_name = get_component("postal_code")
        pc = postal_code.lower()
        if pc == long_name or pc == short_name or long_name.startswith(pc) or pc.startswith(long_name):
            score += 4

    return score


def single_geocode_call(address, key, components=None, country_code=None, admin1=None,
                        admin2=None, city=None, postal_code=None):
    """Make one geocoding API call. Pick the best result: first by constraint match, then by precision."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": key}
    if components:
        params["components"] = components
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "UNKNOWN")
        if status == "OK" and data.get("results"):
            scored = []
            for result in data["results"]:
                loc_type = result["geometry"].get("location_type", "UNKNOWN")
                precision = LOCATION_TYPE_RANK.get(loc_type, 0)
                match_score = score_result_against_constraints(
                    result, country_code, admin1, admin2, city, postal_code)
                scored.append((match_score, precision, result))

            # Sort: constraint match first, then precision as tiebreaker
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best = scored[0][2]
            loc = best["geometry"]["location"]
            loc_type = best["geometry"].get("location_type", "UNKNOWN")

            # Flag if we picked a lower-precision result because it matched constraints better
            constraint_picked = False
            if len(scored) > 1:
                top_match, top_prec = scored[0][0], scored[0][1]
                for ms, pr, _ in scored[1:]:
                    if pr > top_prec and ms < top_match:
                        constraint_picked = True
                        break

            return {"lat": loc["lat"], "lng": loc["lng"], "status": "OK",
                    "location_type": loc_type, "constraint_picked": constraint_picked}
        return {"lat": None, "lng": None, "status": status, "location_type": None,
                "constraint_picked": False}
    except requests.Timeout:
        return {"lat": None, "lng": None, "status": "TIMEOUT", "location_type": None,
                "constraint_picked": False}
    except requests.ConnectionError:
        return {"lat": None, "lng": None, "status": "CONNECTION_ERROR", "location_type": None,
                "constraint_picked": False}
    except Exception as e:
        return {"lat": None, "lng": None, "status": "ERROR", "location_type": None,
                "constraint_picked": False}


LOCATION_TYPE_RANK = {"ROOFTOP": 4, "RANGE_INTERPOLATED": 3, "GEOMETRIC_CENTER": 2, "APPROXIMATE": 1}


def geocode_address(full_address, street_address, key, country_code=None, city=None, admin1=None, admin2=None, postal_code=None):
    """Geocode with constraints first using full address. If quality is below street level,
    try with just the street address string, fully unconstrained."""

    api_calls = 0

    # Build full constraint string
    all_comp = []
    if country_code:
        all_comp.append(f"country:{country_code}")
    if postal_code:
        all_comp.append(f"postal_code:{postal_code}")
    if admin1:
        all_comp.append(f"administrative_area_level_1:{admin1}")
    if admin2:
        all_comp.append(f"administrative_area_level_2:{admin2}")
    if city:
        all_comp.append(f"locality:{city}")

    # Good enough = ROOFTOP or RANGE_INTERPOLATED (better than postal code level)
    GOOD_ENOUGH = {"ROOFTOP", "RANGE_INTERPOLATED"}

    candidates = []

    # Constraint kwargs for result scoring
    score_kwargs = {"country_code": country_code, "admin1": admin1, "admin2": admin2,
                    "city": city, "postal_code": postal_code}

    # --- Attempt 1: Full address with all constraints ---
    if all_comp:
        r1 = single_geocode_call(full_address, key, "|".join(all_comp), **score_kwargs)
        api_calls += 1
        if r1["status"] == "OK":
            candidates.append(("constrained", r1))
            if r1.get("location_type") in GOOD_ENOUGH:
                return {**r1, "method": "constrained", "fallback": False,
                        "addr_only_better": False, "detail": "", "api_calls": api_calls}

        # --- Attempt 2: Drop city (only if attempt 1 wasn't good enough) ---
        no_city = [c for c in all_comp if not c.startswith("locality:")]
        if no_city and no_city != all_comp:
            r2 = single_geocode_call(full_address, key, "|".join(no_city), **score_kwargs)
            api_calls += 1
            if r2["status"] == "OK":
                candidates.append(("constrained_no_city", r2))
                if r2.get("location_type") in GOOD_ENOUGH:
                    return {**r2, "method": "constrained_no_city", "fallback": True,
                            "addr_only_better": False,
                            "detail": "Resolved after dropping city constraint.",
                            "api_calls": api_calls}

        # --- Attempt 3: Country only ---
        country_only = [c for c in all_comp if c.startswith("country:")]
        if country_only and country_only != no_city:
            r3 = single_geocode_call(full_address, key, "|".join(country_only), **score_kwargs)
            api_calls += 1
            if r3["status"] == "OK":
                candidates.append(("country_only", r3))
                if r3.get("location_type") in GOOD_ENOUGH:
                    return {**r3, "method": "country_only", "fallback": True,
                            "addr_only_better": False,
                            "detail": "Resolved with country constraint only.",
                            "api_calls": api_calls}

    # --- Still not good enough — try JUST the street address, fully unconstrained ---
    r_addr = single_geocode_call(street_address, key, None, **score_kwargs)
    api_calls += 1
    if r_addr["status"] == "OK":
        candidates.append(("address_only", r_addr))

    if not candidates:
        detail = diagnose_failure(full_address, city, admin1, admin2, postal_code, country_code)
        return {"lat": None, "lng": None, "status": "ZERO_RESULTS",
                "location_type": None, "method": "failed", "fallback": False,
                "addr_only_better": False, "detail": detail, "api_calls": api_calls}

    # Pick the best result across all attempts
    best_method, best = None, None
    best_rank = -1
    for method, r in candidates:
        rank = LOCATION_TYPE_RANK.get(r.get("location_type"), 0)
        if rank > best_rank:
            best_rank = rank
            best = r
            best_method = method

    # Check if address-only was better than the first constrained attempt
    constrained_rank = 0
    for method, r in candidates:
        if method == "constrained":
            constrained_rank = LOCATION_TYPE_RANK.get(r.get("location_type"), 0)
            break
    addr_only_rank = 0
    for method, r in candidates:
        if method == "address_only":
            addr_only_rank = LOCATION_TYPE_RANK.get(r.get("location_type"), 0)
            break
    addr_only_better = addr_only_rank > constrained_rank

    detail_parts = []
    if best_method == "address_only" and all_comp:
        detail_parts.append("Street address alone gave the best result — admin field data is likely incorrect.")
    elif best_method in ("constrained_no_city", "country_only"):
        detail_parts.append(f"Best result from {best_method} — city/admin constraints may have issues.")
    if addr_only_better:
        detail_parts.append("Data quality flag: unconstrained search outperformed constrained search.")
    if best and best.get("constraint_picked"):
        detail_parts.append("A higher-precision result existed but was in the wrong location — chose the constraint-matching result instead. Manual check recommended.")

    return {**best, "method": best_method, "fallback": best_method != "constrained",
            "addr_only_better": addr_only_better, "detail": " ".join(detail_parts),
            "api_calls": api_calls}


def diagnose_failure(address, city, admin1, admin2, postal_code, country_code):
    addr_lower = address.lower() if address else ""
    reasons = []

    military = ["hmas", "raaf", "adf", "barracks", "base", "camp", "garrison", "depot", "armoury", "armory"]
    if any(kw in addr_lower for kw in military):
        reasons.append("Military/defence facility — Google often can't resolve internal base roads. Manual geocoding recommended.")

    state_abbrevs = {"nsw": "new south wales", "vic": "victoria", "qld": "queensland",
                     "sa": "south australia", "wa": "western australia", "tas": "tasmania",
                     "nt": "northern territory", "act": "australian capital territory"}
    addr_parts = addr_lower.replace(",", " ").split()
    embedded_state = None
    for abbr, full in state_abbrevs.items():
        if abbr in addr_parts or full in addr_lower:
            embedded_state = full
            break
    if embedded_state and admin1 and embedded_state != admin1.lower():
        reasons.append(f"Address contains '{embedded_state.title()}' but Admin1 says '{admin1}' — conflicting states.")

    if city:
        city_words = set(city.lower().split())
        addr_words = set(addr_parts)
        if not city_words.intersection(addr_words) and len(address) > 20:
            reasons.append(f"CityName '{city}' doesn't appear in the address — possible mismatch.")

    if city and city.lower().endswith(" city") and country_code and country_code.upper() == "AU":
        reasons.append(f"CityName '{city}' has 'City' suffix — try just '{city.rsplit(' ', 1)[0]}'.")

    if len((address or "").split(",")[0].strip()) < 10:
        reasons.append("Very short street address — may be incomplete.")

    if not reasons:
        reasons.append("Google could not find this address. Check for typos or incomplete details.")
    return " | ".join(reasons)


def guess_column(field_name, available):
    field_lower = field_name.lower()
    for col in available:
        if col.strip().lower() == field_lower:
            return col
    for col in available:
        cl = col.strip().lower()
        if field_lower in cl or cl in field_lower:
            return col
    aliases = {
        "streetaddress": ["street", "address", "addr", "street address"],
        "addressid": ["id", "address_id", "addr_id", "locid", "location_id"],
        "latitude": ["lat", "y"],
        "longitude": ["lng", "lon", "long", "x"],
        "countrycode": ["country", "countryiso", "iso2", "iso2a", "country_code", "cntry", "country code"],
        "cityname": ["city", "town", "locality", "suburb", "city_name"],
        "admin1name": ["admin1", "state", "province", "region", "admin1_name", "statename"],
        "admin2name": ["admin2", "county", "district", "admin2_name", "countyname"],
        "postalcode": ["postcode", "postal", "zip", "zipcode", "postal_code", "zip_code", "pcode", "zip code"],
    }
    for col in available:
        cl = col.strip().lower()
        if cl in aliases.get(field_lower, []):
            return col
    return UNMAPPED


def read_raw_file(uploaded_file):
    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            try:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, header=None, dtype=str, encoding="utf-8")
            except UnicodeDecodeError:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, header=None, dtype=str, encoding="latin-1")
        elif name.endswith((".xlsx", ".xls")):
            uploaded_file.seek(0)
            try:
                df = pd.read_excel(uploaded_file, header=None, dtype=str, engine="openpyxl")
            except Exception:
                uploaded_file.seek(0)
                df = pd.read_excel(uploaded_file, header=None, dtype=str)
        elif name.endswith((".txt", ".tsv")):
            uploaded_file.seek(0)
            sample = uploaded_file.read(8192)
            if isinstance(sample, bytes):
                sample = sample.decode("utf-8", errors="replace")
            uploaded_file.seek(0)
            counts = {"\t": sample.count("\t"), "|": sample.count("|"), ";": sample.count(";")}
            sep = max(counts, key=counts.get) if max(counts.values()) > 0 else ","
            try:
                df = pd.read_csv(uploaded_file, header=None, dtype=str, sep=sep, encoding="utf-8")
            except UnicodeDecodeError:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, header=None, dtype=str, sep=sep, encoding="latin-1")
        else:
            return None, f"Unsupported file type: **{name.split('.')[-1]}**."
        if df is None or df.empty:
            return None, "File contains no data."
        if len(df.columns) < 2:
            return None, "Only one column detected — wrong delimiter?"
        return df, None
    except Exception as e:
        return None, f"Error reading file: {e}"


def apply_header(raw_df, header_row):
    if header_row >= len(raw_df):
        return None, f"Row {header_row} is beyond the end of the file."
    hv = raw_df.iloc[header_row]
    non_blank = hv.dropna()
    non_blank = non_blank[non_blank.astype(str).str.strip() != ""]
    if len(non_blank) == 0:
        return None, f"Row {header_row} is entirely blank."
    df = raw_df.copy()
    col_names = hv.astype(str).str.strip()
    dupes = col_names[col_names.duplicated(keep=False)]
    dupes = dupes[dupes != ""]
    if len(dupes) > 0:
        return None, f"Duplicate column names: **{', '.join(sorted(dupes.unique()))}**."
    blank_cols = (col_names == "") | (col_names == "nan")
    if blank_cols.any() and not blank_cols.all():
        return None, f"Row {header_row} has **{blank_cols.sum()}** blank column name(s)."
    df.columns = col_names
    df = df.iloc[header_row + 1:].reset_index(drop=True)
    if df.empty:
        return None, "No data rows after header."
    return df, None


def validate_dataframe(df, skip):
    errors, warnings, flagged_rows = [], [], {}
    total_rows = len(df)

    blank_addr = df["StreetAddress"].isna() | (df["StreetAddress"].str.strip() == "")
    ba_count = int(blank_addr.sum())
    if ba_count > 0:
        msg = "**Every row** has a blank `StreetAddress`." if ba_count == total_rows else f"**{ba_count}** of {total_rows} rows have blank `StreetAddress`."
        errors.append(msg)
        flagged_rows["Blank StreetAddress"] = df.loc[blank_addr]

    blank_id = df["AddressID"].isna() | (df["AddressID"].astype(str).str.strip() == "")
    bi_count = int(blank_id.sum())
    if bi_count > 0:
        msg = "**Every row** has a blank `AddressID`." if bi_count == total_rows else f"**{bi_count}** of {total_rows} rows have blank `AddressID`."
        errors.append(msg)
        flagged_rows["Blank AddressID"] = df.loc[blank_id]

    id_col = df["AddressID"].astype(str).str.strip()
    non_blank_ids = id_col[~blank_id]
    dup_ids = non_blank_ids[non_blank_ids.duplicated(keep=False)]
    if len(dup_ids) > 0:
        errors.append(f"**{dup_ids.nunique()}** `AddressID`(s) are duplicated across **{len(dup_ids)}** rows.")
        flagged_rows["Duplicate AddressID"] = df.loc[dup_ids.index]
        check = df.loc[dup_ids.index].copy()
        check["_id"] = check["AddressID"].astype(str).str.strip()
        check["_addr"] = check["StreetAddress"].astype(str).str.strip().str.lower()
        conf = check.groupby("_id").filter(lambda g: g["_addr"].nunique() > 1)
        if len(conf) > 0:
            errors.append(f"**{conf['_id'].nunique()}** `AddressID`(s) linked to different addresses.")
            flagged_rows["Conflicting ID/Address"] = df.loc[conf.index]

    lat_raw, lng_raw = df["Latitude"], df["Longitude"]
    lat_num = pd.to_numeric(lat_raw, errors="coerce")
    lng_num = pd.to_numeric(lng_raw, errors="coerce")

    lat_junk = lat_raw.notna() & (lat_raw.astype(str).str.strip() != "") & lat_num.isna()
    lng_junk = lng_raw.notna() & (lng_raw.astype(str).str.strip() != "") & lng_num.isna()
    junk = lat_junk | lng_junk
    if int(junk.sum()) > 0:
        warnings.append(f"**{int(junk.sum())}** row(s) have non-numeric coordinate values.")
        flagged_rows["Non-numeric Coordinates"] = df.loc[junk]

    oor = (lat_num.notna() & ((lat_num < -90) | (lat_num > 90))) | (lng_num.notna() & ((lng_num < -180) | (lng_num > 180)))
    if int(oor.sum()) > 0:
        warnings.append(f"**{int(oor.sum())}** row(s) have out-of-range coordinates.")
        flagged_rows["Out-of-range Coordinates"] = df.loc[oor]

    valid_addr = ~blank_addr
    short = valid_addr & (df["StreetAddress"].str.strip().str.len() < 5)
    if int(short.sum()) > 0:
        warnings.append(f"**{int(short.sum())}** row(s) have very short addresses.")
        flagged_rows["Short Addresses"] = df.loc[short]

    # --- Data quality checks: postcode vs admin1 ---
    if "PostalCode" in df.columns and "Admin1Name" in df.columns and "CountryCode" in df.columns:
        dq_flags = []
        for idx, row in df.iterrows():
            if blank_addr.loc[idx]:
                continue
            pc = str(row.get("PostalCode", "")).strip()
            a1 = str(row.get("Admin1Name", "")).strip()
            cc = str(row.get("CountryCode", "")).strip().upper()

            if cc == "AU" and pc and a1:
                first_digit = pc[0] if pc else ""
                expected_state = AU_POSTCODE_STATE.get(first_digit, "")
                if expected_state and expected_state.lower() != a1.lower():
                    dq_flags.append(idx)
            elif cc == "US" and pc and a1:
                try:
                    zip_prefix = int(pc[:2])
                    for st, (lo, hi) in US_STATE_ZIPS.items():
                        if lo <= zip_prefix <= hi and st.lower() != a1.lower() and a1.lower() not in ["", "nan"]:
                            pass  # Could flag but US state names vs codes makes this complex
                except (ValueError, IndexError):
                    pass

        if dq_flags:
            warnings.append(f"**{len(dq_flags)}** row(s) have a postal code that doesn't match the Admin1 region — possible data quality issue.")
            flagged_rows["Postcode/Admin1 Mismatch"] = df.loc[dq_flags]

    # --- Data quality: address contains location info conflicting with admin fields ---
    state_abbrevs = {"nsw": "new south wales", "vic": "victoria", "qld": "queensland",
                     "sa": "south australia", "wa": "western australia", "tas": "tasmania",
                     "nt": "northern territory", "act": "australian capital territory"}
    conflict_flags = []
    for idx, row in df.iterrows():
        if blank_addr.loc[idx]:
            continue
        addr = str(row["StreetAddress"]).lower()
        a1 = str(row.get("Admin1Name", "")).strip().lower()
        cc = str(row.get("CountryCode", "")).strip().upper()
        if cc == "AU" and a1:
            for abbr, full in state_abbrevs.items():
                if (abbr in addr.split() or full in addr) and full != a1.lower():
                    conflict_flags.append(idx)
                    break
    if conflict_flags:
        warnings.append(f"**{len(conflict_flags)}** row(s) have a state/region in the address that conflicts with Admin1Name.")
        flagged_rows["Address/Admin1 Conflict"] = df.loc[conflict_flags]

    already = int((lat_num.notna() & lng_num.notna()).sum())
    valid_count = int(valid_addr.sum())
    if skip and already == valid_count and valid_count > 0:
        warnings.append("**Every row already has coordinates** and skip is on. Nothing will be geocoded.")

    to_geo = df.loc[valid_addr, "StreetAddress"].nunique()
    if skip:
        needs = valid_addr & (lat_num.isna() | lng_num.isna())
        to_geo = df.loc[needs, "StreetAddress"].nunique()
    if to_geo > 2500:
        warnings.append(f"**{to_geo}** unique addresses. May incur significant API costs.")
    elif to_geo > 500:
        warnings.append(f"**{to_geo}** unique addresses. ~**{int(to_geo * (delay + 0.3) / 60)} min** estimated.")

    return {
        "errors": errors, "warnings": warnings, "flagged_rows": flagged_rows,
        "stats": {"total_rows": total_rows, "unique_addresses": int(df.loc[valid_addr, "StreetAddress"].nunique()),
                  "blank_addresses": ba_count, "already_geocoded": already, "to_geocode": to_geo}
    }


def process_dataframe(df, key, delay_s, skip):
    result = df.copy()
    result["Latitude"] = pd.to_numeric(result["Latitude"], errors="coerce")
    result["Longitude"] = pd.to_numeric(result["Longitude"], errors="coerce")

    has_addr = result["StreetAddress"].notna() & (result["StreetAddress"].str.strip() != "")

    # Build composite address
    addr_parts = ["StreetAddress", "CityName", "Admin2Name", "Admin1Name", "PostalCode"]
    present_parts = [c for c in addr_parts if c in result.columns]

    def build_addr(row):
        p = []
        for c in present_parts:
            v = str(row[c]).strip() if pd.notna(row[c]) else ""
            if v and v.lower() != "nan":
                p.append(v)
        return ", ".join(p)

    result["_full_addr"] = result.apply(build_addr, axis=1)
    st.caption(f"Building address from: {', '.join(present_parts)}")

    def clean_col(col):
        return result[col].fillna("").astype(str).str.strip() if col in result.columns else pd.Series("", index=result.index)

    result["_cc"] = clean_col("CountryCode").str.upper()
    result["_city"] = clean_col("CityName")
    result["_admin1"] = clean_col("Admin1Name")
    result["_admin2"] = clean_col("Admin2Name")
    result["_postal"] = clean_col("PostalCode")

    had_coords = has_addr & result["Latitude"].notna() & result["Longitude"].notna()
    orig_lats = result.loc[had_coords, "Latitude"].copy()
    orig_lngs = result.loc[had_coords, "Longitude"].copy()

    needs = (has_addr & (result["Latitude"].isna() | result["Longitude"].isna())) if skip else has_addr

    # Also need raw street address for address-only fallback
    result["_street_only"] = result["StreetAddress"].fillna("").astype(str).str.strip()

    constraint_cols = ["_full_addr", "_street_only", "_cc", "_city", "_admin1", "_admin2", "_postal"]
    geo_sub = result.loc[needs, constraint_cols].copy()
    geo_sub = geo_sub[geo_sub["_full_addr"].str.strip() != ""]
    combos = geo_sub.drop_duplicates().values.tolist()
    total = len(combos)

    if total == 0:
        st.info("Nothing to geocode.")
        return result.drop(columns=["_full_addr", "_cc", "_city", "_admin1", "_admin2", "_postal"]), None

    sample_addrs = [combo[0] for combo in combos[:5]]
    with st.expander("🔍 Sample addresses being sent to Google"):
        for sa in sample_addrs:
            st.text(sa)

    with st.spinner("Validating API key…"):
        test = single_geocode_call("10 Downing Street, London, UK", key)
        if test["status"] == "REQUEST_DENIED":
            st.error("❌ API key rejected.")
            return result.drop(columns=["_full_addr", "_cc", "_city", "_admin1", "_admin2", "_postal"]), None

    prog = st.progress(0, text="Starting…")
    status_area = st.empty()

    cache, errs, fallbacks = {}, [], []
    geo_cache = st.session_state["_geocode_cache"]
    cache_hits, consec, total_api_calls = 0, 0, 0

    for i, (addr, street, cc, city, admin1, admin2, postal) in enumerate(combos):
        prog.progress((i + 1) / total, text=f"Geocoding {i + 1} of {total}…")
        ck = (addr, street, cc, city, admin1, admin2, postal)

        if ck in geo_cache:
            cache[ck] = geo_cache[ck]
            cache_hits += 1
            geo = geo_cache[ck]
        else:
            geo = geocode_address(addr, street, key,
                                  country_code=cc if cc else None,
                                  city=city if city else None,
                                  admin1=admin1 if admin1 else None,
                                  admin2=admin2 if admin2 else None,
                                  postal_code=postal if postal else None)
            cache[ck] = geo
            geo_cache[ck] = geo
            total_api_calls += geo.get("api_calls", 1)
            if delay_s > 0:
                time.sleep(delay_s)

        if geo["status"] != "OK":
            errs.append({"FullAddress": addr, "StreetAddress": street,
                         "CountryCode": cc or "(none)",
                         "City": city or "(none)", "Admin1": admin1 or "(none)",
                         "Status": geo["status"], "Detail": geo.get("detail", "")})
            consec += 1
            if consec >= 5 and i < total - 1:
                st.warning(f"⚠️ {consec} consecutive failures. Stopping early.")
                break
        else:
            consec = 0
            if geo.get("fallback"):
                fallbacks.append({"FullAddress": addr, "StreetAddress": street,
                                  "City": city or "(none)",
                                  "Admin1": admin1 or "(none)", "Method": geo.get("method", ""),
                                  "Detail": geo.get("detail", "")})

    prog.progress(1.0, text="Done!")

    # Apply results
    result["GoogleLocationType"] = ""
    result["GeoMethod"] = ""
    result["AddrOnlyBetter"] = False

    for (addr, street, cc, city, admin1, admin2, postal), geo in cache.items():
        mask = (result["_full_addr"] == addr) & (result["_street_only"] == street) & \
               (result["_cc"] == cc) & (result["_city"] == city) & \
               (result["_admin1"] == admin1) & (result["_admin2"] == admin2) & \
               (result["_postal"] == postal) & needs
        if geo["lat"] is not None:
            result.loc[mask, "Latitude"] = geo["lat"]
            result.loc[mask, "Longitude"] = geo["lng"]
            result.loc[mask, "GoogleLocationType"] = geo.get("location_type", "")
            result.loc[mask, "GeoMethod"] = geo.get("method", "")
            result.loc[mask, "AddrOnlyBetter"] = geo.get("addr_only_better", False)

    ok = sum(1 for g in cache.values() if g["status"] == "OK")
    fail = len(cache) - ok
    status_area.markdown(
        f"**{ok}** geocoded. **{fail}** failed. "
        f"**{total_api_calls}** API calls total (**{cache_hits}** from cache, **{total}** unique addresses). "
        f"**{len(result)}** total rows."
    )
    if fallbacks:
        with st.expander(f"ℹ️ {len(fallbacks)} resolved with relaxed constraints — review recommended"):
            st.dataframe(pd.DataFrame(fallbacks), use_container_width=True)
    if errs:
        with st.expander(f"⚠️ {fail} failed — click for details"):
            st.dataframe(pd.DataFrame(errs), use_container_width=True)

    result = result.drop(columns=["_cc", "_full_addr", "_city", "_admin1", "_admin2", "_postal", "_street_only"])

    # Comparison report
    comp = None
    if not skip:
        try:
            cidx = had_coords[had_coords].index
            if len(cidx) > 0:
                cd = {"AddressID": result.loc[cidx, "AddressID"].values,
                      "StreetAddress": result.loc[cidx, "StreetAddress"].values}
                for c in LOCATION_FIELDS:
                    if c in result.columns:
                        cd[c] = result.loc[cidx, c].values
                cd.update({
                    "Original_Latitude": orig_lats.values, "Original_Longitude": orig_lngs.values,
                    "New_Latitude": result.loc[cidx, "Latitude"].values,
                    "New_Longitude": result.loc[cidx, "Longitude"].values,
                    "GoogleLocationType": result.loc[cidx, "GoogleLocationType"].values,
                    "GeoMethod": result.loc[cidx, "GeoMethod"].values,
                    "AddrOnlyBetter": result.loc[cidx, "AddrOnlyBetter"].values,
                })
                comp = pd.DataFrame(cd)
                dists = []
                for _, r in comp.iterrows():
                    try:
                        if all(pd.notna(r[c]) for c in ["Original_Latitude", "Original_Longitude", "New_Latitude", "New_Longitude"]):
                            dists.append(round(haversine_m(float(r["Original_Latitude"]), float(r["Original_Longitude"]),
                                                           float(r["New_Latitude"]), float(r["New_Longitude"])), 2))
                        else:
                            dists.append(None)
                    except:
                        dists.append(None)
                comp["Distance_m"] = dists
        except Exception as e:
            st.warning(f"Could not generate comparison report: {e}")

    return result, comp


def build_recommendations(result_df, comparison_df=None):
    """Build a recommendations dataframe from geocoded results.
    If comparison_df is provided, use distance to flag large discrepancies."""

    # Build a distance lookup from comparison report
    distance_lookup = {}
    if comparison_df is not None and len(comparison_df) > 0:
        for _, row in comparison_df.iterrows():
            aid = str(row.get("AddressID", "")).strip()
            dist = row.get("Distance_m")
            if aid and pd.notna(dist):
                distance_lookup[aid] = float(dist)

    recs = []
    for idx, row in result_df.iterrows():
        addr_id = str(row.get("AddressID", "")).strip()
        street = row.get("StreetAddress", "")
        loc_type = str(row.get("GoogleLocationType", "")).strip()
        method = str(row.get("GeoMethod", "")).strip()
        aob = row.get("AddrOnlyBetter", False)
        lat = row.get("Latitude")
        lng = row.get("Longitude")

        # Check distance from original if available
        dist_m = distance_lookup.get(addr_id)
        dist_km = dist_m / 1000 if dist_m is not None else None

        # Distance flags override other categories
        distance_flag = None
        distance_note = ""
        if dist_km is not None:
            if dist_km >= 50:
                distance_flag = "red"
                distance_note = (f" Distance from original: {dist_km:,.1f}km — "
                                 "large discrepancy detected. Either the street address, "
                                 "admin fields, or original coordinates may be incorrect. "
                                 "High priority investigation required.")
            elif dist_km >= 5:
                distance_flag = "orange"
                distance_note = (f" Distance from original: {dist_km:,.1f}km — "
                                 "notable difference from original coordinates. "
                                 "Worth investigating to determine which source is more accurate.")

        if pd.isna(lat) or pd.isna(lng) or loc_type == "":
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": "❌ Failed", "Priority": 1,
                         "Recommendation": "Geocoding failed entirely — all attempts exhausted. Manual geocoding required. "
                         "Check both the street address and admin fields for errors."})
        elif distance_flag == "red":
            # Red distance flag overrides everything — even ROOFTOP
            cat = "🔴 Large coordinate discrepancy"
            rec_text = (f"Google returned {loc_type} via {method}, but coordinates are {dist_km:,.1f}km "
                        "from the original. This is a significant discrepancy that requires investigation. "
                        "Possible causes: (1) street address is wrong or ambiguous and matched a different location, "
                        "(2) admin fields were incorrect causing a wrong match, "
                        "(3) original coordinates were wrong and Google is actually correct. "
                        "Manually verify which coordinates are right.")
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": cat, "Priority": 1,
                         "Recommendation": rec_text})
        elif distance_flag == "orange":
            cat = "🟠 Notable coordinate difference"
            rec_text = (f"Google returned {loc_type} via {method}, but coordinates are {dist_km:,.1f}km "
                        "from the original. Check whether the new or original coordinates are more accurate.")
            if aob:
                rec_text += " Address-only search outperformed constrained — admin data may have issues."
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": cat, "Priority": 3,
                         "Recommendation": rec_text})
        elif loc_type == "APPROXIMATE":
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": "🔴 Very low precision", "Priority": 2,
                         "Recommendation": "Approximate location only (admin/region level) — best result across all attempts. "
                         "Not suitable for cat modelling. Manual check required. "
                         "Verify the street address is correct and complete." + distance_note})
        elif loc_type == "GEOMETRIC_CENTER":
            rec_text = ("Centroid of postal code or city area — best result across all attempts. "
                        "Acceptable for DLM but not HD models. Manual check recommended.")
            if method in ("constrained", "constrained_no_city", "country_only"):
                rec_text += " The constrained result was preferred — verify the street address exists."
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": "🟠 Low precision", "Priority": 3,
                         "Recommendation": rec_text + distance_note})
        elif loc_type == "RANGE_INTERPOLATED":
            rec_text = "Interpolated street position — not rooftop level."
            if aob:
                rec_text += " Address-only outperformed constrained — admin data may be incorrect."
            if method != "constrained":
                rec_text += f" Resolved via {method}."
            rec_text += " Review if high-value location."
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": "🟡 Below rooftop", "Priority": 4,
                         "Recommendation": rec_text + distance_note})
        elif loc_type == "ROOFTOP" and aob:
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": "🟢 Rooftop (data quality flag)", "Priority": 5,
                         "Recommendation": "Rooftop precision achieved, but address-only outperformed constrained — "
                         "admin fields likely have data quality issues. Coordinates are good, but review admin data." + distance_note})
        elif loc_type == "ROOFTOP" and method != "constrained":
            recs.append({"AddressID": addr_id, "StreetAddress": street,
                         "GoogleLocationType": loc_type, "GeoMethod": method,
                         "Distance_km": dist_km,
                         "Category": "🟢 Rooftop (relaxed constraints)", "Priority": 5,
                         "Recommendation": f"Rooftop precision via {method}. Coordinates are good but admin data may need correction." + distance_note})
        # ROOFTOP + constrained + no flags + no distance flag = perfect, skip

    if not recs:
        return None

    rec_df = pd.DataFrame(recs)
    rec_df = rec_df.sort_values("Priority").reset_index(drop=True)
    return rec_df


# =============================================================================
# MAIN APP
# =============================================================================

uploaded = st.file_uploader("Upload your file", type=["csv", "xlsx", "xls", "txt", "tsv"])

if uploaded:
    raw_df, err = read_raw_file(uploaded)
    if err:
        st.error(err)
        st.stop()

    # Step 1: Header row
    st.subheader("Step 1: Select the header row")
    st.markdown("Pick the row that contains your column names.")
    preview = min(15, len(raw_df))
    disp = raw_df.head(preview).copy()
    disp.index = [f"Row {i}" for i in range(preview)]
    st.dataframe(disp, use_container_width=True)
    header_row = st.number_input("Header row number", 0, max(0, len(raw_df) - 2), 0, 1)
    df_h, h_err = apply_header(raw_df, header_row)
    if h_err:
        st.error(h_err)
        st.stop()
    avail = list(df_h.columns)
    st.success(f"Row {header_row} as header. **{len(df_h)}** data rows, **{len(avail)}** columns.")

    # Step 2: Column mapping
    st.subheader("Step 2: Map your columns")
    st.markdown("Match each field to a column from your file.")
    opts = [UNMAPPED] + avail
    mapping = {}

    st.markdown("**Core fields:**")
    c1, c2, c3, c4 = st.columns(4)
    for col_ui, field in zip([c1, c2, c3, c4], CORE_FIELDS):
        idx = opts.index(guess_column(field, avail)) if guess_column(field, avail) in opts else 0
        with col_ui:
            mapping[field] = st.selectbox(field, opts, idx, key=f"m_{field}")

    st.markdown("**Location fields:**")
    l1, l2, l3 = st.columns(3)
    for col_ui, field in zip([l1, l2, l3], LOCATION_FIELDS[:3]):
        idx = opts.index(guess_column(field, avail)) if guess_column(field, avail) in opts else 0
        with col_ui:
            mapping[field] = st.selectbox(field, opts, idx, key=f"m_{field}")
    l4, l5, _ = st.columns(3)
    for col_ui, field in zip([l4, l5], LOCATION_FIELDS[3:]):
        idx = opts.index(guess_column(field, avail)) if guess_column(field, avail) in opts else 0
        with col_ui:
            mapping[field] = st.selectbox(field, opts, idx, key=f"m_{field}")

    st.markdown("**Your column mappings:**")
    lines = []
    for f in ALL_FIELDS:
        s = mapping.get(f, UNMAPPED)
        lines.append(f"- **{f}** ← `{s}`" if s != UNMAPPED else f"- **{f}** ← *(not mapped)*")
    st.markdown("\n".join(lines))

    unmapped = [f for f in ALL_FIELDS if mapping.get(f) == UNMAPPED]
    if unmapped:
        st.warning(f"Fields not mapped: **{', '.join(unmapped)}**.")
        st.stop()

    mapped_vals = [c for c in mapping.values() if c != UNMAPPED]
    if len(mapped_vals) != len(set(mapped_vals)):
        seen, dupes = set(), set()
        for c in mapped_vals:
            (dupes if c in seen else seen).add(c)
        st.error(f"Column **{', '.join(dupes)}** mapped to multiple fields.")
        st.stop()

    df_m = df_h.copy().rename(columns={v: k for k, v in mapping.items() if v != UNMAPPED})

    st.subheader("Mapped data preview (first 10 rows)")
    st.dataframe(df_m.head(10), use_container_width=True)

    # Step 3: Validation
    st.subheader("Step 3: Validation")
    val = validate_dataframe(df_m, skip_existing)
    s = val["stats"]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Rows", s["total_rows"])
    c2.metric("Unique Addresses", s["unique_addresses"])
    c3.metric("Already Geocoded", s["already_geocoded"])
    c4.metric("Blank Addresses", s["blank_addresses"])
    c5.metric("To Geocode", s["to_geocode"])

    has_err = len(val["errors"]) > 0
    if has_err:
        st.markdown("#### ❌ Errors")
        for e in val["errors"]:
            st.error(e)
    if val["warnings"]:
        st.markdown("#### ⚠️ Warnings")
        for w in val["warnings"]:
            st.warning(w)
    if val["flagged_rows"]:
        st.markdown("#### 🔍 Flagged Rows")
        for lbl, fdf in val["flagged_rows"].items():
            with st.expander(f"{lbl} ({len(fdf)} rows)"):
                st.dataframe(fdf, use_container_width=True)
    if not has_err and not val["warnings"]:
        st.success("✅ All checks passed!")

    # Step 4: Geocode
    st.divider()
    st.subheader("Step 4: Geocode")
    can_go = True
    if has_err:
        st.warning("Errors found. Affected rows will be skipped.")
        can_go = st.checkbox("I've reviewed the errors and want to proceed anyway")

    if can_go:
        if st.button("🚀 Geocode", disabled=not api_key, type="primary"):
            if not api_key:
                st.error("Enter your API key in the sidebar.")
            else:
                res_df, comp_df = process_dataframe(df_m, api_key, delay, skip_existing)

                st.subheader("Results")
                st.dataframe(res_df, use_container_width=True)
                st.download_button("📥 Download geocoded CSV", res_df.to_csv(index=False),
                                   "geocoded_output.csv", "text/csv")

                # Map preview
                md = res_df[["StreetAddress", "Latitude", "Longitude"]].copy()
                md["Latitude"] = pd.to_numeric(md["Latitude"], errors="coerce")
                md["Longitude"] = pd.to_numeric(md["Longitude"], errors="coerce")
                md = md.dropna(subset=["Latitude", "Longitude"])
                if len(md) > 0:
                    st.subheader("Map Preview")
                    st.markdown("Hover over a point to see the address.")
                    clat, clng = md["Latitude"].mean(), md["Longitude"].mean()
                    sp = max(md["Latitude"].max() - md["Latitude"].min(),
                             md["Longitude"].max() - md["Longitude"].min())
                    zm = 14 if sp < 0.01 else 11 if sp < 0.1 else 8 if sp < 1 else 5 if sp < 10 else 2
                    ps = st.slider("Point size (pixels)", 2, 20, 6, 1)
                    layer = pdk.Layer("ScatterplotLayer", data=md,
                                      get_position=["Longitude", "Latitude"], get_radius=100,
                                      radius_min_pixels=ps, radius_max_pixels=ps * 3,
                                      get_fill_color=[65, 105, 225, 180], pickable=True, auto_highlight=True)
                    tip = {"html": "<b>{StreetAddress}</b><br/>Lat: {Latitude}<br/>Lng: {Longitude}",
                           "style": {"backgroundColor": "#1a1a2e", "color": "white", "fontSize": "12px"}}
                    st.pydeck_chart(pdk.Deck(layers=[layer],
                                             initial_view_state=pdk.ViewState(latitude=clat, longitude=clng, zoom=zm, pitch=0),
                                             tooltip=tip, map_provider="carto", map_style="light"))

                # Recommendations
                rec_df = build_recommendations(res_df, comp_df)
                if rec_df is not None and len(rec_df) > 0:
                    st.subheader("Step 5: Recommendations")
                    st.markdown(
                        "Based on geocoding quality, the following rows need attention. "
                        "Rows with ROOFTOP precision via the constrained method are not listed — those are good."
                    )

                    # Summary counts
                    cat_counts = rec_df["Category"].value_counts()
                    for cat in sorted(cat_counts.index, key=lambda x: rec_df.loc[rec_df["Category"] == x, "Priority"].iloc[0]):
                        st.markdown(f"- {cat}: **{cat_counts[cat]}** row(s)")

                    st.dataframe(rec_df.drop(columns=["Priority"]), use_container_width=True)
                    st.download_button("📥 Download recommendations CSV", rec_df.drop(columns=["Priority"]).to_csv(index=False),
                                       "geocode_recommendations.csv", "text/csv", key="dl_rec")
                else:
                    st.success("🎉 All addresses geocoded at ROOFTOP precision with full constraints — no recommendations needed!")

                # Comparison report
                if not skip_existing:
                    if comp_df is not None and len(comp_df) > 0:
                        st.subheader("Comparison Report")
                        st.markdown("Original vs newly geocoded coordinates, with distance and quality.")
                        st.dataframe(comp_df, use_container_width=True)
                        st.download_button("📥 Download comparison report", comp_df.to_csv(index=False),
                                           "geocode_comparison_report.csv", "text/csv", key="dl_comp")
                    else:
                        st.info("ℹ️ No comparison report — no rows had existing coordinates.")

else:
    st.markdown(
        """
        ### How to use
        1. Paste your **Google Geocoding API key** in the sidebar.
        2. Upload a file — CSV, Excel (.xlsx / .xls), or delimited text (.txt / .tsv).
        3. **Select the header row** — pick which row contains your column names.
        4. **Map your columns** — match your file's columns to: `AddressID`, `StreetAddress`, `Latitude`, `Longitude`, `CityName`, `Admin2Name`, `Admin1Name`, `PostalCode`, `CountryCode`.
        5. Review the **validation report** and fix any issues if needed.
        6. Click **Geocode** — the app uses a two-pass strategy (constrained then address-only) and deduplicates to minimise API calls.
        7. Review the **recommendations** for any rows that need manual attention.
        8. Download the results and comparison report.

        Any additional columns in your file are preserved as-is in the output.
        """
    )