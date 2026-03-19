import streamlit as st
import pandas as pd
import requests
import time
import math
import pydeck as pdk

st.set_page_config(page_title="Batch Geocoder", page_icon="🌍", layout="wide")

st.title("🌍 Batch Geocoder")
st.markdown("Upload a file, geocode addresses using Google's Geocoding API, and download the results.")

# --- Sidebar: API Key ---
st.sidebar.header("Configuration")
api_key = st.sidebar.text_input(
    "Google Geocoding API Key", type="password",
    help="Paste your API key here. It is not stored anywhere."
)
if not api_key:
    st.sidebar.warning("Please enter your API key to enable geocoding.")

# --- Sidebar: Settings ---
st.sidebar.header("Settings")
delay = st.sidebar.slider(
    "Delay between API calls (seconds)",
    min_value=0.0, max_value=2.0, value=0.1, step=0.05,
    help="Adds a pause between requests to avoid hitting rate limits."
)
skip_existing = st.sidebar.checkbox(
    "Skip rows that already have coordinates", value=True,
    help="If checked, rows with non-empty Latitude and Longitude will be left as-is."
)

CORE_FIELDS = ["AddressID", "StreetAddress", "Latitude", "Longitude"]
LOCATION_FIELDS = ["CityName", "Admin2Name", "Admin1Name", "PostalCode", "CountryCode"]
ALL_FIELDS = CORE_FIELDS + LOCATION_FIELDS
UNMAPPED = "-- Not mapped --"


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def geocode_address(address: str, key: str, country_code: str = None) -> dict:
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": key}
    if country_code:
        params["components"] = f"country:{country_code}"
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "UNKNOWN")
        if status == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return {"lat": loc["lat"], "lng": loc["lng"], "status": "OK"}
        status_messages = {
            "ZERO_RESULTS": "No location found for this address. Check for typos or missing details.",
            "OVER_DAILY_LIMIT": "API daily quota exceeded. Try again tomorrow or check your billing setup in Google Cloud Console.",
            "OVER_QUERY_LIMIT": "Too many requests too quickly. Increase the delay in the sidebar and try again.",
            "REQUEST_DENIED": "API request denied. Check that your API key is valid, the Geocoding API is enabled in Google Cloud Console, and billing is active.",
            "INVALID_REQUEST": "The address could not be processed. It may be empty or contain invalid characters.",
            "UNKNOWN_ERROR": "Google returned an unknown server error. Try again in a few seconds.",
        }
        detail = status_messages.get(status, f"Unexpected API status: {status}")
        return {"lat": None, "lng": None, "status": status, "detail": detail}
    except requests.Timeout:
        return {"lat": None, "lng": None, "status": "TIMEOUT",
                "detail": "Request timed out after 10 seconds. Check your internet connection and try again."}
    except requests.ConnectionError:
        return {"lat": None, "lng": None, "status": "CONNECTION_ERROR",
                "detail": "Could not connect to Google's servers. Check your internet connection."}
    except requests.HTTPError as e:
        return {"lat": None, "lng": None, "status": "HTTP_ERROR",
                "detail": f"HTTP error from Google: {e}. Check your API key and billing."}
    except requests.RequestException as e:
        return {"lat": None, "lng": None, "status": "ERROR",
                "detail": f"Unexpected request error: {e}"}
    except (KeyError, IndexError, TypeError) as e:
        return {"lat": None, "lng": None, "status": "PARSE_ERROR",
                "detail": f"Could not parse Google's response: {e}. This is unusual — try again."}


# =============================================================================
# FILE READING
# =============================================================================

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
            return None, (
                f"Unsupported file type: **{name.split('.')[-1]}**. "
                "Please upload a CSV, Excel (.xlsx/.xls), or delimited text (.txt/.tsv) file."
            )
        if df is None or df.empty:
            return None, "The file was read successfully but contains no data. Please check the file and try again."
        if len(df.columns) < 2:
            return None, (
                "The file appears to have only one column. This usually means the wrong delimiter was detected. "
                "If this is a text file, try saving it as a CSV instead."
            )
        return df, None
    except pd.errors.EmptyDataError:
        return None, "The file is empty. Please upload a file that contains data."
    except pd.errors.ParserError as e:
        return None, f"Could not parse the file. Detail: {e}"
    except Exception as e:
        return None, f"Unexpected error reading the file: **{type(e).__name__}** — {e}"


def apply_header(raw_df: pd.DataFrame, header_row: int) -> tuple[pd.DataFrame, str | None]:
    if header_row >= len(raw_df):
        return None, f"Row {header_row} is beyond the end of the file (file has {len(raw_df)} rows)."
    header_values = raw_df.iloc[header_row]
    non_blank = header_values.dropna()
    non_blank = non_blank[non_blank.astype(str).str.strip() != ""]
    if len(non_blank) == 0:
        return None, (
            f"Row {header_row} is entirely blank. This probably isn't your header row. "
            "Look at the raw preview above and pick the row that contains your column names."
        )
    df = raw_df.copy()
    col_names = header_values.astype(str).str.strip()
    dupes = col_names[col_names.duplicated(keep=False)]
    dupes = dupes[dupes != ""]
    if len(dupes) > 0:
        dupe_list = ", ".join(sorted(dupes.unique()))
        return None, (
            f"Row {header_row} has duplicate column names: **{dupe_list}**. "
            "Each column must have a unique name."
        )
    blank_cols = (col_names == "") | (col_names == "nan")
    if blank_cols.any() and not blank_cols.all():
        n_blank = blank_cols.sum()
        return None, (
            f"Row {header_row} has **{n_blank}** blank column name(s) alongside named columns. "
            "Check your file or choose a different header row."
        )
    df.columns = col_names
    df = df.iloc[header_row + 1:].reset_index(drop=True)
    if df.empty:
        return None, f"After using row {header_row} as the header, there are no data rows left."
    return df, None


def guess_column(field_name, available):
    field_lower = field_name.lower()
    for col in available:
        if col.strip().lower() == field_lower:
            return col
    for col in available:
        col_clean = col.strip().lower()
        if field_lower in col_clean or col_clean in field_lower:
            return col
    aliases = {
        "streetaddress": ["street", "address", "addr", "streetaddr", "street_address"],
        "addressid": ["id", "address_id", "addr_id", "locid", "location_id"],
        "latitude": ["lat", "y"],
        "longitude": ["lng", "lon", "long", "x"],
        "countrycode": ["country", "countryiso", "iso2", "iso2a", "country_code", "cntry"],
        "cityname": ["city", "town", "locality", "suburb", "city_name"],
        "admin1name": ["admin1", "state", "province", "region", "admin1_name", "statename"],
        "admin2name": ["admin2", "county", "district", "admin2_name", "countyname"],
        "postalcode": ["postcode", "postal", "zip", "zipcode", "postal_code", "zip_code", "pcode"],
    }
    field_aliases = aliases.get(field_lower, [])
    for col in available:
        col_clean = col.strip().lower()
        if col_clean in field_aliases:
            return col
    return UNMAPPED


# =============================================================================
# VALIDATION
# =============================================================================

def validate_dataframe(df: pd.DataFrame, skip: bool) -> dict:
    errors = []
    warnings = []
    flagged_rows = {}
    total_rows = len(df)

    blank_address = df["StreetAddress"].isna() | (df["StreetAddress"].str.strip() == "")
    blank_address_count = int(blank_address.sum())
    if blank_address_count > 0:
        if blank_address_count == total_rows:
            errors.append("**Every row** has a blank `StreetAddress`. Did you map the right column?")
        else:
            errors.append(f"**{blank_address_count}** of {total_rows} rows have a blank or missing `StreetAddress`.")
        flagged_rows["Blank StreetAddress"] = df.loc[blank_address]

    blank_id = df["AddressID"].isna() | (df["AddressID"].astype(str).str.strip() == "")
    blank_id_count = int(blank_id.sum())
    if blank_id_count > 0:
        if blank_id_count == total_rows:
            errors.append("**Every row** has a blank `AddressID`. Did you map the right column?")
        else:
            errors.append(f"**{blank_id_count}** of {total_rows} rows have a blank or missing `AddressID`.")
        flagged_rows["Blank AddressID"] = df.loc[blank_id]

    id_col = df["AddressID"].astype(str).str.strip()
    non_blank_ids = id_col[~blank_id]
    dup_mask = non_blank_ids.duplicated(keep=False)
    dup_ids = non_blank_ids[dup_mask]
    if len(dup_ids) > 0:
        n_dup = dup_ids.nunique()
        n_rows = len(dup_ids)
        errors.append(f"**{n_dup}** `AddressID` value(s) are duplicated across **{n_rows}** rows.")
        flagged_rows["Duplicate AddressID"] = df.loc[dup_ids.index]

    if len(dup_ids) > 0:
        check = df.loc[dup_ids.index].copy()
        check["_id_clean"] = check["AddressID"].astype(str).str.strip()
        check["_addr_clean"] = check["StreetAddress"].astype(str).str.strip().str.lower()
        conflicting = check.groupby("_id_clean").filter(lambda g: g["_addr_clean"].nunique() > 1)
        if len(conflicting) > 0:
            n_conf = conflicting["_id_clean"].nunique()
            errors.append(f"**{n_conf}** `AddressID`(s) are linked to **different** `StreetAddress` values.")
            flagged_rows["Conflicting ID/Address"] = df.loc[conflicting.index]

    lat_raw, lng_raw = df["Latitude"], df["Longitude"]
    lat_numeric = pd.to_numeric(lat_raw, errors="coerce")
    lng_numeric = pd.to_numeric(lng_raw, errors="coerce")

    lat_junk = lat_raw.notna() & (lat_raw.astype(str).str.strip() != "") & lat_numeric.isna()
    lng_junk = lng_raw.notna() & (lng_raw.astype(str).str.strip() != "") & lng_numeric.isna()
    junk_coords = lat_junk | lng_junk
    junk_count = int(junk_coords.sum())
    if junk_count > 0:
        warnings.append(f"**{junk_count}** row(s) have non-numeric values in `Latitude` or `Longitude`.")
        flagged_rows["Non-numeric Coordinates"] = df.loc[junk_coords]

    lat_out = lat_numeric.notna() & ((lat_numeric < -90) | (lat_numeric > 90))
    lng_out = lng_numeric.notna() & ((lng_numeric < -180) | (lng_numeric > 180))
    out_of_range = lat_out | lng_out
    if int(out_of_range.sum()) > 0:
        warnings.append(f"**{int(out_of_range.sum())}** row(s) have coordinates outside valid ranges.")
        flagged_rows["Out-of-range Coordinates"] = df.loc[out_of_range]

    valid_addr = ~blank_address
    short_addr = valid_addr & (df["StreetAddress"].str.strip().str.len() < 5)
    if int(short_addr.sum()) > 0:
        warnings.append(f"**{int(short_addr.sum())}** row(s) have very short addresses (< 5 chars).")
        flagged_rows["Short Addresses"] = df.loc[short_addr]

    already_geocoded = int((lat_numeric.notna() & lng_numeric.notna()).sum())
    valid_with_coords = int((lat_numeric.notna() & lng_numeric.notna() & valid_addr).sum())
    valid_address_count = int(valid_addr.sum())

    if skip and valid_with_coords == valid_address_count and valid_address_count > 0:
        warnings.append(
            "**Every row with an address already has coordinates**, and \"Skip rows that already have coordinates\" "
            "is turned on. Nothing will be geocoded. Uncheck that option in the sidebar if you want to re-geocode."
        )

    unique_to_geocode = df.loc[valid_addr, "StreetAddress"].nunique()
    if skip:
        needs_geo = valid_addr & (lat_numeric.isna() | lng_numeric.isna())
        unique_to_geocode = df.loc[needs_geo, "StreetAddress"].nunique()

    if unique_to_geocode > 2500:
        warnings.append(f"**{unique_to_geocode}** unique addresses need geocoding. This may incur significant API costs.")
    elif unique_to_geocode > 500:
        warnings.append(
            f"**{unique_to_geocode}** unique addresses need geocoding. "
            f"Estimated time: ~**{int(unique_to_geocode * (delay + 0.3) / 60)} minutes**."
        )

    stats = {
        "total_rows": total_rows,
        "unique_addresses": int(df.loc[valid_addr, "StreetAddress"].nunique()),
        "blank_addresses": blank_address_count,
        "already_geocoded": already_geocoded,
        "to_geocode": unique_to_geocode,
    }

    return {"errors": errors, "warnings": warnings, "stats": stats, "flagged_rows": flagged_rows}


# =============================================================================
# GEOCODING
# =============================================================================

def process_dataframe(df: pd.DataFrame, key: str, delay_s: float, skip: bool) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    result = df.copy()
    result["Latitude"] = pd.to_numeric(result["Latitude"], errors="coerce")
    result["Longitude"] = pd.to_numeric(result["Longitude"], errors="coerce")

    has_address = result["StreetAddress"].notna() & (result["StreetAddress"].str.strip() != "")

    # Build composite address string from all available location fields
    address_parts = ["StreetAddress", "CityName", "Admin2Name", "Admin1Name", "PostalCode"]

    def build_full_address(row):
        parts = []
        for col in address_parts:
            if col in row.index:
                val = str(row[col]).strip() if pd.notna(row[col]) else ""
                if val and val.lower() != "nan":
                    parts.append(val)
        return ", ".join(parts)

    result["_full_address"] = result.apply(build_full_address, axis=1)

    # Prepare country codes if available
    has_country = "CountryCode" in result.columns
    if has_country:
        result["_country_clean"] = result["CountryCode"].fillna("").astype(str).str.strip().str.upper()
    else:
        result["_country_clean"] = ""

    # Track original coordinates for comparison
    had_coords = has_address & result["Latitude"].notna() & result["Longitude"].notna()
    original_lats = result.loc[had_coords, "Latitude"].copy()
    original_lngs = result.loc[had_coords, "Longitude"].copy()

    if skip:
        needs_geocoding = has_address & (result["Latitude"].isna() | result["Longitude"].isna())
    else:
        needs_geocoding = has_address

    # Deduplicate on full address + country code
    geo_subset = result.loc[needs_geocoding, ["_full_address", "_country_clean"]].copy()
    geo_subset = geo_subset[geo_subset["_full_address"].str.strip() != ""]
    unique_combos = geo_subset.drop_duplicates().values.tolist()
    total = len(unique_combos)

    if total == 0:
        st.info("Nothing to geocode — all valid rows already have coordinates.")
        return result.drop(columns=["_full_address", "_country_clean"]), None

    # Quick API key validation
    with st.spinner("Validating API key…"):
        test = geocode_address("10 Downing Street, London, UK", key)
        if test["status"] == "REQUEST_DENIED":
            st.error("❌ **API key rejected by Google.** " + test.get("detail", ""))
            return result.drop(columns=["_full_address", "_country_clean"]), None
        if test["status"] in ("OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"):
            st.error("❌ **API quota exceeded.** " + test.get("detail", ""))
            return result.drop(columns=["_full_address", "_country_clean"]), None

    progress = st.progress(0, text="Starting geocoding…")
    status_area = st.empty()

    cache = {}
    geo_errors = []
    consecutive_failures = 0

    for i, (addr, cc) in enumerate(unique_combos):
        progress.progress((i + 1) / total, text=f"Geocoding {i + 1} of {total}…")
        cache_key = (addr, cc)
        geo = geocode_address(addr, key, country_code=cc if cc else None)
        cache[cache_key] = geo

        if geo["status"] != "OK":
            geo_errors.append({
                "FullAddress": addr,
                "CountryCode": cc if cc else "(none)",
                "Status": geo["status"],
                "Detail": geo.get("detail", ""),
            })
            consecutive_failures += 1
            if consecutive_failures >= 5 and i < total - 1:
                st.warning(
                    f"⚠️ **{consecutive_failures} consecutive failures.** Last error: "
                    f"*{geo.get('detail', geo['status'])}*. Stopping early."
                )
                break
        else:
            consecutive_failures = 0

        if delay_s > 0:
            time.sleep(delay_s)

    progress.progress(1.0, text="Done!")

    # Apply results
    for (addr, cc), geo in cache.items():
        mask = (result["_full_address"] == addr) & (result["_country_clean"] == cc) & needs_geocoding
        if geo["lat"] is not None:
            result.loc[mask, "Latitude"] = geo["lat"]
            result.loc[mask, "Longitude"] = geo["lng"]

    ok_count = sum(1 for g in cache.values() if g["status"] == "OK")
    fail_count = len(cache) - ok_count
    calls_saved = int(needs_geocoding.sum()) - total

    status_area.markdown(
        f"**{ok_count}** unique addresses geocoded successfully. "
        f"**{fail_count}** failed. "
        f"**{len(cache)}** API calls made (saved **{calls_saved}** through deduplication). "
        f"**{len(result)}** total rows in output."
    )

    if geo_errors:
        with st.expander(f"⚠️ {fail_count} address(es) could not be geocoded — click for details"):
            st.dataframe(pd.DataFrame(geo_errors), use_container_width=True)

    # Clean up temp columns
    result = result.drop(columns=["_country_clean", "_full_address"])

    # Comparison report
    comparison = None
    if not skip:
        try:
            comp_idx = had_coords[had_coords].index
            if len(comp_idx) > 0:
                comp_data = {
                    "AddressID": result.loc[comp_idx, "AddressID"].values,
                    "StreetAddress": result.loc[comp_idx, "StreetAddress"].values,
                }
                for col in ["CityName", "Admin2Name", "Admin1Name", "PostalCode", "CountryCode"]:
                    if col in result.columns:
                        comp_data[col] = result.loc[comp_idx, col].values
                comp_data.update({
                    "Original_Latitude": original_lats.values,
                    "Original_Longitude": original_lngs.values,
                    "New_Latitude": result.loc[comp_idx, "Latitude"].values,
                    "New_Longitude": result.loc[comp_idx, "Longitude"].values,
                })
                comparison = pd.DataFrame(comp_data)
                distances = []
                for _, r in comparison.iterrows():
                    try:
                        if pd.notna(r["Original_Latitude"]) and pd.notna(r["Original_Longitude"]) \
                                and pd.notna(r["New_Latitude"]) and pd.notna(r["New_Longitude"]):
                            d = haversine_m(
                                float(r["Original_Latitude"]), float(r["Original_Longitude"]),
                                float(r["New_Latitude"]), float(r["New_Longitude"])
                            )
                            distances.append(round(d, 2))
                        else:
                            distances.append(None)
                    except (ValueError, TypeError):
                        distances.append(None)
                comparison["Distance_m"] = distances
        except Exception as e:
            st.warning(f"⚠️ Could not generate comparison report: {e}")
            comparison = None

    return result, comparison


# =============================================================================
# MAIN APP FLOW
# =============================================================================

uploaded = st.file_uploader("Upload your file", type=["csv", "xlsx", "xls", "txt", "tsv"])

if uploaded:

    raw_df, read_error = read_raw_file(uploaded)
    if read_error:
        st.error(read_error)
        st.stop()

    # ---- Step 1: Header row selection ----
    st.subheader("Step 1: Select the header row")
    st.markdown("Below are the first rows of your file as raw data. Pick the row that contains your column names.")

    preview_rows = min(15, len(raw_df))
    display_raw = raw_df.head(preview_rows).copy()
    display_raw.index = [f"Row {i}" for i in range(preview_rows)]
    st.dataframe(display_raw, use_container_width=True)

    header_row = st.number_input(
        "Header row number", min_value=0, max_value=max(0, len(raw_df) - 2),
        value=0, step=1, help="0-indexed. Row 0 = the first row in the file."
    )

    df_with_header, header_error = apply_header(raw_df, header_row)
    if header_error:
        st.error(header_error)
        st.stop()

    available_columns = list(df_with_header.columns)
    st.success(f"Using row {header_row} as header. **{len(df_with_header)}** data rows, **{len(available_columns)}** columns detected.")

    # ---- Step 2: Column mapping ----
    st.subheader("Step 2: Map your columns")
    st.markdown("Match each field to a column from your file. The app will try to auto-detect matches, but you can change them.")

    options = [UNMAPPED] + available_columns
    mapping = {}

    st.markdown("**Core fields:**")
    core_cols = st.columns(4)
    for i, field in enumerate(CORE_FIELDS):
        default = guess_column(field, available_columns)
        default_idx = options.index(default) if default in options else 0
        with core_cols[i]:
            mapping[field] = st.selectbox(field, options=options, index=default_idx, key=f"map_{field}")

    st.markdown("**Location fields:**")
    loc_cols_row1 = st.columns(3)
    for i, field in enumerate(LOCATION_FIELDS[:3]):
        default = guess_column(field, available_columns)
        default_idx = options.index(default) if default in options else 0
        with loc_cols_row1[i]:
            mapping[field] = st.selectbox(field, options=options, index=default_idx, key=f"map_{field}")

    loc_cols_row2 = st.columns(3)
    for i, field in enumerate(LOCATION_FIELDS[3:]):
        default = guess_column(field, available_columns)
        default_idx = options.index(default) if default in options else 0
        with loc_cols_row2[i]:
            mapping[field] = st.selectbox(field, options=options, index=default_idx, key=f"map_{field}")

    # Show mapping summary
    st.markdown("**Your column mappings:**")
    mapping_lines = []
    for field in ALL_FIELDS:
        src = mapping.get(field, UNMAPPED)
        if src != UNMAPPED:
            mapping_lines.append(f"- **{field}** ← `{src}`")
        else:
            mapping_lines.append(f"- **{field}** ← *(not mapped)*")
    st.markdown("\n".join(mapping_lines))

    # Check all fields are mapped
    unmapped = [f for f in ALL_FIELDS if mapping.get(f) == UNMAPPED]
    if unmapped:
        st.warning(
            f"The following fields are not mapped yet: **{', '.join(unmapped)}**. "
            "Use the dropdowns above to map each one to a column from your file."
        )
        st.stop()

    # Check no duplicate mappings
    mapped_cols = [c for c in mapping.values() if c != UNMAPPED]
    if len(mapped_cols) != len(set(mapped_cols)):
        seen = set()
        dupes = set()
        for c in mapped_cols:
            if c in seen:
                dupes.add(c)
            seen.add(c)
        st.error(
            f"The column **{', '.join(dupes)}** is mapped to multiple fields. "
            "Each field must use a different column."
        )
        st.stop()

    # Build working dataframe
    df_mapped = df_with_header.copy()
    rename_map = {v: k for k, v in mapping.items() if v != UNMAPPED}
    df_mapped = df_mapped.rename(columns=rename_map)

    st.subheader("Mapped data preview (first 10 rows)")
    st.dataframe(df_mapped.head(10), use_container_width=True)

    # ---- Step 3: Validation ----
    st.subheader("Step 3: Validation")
    validation = validate_dataframe(df_mapped, skip_existing)
    stats = validation["stats"]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Rows", stats["total_rows"])
    c2.metric("Unique Addresses", stats["unique_addresses"])
    c3.metric("Already Geocoded", stats["already_geocoded"])
    c4.metric("Blank Addresses", stats["blank_addresses"])
    c5.metric("To Geocode", stats["to_geocode"])

    has_errors = len(validation["errors"]) > 0

    if has_errors:
        st.markdown("#### ❌ Errors")
        for err in validation["errors"]:
            st.error(err)

    if validation["warnings"]:
        st.markdown("#### ⚠️ Warnings")
        for warn in validation["warnings"]:
            st.warning(warn)

    if validation["flagged_rows"]:
        st.markdown("#### 🔍 Flagged Rows")
        for label, flagged_df in validation["flagged_rows"].items():
            with st.expander(f"{label} ({len(flagged_df)} rows)"):
                st.dataframe(flagged_df, use_container_width=True)

    if not has_errors and not validation["warnings"]:
        st.success("✅ All validation checks passed — your data looks good!")

    # ---- Step 4: Geocode ----
    st.divider()
    st.subheader("Step 4: Geocode")

    if has_errors:
        st.warning("There are errors in your data. You can still proceed, but affected rows will be skipped.")
        proceed_anyway = st.checkbox("I've reviewed the errors and want to proceed anyway")
        can_geocode = proceed_anyway
    else:
        can_geocode = True

    if can_geocode:
        if st.button("🚀 Geocode", disabled=not api_key, type="primary"):
            if not api_key:
                st.error("Please enter your API key in the sidebar first.")
            else:
                result_df, comparison_df = process_dataframe(df_mapped, api_key, delay, skip_existing)

                st.subheader("Results")
                st.dataframe(result_df, use_container_width=True)

                csv_out = result_df.to_csv(index=False)
                st.download_button(
                    label="📥 Download geocoded CSV",
                    data=csv_out,
                    file_name="geocoded_output.csv",
                    mime="text/csv",
                )

                # ---- Map Preview ----
                map_data = result_df[["StreetAddress", "Latitude", "Longitude"]].copy()
                map_data["Latitude"] = pd.to_numeric(map_data["Latitude"], errors="coerce")
                map_data["Longitude"] = pd.to_numeric(map_data["Longitude"], errors="coerce")
                map_data = map_data.dropna(subset=["Latitude", "Longitude"])

                if len(map_data) > 0:
                    st.subheader("Map Preview")
                    st.markdown(
                        "Sanity-check your results — points should cluster where you expect. "
                        "Hover over a point to see the address and coordinates."
                    )

                    centre_lat = map_data["Latitude"].mean()
                    centre_lng = map_data["Longitude"].mean()

                    lat_range = map_data["Latitude"].max() - map_data["Latitude"].min()
                    lng_range = map_data["Longitude"].max() - map_data["Longitude"].min()
                    spread = max(lat_range, lng_range)
                    if spread < 0.01:
                        zoom = 14
                    elif spread < 0.1:
                        zoom = 11
                    elif spread < 1:
                        zoom = 8
                    elif spread < 10:
                        zoom = 5
                    else:
                        zoom = 2

                    point_size = st.slider(
                        "Point size (pixels)", min_value=2, max_value=20, value=6, step=1,
                        help="Adjust how large each point appears on the map."
                    )

                    layer = pdk.Layer(
                        "ScatterplotLayer",
                        data=map_data,
                        get_position=["Longitude", "Latitude"],
                        get_radius=100,
                        radius_min_pixels=point_size,
                        radius_max_pixels=point_size * 3,
                        get_fill_color=[65, 105, 225, 180],
                        pickable=True,
                        auto_highlight=True,
                    )

                    tooltip = {
                        "html": "<b>{StreetAddress}</b><br/>Lat: {Latitude}<br/>Lng: {Longitude}",
                        "style": {"backgroundColor": "#1a1a2e", "color": "white", "fontSize": "12px"},
                    }

                    deck = pdk.Deck(
                        layers=[layer],
                        initial_view_state=pdk.ViewState(
                            latitude=centre_lat, longitude=centre_lng, zoom=zoom, pitch=0
                        ),
                        tooltip=tooltip,
                        map_provider="carto",
                        map_style="light",
                    )

                    st.pydeck_chart(deck)
                else:
                    st.warning("No rows have valid coordinates to display on the map.")

                if not skip_existing:
                    if comparison_df is not None and len(comparison_df) > 0:
                        st.subheader("Comparison Report")
                        st.markdown(
                            "These rows already had coordinates before geocoding. "
                            "The table below compares the original values with the newly geocoded ones, "
                            "including the distance between them in metres."
                        )
                        st.dataframe(comparison_df, use_container_width=True)

                        comp_csv = comparison_df.to_csv(index=False)
                        st.download_button(
                            label="📥 Download comparison report",
                            data=comp_csv,
                            file_name="geocode_comparison_report.csv",
                            mime="text/csv",
                            key="dl_comparison",
                        )
                    else:
                        st.info(
                            "ℹ️ **No comparison report generated.** "
                            "None of the rows had existing coordinates before geocoding, "
                            "so there is nothing to compare against. The comparison report "
                            "is only produced when you re-geocode rows that already had "
                            "Latitude and Longitude values."
                        )

else:
    st.markdown(
        """
        ### How to use
        1. Paste your **Google Geocoding API key** in the sidebar.
        2. Upload a file — CSV, Excel (.xlsx / .xls), or delimited text (.txt / .tsv).
        3. **Select the header row** — pick which row contains your column names.
        4. **Map your columns** — match your file's columns to: `AddressID`, `StreetAddress`, `Latitude`, `Longitude`, `CityName`, `Admin2Name`, `Admin1Name`, `PostalCode`, `CountryCode`.
        5. Review the **validation report** and fix any issues if needed.
        6. Click **Geocode** — the app deduplicates addresses and only calls the API once per unique address.
        7. Download the results. If you re-geocoded existing coordinates, a **comparison report** is also available.

        Any additional columns in your file are preserved as-is in the output.
        """
    )