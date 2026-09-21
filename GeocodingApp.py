import streamlit as st
import pandas as pd
import requests
import time
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
skip_existing = st.sidebar.checkbox("Skip rows that already have valid coordinates", value=True)

# Ark Google Cloud project quota: 6,000 Geocoding v3 requests/minute (100/sec).
# Default to 95/sec to leave a small safety margin for quota-window jitter and other users of the same project.
GOOGLE_QPM_QUOTA = 6000
GOOGLE_RPS_HARD_LIMIT = GOOGLE_QPM_QUOTA // 60
GOOGLE_RPS_DEFAULT = 95

target_rps = st.sidebar.slider(
    "Google requests per second", 5, 99, GOOGLE_RPS_DEFAULT, 1,
    help=(
        f"Ark quota is {GOOGLE_QPM_QUOTA:,} requests/minute ({GOOGLE_RPS_HARD_LIMIT}/sec). "
        "The default 95/sec leaves a small safety margin below the project limit."
    )
)
max_workers = st.sidebar.slider(
    "Concurrent workers", 8, 128, 96, 8,
    help=(
        "Number of address jobs processed concurrently. The 96-worker default is sized to keep the "
        "95 requests/sec limiter busy even when Google responses take around a second. "
        "API calls are still capped by the requests/sec setting."
    )
)
st.sidebar.caption(
    f"Approved Geocoding v3 quota: {GOOGLE_QPM_QUOTA:,} requests/minute. "
    f"Recommended operating rate: {GOOGLE_RPS_DEFAULT}/sec."
)

st.sidebar.header("Cache")
if "_geocode_cache" not in st.session_state:
    st.session_state["_geocode_cache"] = {}
if "_confirm_clear" not in st.session_state:
    st.session_state["_confirm_clear"] = False
if "_last_geocode_results" not in st.session_state:
    st.session_state["_last_geocode_results"] = None
if "_last_geocode_comparison" not in st.session_state:
    st.session_state["_last_geocode_comparison"] = None
if "_last_geocode_skip_existing" not in st.session_state:
    st.session_state["_last_geocode_skip_existing"] = True

cache_display = st.sidebar.empty()
cache_display.caption(f"**{len(st.session_state['_geocode_cache'])}** addresses cached this session.")

if not st.session_state["_confirm_clear"]:
    if st.sidebar.button("🗑️ Clear cache"):
        st.session_state["_confirm_clear"] = True
        st.rerun()
else:
    st.sidebar.warning("Are you sure? This cannot be undone.")
    col_yes, col_no = st.sidebar.columns(2)
    with col_yes:
        if st.button("Yes, clear", type="primary"):
            st.session_state["_geocode_cache"] = {}
            st.session_state["_confirm_clear"] = False
            st.rerun()
    with col_no:
        if st.button("Cancel"):
            st.session_state["_confirm_clear"] = False
            st.rerun()

CORE_FIELDS = ["StreetAddress"]
COORD_FIELDS = ["Latitude", "Longitude"]
OPTIONAL_ID = "AddressID"
LOCATION_FIELDS = ["CityName", "Admin2Name", "Admin1Name", "PostalCode", "CountryCode"]
ALL_FIELDS = [OPTIONAL_ID] + CORE_FIELDS + COORD_FIELDS + LOCATION_FIELDS
UNMAPPED = "-- Not mapped --"

AU_POSTCODE_STATE = {
    "1": "New South Wales", "2": "New South Wales", "3": "Victoria",
    "4": "Queensland", "5": "South Australia", "6": "Western Australia",
    "7": "Tasmania", "0": "Northern Territory",
}

LOCATION_TYPE_RANK = {"ROOFTOP": 4, "RANGE_INTERPOLATED": 3, "GEOMETRIC_CENTER": 2, "APPROXIMATE": 1}

CACHE_VERSION = "v2-fast-soft-country"
_thread_local = threading.local()


class RateLimiter:
    """Thread-safe fixed-rate limiter used per actual Google request."""
    def __init__(self, requests_per_second):
        self.interval = 1.0 / max(float(requests_per_second), 0.1)
        self.lock = threading.Lock()
        self.next_allowed = time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            wait_for = max(0.0, self.next_allowed - now)
            self.next_allowed = max(now, self.next_allowed) + self.interval
        if wait_for > 0:
            time.sleep(wait_for)


def _http_session():
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


def clean_text(value):
    if value is None or pd.isna(value):
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text)


def valid_coordinate_pair(lat, lng):
    try:
        lat = float(lat)
        lng = float(lng)
        return -90 <= lat <= 90 and -180 <= lng <= 180
    except (TypeError, ValueError):
        return False


def result_component(result, comp_type):
    for comp in result.get("address_components", []):
        if comp_type in comp.get("types", []):
            return comp.get("long_name", ""), comp.get("short_name", "")
    return "", ""


def postal_equivalent(a, b):
    a, b = clean_text(a).replace(" ", "").upper(), clean_text(b).replace(" ", "").upper()
    if not a or not b:
        return False
    return a == b or a.lstrip("0") == b.lstrip("0")


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def score_result_against_constraints(result, country_code=None, admin1=None, admin2=None, city=None, postal_code=None):
    score = 0
    components = result.get("address_components", [])
    def get_comp(comp_type):
        for c in components:
            if comp_type in c.get("types", []):
                return c.get("long_name", "").lower(), c.get("short_name", "").lower()
        return "", ""
    if country_code:
        _, short = get_comp("country")
        if short == country_code.lower():
            score += 10
    if admin1:
        ln, sn = get_comp("administrative_area_level_1")
        a1 = admin1.lower()
        if a1 == ln or a1 == sn or ln in a1 or a1 in ln:
            score += 5
    if admin2:
        ln, sn = get_comp("administrative_area_level_2")
        a2 = admin2.lower()
        if a2 == ln or a2 == sn or ln in a2 or a2 in ln:
            score += 3
    if city:
        ln, sn = get_comp("locality")
        ct = city.lower()
        if ct == ln or ct == sn or ln in ct or ct in ln:
            score += 3
    if postal_code:
        ln, sn = get_comp("postal_code")
        pc = postal_code.lower()
        if pc == ln or pc == sn or ln.startswith(pc) or pc.startswith(ln):
            score += 4
    return score


def single_geocode_call(address, key, components=None, country_code=None, admin1=None,
                        admin2=None, city=None, postal_code=None, limiter=None, retries=2):
    """One Google request with bounded retries and rich provenance."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": key}
    if components:
        params["components"] = components

    last_status = "UNKNOWN"
    last_error = ""
    for attempt in range(retries + 1):
        if limiter:
            limiter.wait()
        try:
            resp = _http_session().get(url, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "UNKNOWN")
            last_status = status
            last_error = clean_text(data.get("error_message", ""))

            if status == "OK" and data.get("results"):
                scored = []
                for pos, result in enumerate(data["results"]):
                    loc_type = result.get("geometry", {}).get("location_type", "UNKNOWN")
                    precision = LOCATION_TYPE_RANK.get(loc_type, 0)
                    match_score = score_result_against_constraints(
                        result, country_code, admin1, admin2, city, postal_code)
                    # Stable tie-break keeps Google's ranking when our scores are equal.
                    scored.append((match_score, precision, -pos, result))
                scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
                match_score, _, _, best = scored[0]
                loc = best.get("geometry", {}).get("location", {})
                loc_type = best.get("geometry", {}).get("location_type", "UNKNOWN")
                _, google_country = result_component(best, "country")
                google_postal, _ = result_component(best, "postal_code")
                return {
                    "lat": loc.get("lat"), "lng": loc.get("lng"), "status": "OK",
                    "location_type": loc_type, "match_score": match_score,
                    "formatted_address": best.get("formatted_address", ""),
                    "place_id": best.get("place_id", ""),
                    "google_country": google_country.upper(),
                    "google_postal": google_postal,
                    "partial_match": bool(best.get("partial_match", False)),
                    "error_message": "",
                }

            # Quota/transient statuses are worth retrying. ZERO_RESULTS is not.
            if status in {"OVER_QUERY_LIMIT", "UNKNOWN_ERROR"} and attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return {
                "lat": None, "lng": None, "status": status, "location_type": None,
                "match_score": 0, "formatted_address": "", "place_id": "",
                "google_country": "", "google_postal": "", "partial_match": False,
                "error_message": last_error,
            }
        except requests.Timeout:
            last_status, last_error = "TIMEOUT", "Google request timed out."
        except requests.ConnectionError:
            last_status, last_error = "CONNECTION_ERROR", "Could not connect to Google."
        except requests.RequestException as exc:
            last_status, last_error = "HTTP_ERROR", str(exc)
        except Exception as exc:
            last_status, last_error = "ERROR", str(exc)

        if attempt < retries:
            time.sleep(0.5 * (2 ** attempt))

    return {
        "lat": None, "lng": None, "status": last_status, "location_type": None,
        "match_score": 0, "formatted_address": "", "place_id": "",
        "google_country": "", "google_postal": "", "partial_match": False,
        "error_message": last_error,
    }


def _query_parts(*values):
    parts = []
    seen = set()
    for value in values:
        value = clean_text(value)
        if value and value.casefold() not in seen:
            parts.append(value)
            seen.add(value.casefold())
    return ", ".join(parts)


def _quality_flags(geo, source_country, source_postal, has_street):
    flags = []
    gc = clean_text(geo.get("google_country")).upper()
    sc = clean_text(source_country).upper()
    gp = clean_text(geo.get("google_postal"))
    sp = clean_text(source_postal)
    if sc and gc and sc != gc:
        flags.append(f"Source country {sc} differs from Google country {gc}")
    if sp and gp and sp != gp:
        if postal_equivalent(sp, gp):
            flags.append(f"Postal code normalised {sp} -> {gp}")
        else:
            flags.append(f"Source postal code {sp} differs from Google {gp}")
    if geo.get("partial_match"):
        flags.append("Google returned a partial match")
    if not has_street:
        flags.append("No street address supplied; result is locality/postcode based")
    if geo.get("location_type") in {"GEOMETRIC_CENTER", "APPROXIMATE"}:
        flags.append(f"Low precision: {geo.get('location_type')}")
    return " | ".join(flags)


def geocode_address(full_address, street_address, key, country_code=None, city=None,
                    admin1=None, admin2=None, postal_code=None, limiter=None):
    """Fast soft-geography strategy.

    Primary search deliberately does NOT hard-constrain source country/admin data. Dirty
    RMS exports can therefore still resolve. Fallbacks run only for weak/failed results.
    """
    full_address = clean_text(full_address)
    street_address = clean_text(street_address)
    city, admin1, admin2, postal_code = map(clean_text, (city, admin1, admin2, postal_code))
    country_code = clean_text(country_code).upper()

    score_kwargs = {
        # Source country is intentionally excluded from result scoring. It is audited later.
        "country_code": None,
        "admin1": admin1 or None,
        "admin2": admin2 or None,
        "city": city or None,
        "postal_code": postal_code or None,
    }

    queries = []
    if full_address:
        queries.append(("primary", full_address))

    # For weak results, drop noisier admin fields while keeping locality/postcode context.
    compact = _query_parts(street_address, city, postal_code)
    if compact and compact != full_address:
        queries.append(("compact", compact))
    if street_address and street_address not in {q for _, q in queries}:
        queries.append(("street_only", street_address))

    # Rows without a street are still worth resolving at locality/postcode level.
    if not street_address:
        locality = _query_parts(city, admin2, admin1, postal_code)
        if locality and locality not in {q for _, q in queries}:
            queries.append(("locality_postcode", locality))

    if not queries:
        return {
            "lat": None, "lng": None, "status": "NO_SEARCHABLE_ADDRESS", "location_type": None,
            "method": "failed", "fallback": False, "detail": "No usable address/geography fields.",
            "api_calls": 0, "formatted_address": "", "place_id": "", "google_country": "",
            "google_postal": "", "partial_match": False, "quality_flag": "No searchable address",
            "error_message": "",
        }

    candidates = []
    api_calls = 0
    statuses = []

    for idx, (method, query) in enumerate(queries):
        r = single_geocode_call(query, key, limiter=limiter, **score_kwargs)
        api_calls += 1
        statuses.append(r.get("status", "UNKNOWN"))
        if r.get("status") == "OK":
            candidates.append((method, r))
            # Strong first-pass results stop immediately. RANGE_INTERPOLATED is intentionally
            # accepted as operationally useful; weaker/partial results get one or more fallbacks.
            if idx == 0 and r.get("location_type") in {"ROOFTOP", "RANGE_INTERPOLATED"} and not r.get("partial_match"):
                break
            if method != "primary" and r.get("location_type") == "ROOFTOP" and not r.get("partial_match"):
                break
        # If the primary failed, continue. If it succeeded weakly, fallbacks may improve it.

    if not candidates:
        final_status = next((x for x in reversed(statuses) if x not in {"ZERO_RESULTS", "UNKNOWN"}), statuses[-1] if statuses else "ZERO_RESULTS")
        detail = diagnose_failure(full_address, city, admin1, admin2, postal_code, country_code)
        return {
            "lat": None, "lng": None, "status": final_status, "location_type": None,
            "method": "failed", "fallback": False, "detail": detail, "api_calls": api_calls,
            "formatted_address": "", "place_id": "", "google_country": "", "google_postal": "",
            "partial_match": False, "quality_flag": "Geocoding failed",
            "error_message": "",
        }

    # Prefer geography consistency first, then precision. This prevents a stray rooftop result
    # from beating a lower-precision result that actually matches the supplied city/postcode.
    best_method, best = max(
        candidates,
        key=lambda item: (item[1].get("match_score", 0), LOCATION_TYPE_RANK.get(item[1].get("location_type"), 0))
    )
    flag = _quality_flags(best, country_code, postal_code, bool(street_address))
    detail_parts = []
    if best_method != "primary":
        detail_parts.append(f"Resolved via {best_method} fallback.")
    if flag:
        detail_parts.append(flag)
    return {
        **best,
        "method": best_method,
        "fallback": best_method != "primary",
        "addr_only_better": best_method == "street_only",
        "detail": " ".join(detail_parts),
        "quality_flag": flag,
        "api_calls": api_calls,
    }


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
        if not city_words.intersection(set(addr_parts)) and len(address) > 20:
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
        "streetaddress": ["street", "streetname", "street_name", "address", "addr", "street address", "locationaddress"],
        "addressid": ["id", "address_id", "addr_id", "locid", "location_id", "locnum", "locationid"],
        "latitude": ["lat", "y"],
        "longitude": ["lng", "lon", "long", "x"],
        "countrycode": ["country", "countryiso", "iso2", "iso2a", "country_code", "cntry", "country code", "cntrycode"],
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


# =============================================================================
# FILE READING
# =============================================================================

def _read_delimited_with_encodings(uploaded_file, sep=","):
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, header=None, dtype=str, sep=sep, encoding=encoding, keep_default_na=False), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    raise last_error or UnicodeDecodeError("unknown", b"", 0, 1, "Could not decode file")


def read_raw_file(uploaded_file):
    name = uploaded_file.name.lower()
    try:
        used_encoding = None
        if name.endswith(".csv"):
            df, used_encoding = _read_delimited_with_encodings(uploaded_file, ",")
        elif name.endswith((".xlsx", ".xls")):
            uploaded_file.seek(0)
            try:
                df = pd.read_excel(uploaded_file, header=None, dtype=str, engine="openpyxl", keep_default_na=False)
            except Exception:
                uploaded_file.seek(0)
                df = pd.read_excel(uploaded_file, header=None, dtype=str, keep_default_na=False)
        elif name.endswith((".txt", ".tsv")):
            uploaded_file.seek(0)
            sample_bytes = uploaded_file.read(8192)
            sample = ""
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    sample = sample_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            uploaded_file.seek(0)
            counts = {"\t": sample.count("\t"), "|": sample.count("|"), ";": sample.count(";"), ",": sample.count(",")}
            sep = max(counts, key=counts.get) if max(counts.values()) > 0 else ","
            df, used_encoding = _read_delimited_with_encodings(uploaded_file, sep)
        else:
            return None, f"Unsupported file type: **{name.split('.')[-1]}**."
        if df is None or df.empty:
            return None, "File contains no data."
        if len(df.columns) < 2:
            return None, "Only one column detected — wrong delimiter?"
        df.attrs["encoding"] = used_encoding or "n/a"
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


# =============================================================================
# VALIDATION
# =============================================================================

def validate_dataframe(df, skip):
    """Fast preflight. Flags are informative; they do not force a human review gate."""
    warnings = []
    total_rows = len(df)

    street = df["StreetAddress"].map(clean_text)
    city = df["CityName"].map(clean_text) if "CityName" in df.columns else pd.Series("", index=df.index)
    admin1 = df["Admin1Name"].map(clean_text) if "Admin1Name" in df.columns else pd.Series("", index=df.index)
    admin2 = df["Admin2Name"].map(clean_text) if "Admin2Name" in df.columns else pd.Series("", index=df.index)
    postal = df["PostalCode"].map(clean_text) if "PostalCode" in df.columns else pd.Series("", index=df.index)
    searchable = (street != "") | (city != "") | (admin1 != "") | (admin2 != "") | (postal != "")
    blank_street = street == ""

    lat_num = pd.to_numeric(df["Latitude"], errors="coerce")
    lng_num = pd.to_numeric(df["Longitude"], errors="coerce")
    coord_valid = lat_num.between(-90, 90) & lng_num.between(-180, 180)
    any_coord_text = df["Latitude"].map(clean_text).ne("") | df["Longitude"].map(clean_text).ne("")
    invalid_coords = any_coord_text & ~coord_valid

    if blank_street.any():
        warnings.append(f"{int(blank_street.sum()):,} row(s) have no street address; locality/postcode geocoding will be attempted automatically.")
    if invalid_coords.any():
        warnings.append(f"{int(invalid_coords.sum()):,} row(s) have incomplete/invalid existing coordinates; they will be re-geocoded even when skip-existing is enabled.")
    unsearchable = ~searchable
    if unsearchable.any():
        warnings.append(f"{int(unsearchable.sum()):,} row(s) contain no usable address/geography fields and cannot be geocoded.")

    # Country codes are soft evidence only. A dominant code is reported, not trusted as a hard filter.
    cc = df["CountryCode"].map(clean_text).str.upper() if "CountryCode" in df.columns else pd.Series("", index=df.index)
    nonblank_cc = cc[cc != ""]
    if len(nonblank_cc):
        top_cc = nonblank_cc.value_counts().index[0]
        top_n = int((nonblank_cc == top_cc).sum())
        if top_n / len(nonblank_cc) >= 0.95:
            warnings.append(f"Source country is overwhelmingly {top_cc} ({top_n:,}/{len(nonblank_cc):,}). Country is treated as a soft audit field, not a hard Google constraint.")

    needs = searchable & (~coord_valid if skip else True)
    keys = pd.DataFrame({
        "street": street, "city": city, "admin1": admin1, "admin2": admin2, "postal": postal
    }, index=df.index)
    unique_jobs = int(keys.loc[needs].drop_duplicates().shape[0])

    return {
        "errors": [], "warnings": warnings, "flagged_rows": {},
        "stats": {
            "total_rows": total_rows,
            "unique_addresses": int(keys.loc[searchable].drop_duplicates().shape[0]),
            "blank_addresses": int(blank_street.sum()),
            "already_geocoded": int((searchable & coord_valid).sum()),
            "to_geocode": unique_jobs,
            "unsearchable": int(unsearchable.sum()),
        }
    }


# =============================================================================
# GEOCODING
# =============================================================================

def process_dataframe(df, key, target_rps, max_workers, skip):
    result = df.copy()
    result["Latitude"] = pd.to_numeric(result["Latitude"], errors="coerce")
    result["Longitude"] = pd.to_numeric(result["Longitude"], errors="coerce")

    for col in ["StreetAddress", "CityName", "Admin2Name", "Admin1Name", "PostalCode", "CountryCode"]:
        if col not in result.columns:
            result[col] = ""
        result[col] = result[col].map(clean_text)

    result["_street"] = result["StreetAddress"]
    result["_city"] = result["CityName"]
    result["_admin1"] = result["Admin1Name"]
    result["_admin2"] = result["Admin2Name"]
    result["_postal"] = result["PostalCode"]
    result["_cc"] = result["CountryCode"].str.upper()

    def build_addr(row):
        return _query_parts(row["_street"], row["_city"], row["_admin2"], row["_admin1"], row["_postal"])

    result["_full_addr"] = result.apply(build_addr, axis=1)
    searchable = result["_full_addr"].ne("")
    coord_valid = result.apply(lambda r: valid_coordinate_pair(r["Latitude"], r["Longitude"]), axis=1)

    had_coords = searchable & coord_valid
    orig_lats = result.loc[had_coords, "Latitude"].copy()
    orig_lngs = result.loc[had_coords, "Longitude"].copy()
    needs = searchable & (~coord_valid if skip else True)

    # Explicit status prevents skipped coordinates from being reported as failed geocodes.
    result["GoogleLocationType"] = ""
    result["GeoMethod"] = ""
    result["GeoStatus"] = ""
    result["GeoAttempts"] = 0
    result["GeoQualityFlag"] = ""
    result["GoogleFormattedAddress"] = ""
    result["GooglePlaceID"] = ""
    result["GoogleCountryCode"] = ""
    result["GooglePostalCode"] = ""
    result["GooglePartialMatch"] = False
    result["AddrOnlyBetter"] = False
    result.loc[had_coords & skip, "GeoStatus"] = "EXISTING_SKIPPED"
    result.loc[~searchable, "GeoStatus"] = "NO_SEARCHABLE_ADDRESS"

    job_cols = ["_full_addr", "_street", "_city", "_admin1", "_admin2", "_postal", "_cc"]
    geo_sub = result.loc[needs, job_cols].copy()
    # Country is not part of the query identity. Same address gets one Google job even if source country varies.
    dedupe_cols = ["_full_addr", "_street", "_city", "_admin1", "_admin2", "_postal"]
    jobs = geo_sub.drop_duplicates(subset=dedupe_cols).to_dict("records")
    total = len(jobs)

    if total == 0:
        st.info("Nothing to geocode.")
        return result.drop(columns=job_cols), None

    with st.expander("Sample primary searches"):
        for job in jobs[:5]:
            st.text(job["_full_addr"])

    limiter = RateLimiter(target_rps)
    with st.spinner("Validating API key…"):
        test = single_geocode_call("10 Downing Street, London", key, limiter=limiter, retries=0)
        if test["status"] == "REQUEST_DENIED":
            msg = test.get("error_message") or "Google rejected the API key."
            st.error(f"API key rejected: {msg}")
            return result.drop(columns=job_cols), None

    prog = st.progress(0, text=f"Starting {total:,} unique address jobs…")
    status_area = st.empty()
    geo_cache = st.session_state["_geocode_cache"]
    local_results = {}
    cache_hits = 0
    total_api_calls = 0
    completed = 0
    failures = 0
    fallbacks = 0

    def cache_key(job):
        return (CACHE_VERSION, job["_full_addr"], job["_street"], job["_city"], job["_admin1"], job["_admin2"], job["_postal"])

    pending = []
    for job in jobs:
        ck = cache_key(job)
        cached = geo_cache.get(ck)
        if cached and cached.get("status") == "OK":
            local_results[ck] = cached
            cache_hits += 1
            completed += 1
        else:
            pending.append((ck, job))

    def run_job(job):
        return geocode_address(
            job["_full_addr"], job["_street"], key,
            country_code=job.get("_cc") or None,
            city=job.get("_city") or None,
            admin1=job.get("_admin1") or None,
            admin2=job.get("_admin2") or None,
            postal_code=job.get("_postal") or None,
            limiter=limiter,
        )

    if completed:
        prog.progress(completed / total, text=f"{completed:,} of {total:,} complete ({cache_hits:,} cached)…")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(run_job, job): (ck, job) for ck, job in pending}
        last_ui_update = time.monotonic()
        for fut in as_completed(future_map):
            ck, job = future_map[fut]
            try:
                geo = fut.result()
            except Exception as exc:
                geo = {
                    "lat": None, "lng": None, "status": "WORKER_ERROR", "location_type": None,
                    "method": "failed", "fallback": False, "detail": str(exc), "api_calls": 0,
                    "formatted_address": "", "place_id": "", "google_country": "", "google_postal": "",
                    "partial_match": False, "quality_flag": "Worker error", "error_message": str(exc),
                }
            local_results[ck] = geo
            total_api_calls += int(geo.get("api_calls", 0))
            if geo.get("status") == "OK":
                # Cache successes only. Transient failures never poison the rest of the session.
                geo_cache[ck] = geo
                if geo.get("fallback"):
                    fallbacks += 1
            else:
                failures += 1
            completed += 1

            now = time.monotonic()
            if now - last_ui_update >= 0.15 or completed == total:
                prog.progress(completed / total, text=f"Geocoding {completed:,} of {total:,} unique jobs…")
                status_area.caption(
                    f"{completed:,}/{total:,} complete | {total_api_calls:,} Google calls | "
                    f"{cache_hits:,} cached | {failures:,} failed so far"
                )
                last_ui_update = now

    prog.progress(1.0, text="Geocoding complete")

    # Apply unique Google results back to source rows with a hash-based lookup.
    # The previous implementation scanned the full source DataFrame once per unique job, which became
    # catastrophically slow on large portfolios (25k jobs x 45k rows). This is O(rows + jobs) instead.
    unique_jobs = geo_sub.drop_duplicates(subset=dedupe_cols)
    lookup_rows = []
    geo_by_key = {}
    for _, job in unique_jobs.iterrows():
        job_dict = job.to_dict()
        ck = cache_key(job_dict)
        geo = local_results.get(ck)
        if not geo:
            continue
        key_tuple = tuple(job_dict[c] for c in dedupe_cols)
        geo_by_key[key_tuple] = geo
        lookup_rows.append({
            **{c: job_dict[c] for c in dedupe_cols},
            "_GeoStatus": geo.get("status", ""),
            "_GeoAttempts": int(geo.get("api_calls", 0)),
            "_GeoMethod": geo.get("method", ""),
            "_GoogleLocationType": geo.get("location_type") or "",
            "_GoogleFormattedAddress": geo.get("formatted_address", ""),
            "_GooglePlaceID": geo.get("place_id", ""),
            "_GoogleCountryCode": geo.get("google_country", ""),
            "_GooglePostalCode": geo.get("google_postal", ""),
            "_GooglePartialMatch": bool(geo.get("partial_match", False)),
            "_AddrOnlyBetter": bool(geo.get("addr_only_better", False)),
            "_GeoLat": geo.get("lat"),
            "_GeoLng": geo.get("lng"),
        })

    if lookup_rows:
        lookup = pd.DataFrame(lookup_rows).set_index(dedupe_cols)
        target_rows = result.loc[needs, dedupe_cols]
        target_keys = pd.MultiIndex.from_frame(target_rows)
        matched = lookup.reindex(target_keys)
        matched.index = target_rows.index

        result.loc[needs, "GeoStatus"] = matched["_GeoStatus"].fillna("").values
        result.loc[needs, "GeoAttempts"] = matched["_GeoAttempts"].fillna(0).astype(int).values
        result.loc[needs, "GeoMethod"] = matched["_GeoMethod"].fillna("").values
        result.loc[needs, "GoogleLocationType"] = matched["_GoogleLocationType"].fillna("").values
        result.loc[needs, "GoogleFormattedAddress"] = matched["_GoogleFormattedAddress"].fillna("").values
        result.loc[needs, "GooglePlaceID"] = matched["_GooglePlaceID"].fillna("").values
        result.loc[needs, "GoogleCountryCode"] = matched["_GoogleCountryCode"].fillna("").values
        result.loc[needs, "GooglePostalCode"] = matched["_GooglePostalCode"].fillna("").values
        result.loc[needs, "GooglePartialMatch"] = matched["_GooglePartialMatch"].fillna(False).astype(bool).values
        result.loc[needs, "AddrOnlyBetter"] = matched["_AddrOnlyBetter"].fillna(False).astype(bool).values

        ok_coords = (
            matched["_GeoStatus"].eq("OK")
            & pd.to_numeric(matched["_GeoLat"], errors="coerce").notna()
            & pd.to_numeric(matched["_GeoLng"], errors="coerce").notna()
        )
        ok_idx = matched.index[ok_coords]
        result.loc[ok_idx, "Latitude"] = matched.loc[ok_idx, "_GeoLat"].values
        result.loc[ok_idx, "Longitude"] = matched.loc[ok_idx, "_GeoLng"].values

        # Quality flags depend on source-country/postcode evidence, so calculate them once per source row.
        # This is at most one pass over the rows being geocoded, rather than one full scan per unique job.
        for idx in target_rows.index:
            key_tuple = tuple(result.at[idx, c] for c in dedupe_cols)
            geo = geo_by_key.get(key_tuple)
            if geo:
                result.at[idx, "GeoQualityFlag"] = _quality_flags(
                    geo, result.at[idx, "_cc"], result.at[idx, "_postal"], bool(result.at[idx, "_street"])
                )

    succeeded = sum(1 for g in local_results.values() if g.get("status") == "OK")
    failed = total - succeeded
    status_area.markdown(
        f"**{succeeded:,}** unique locations geocoded, **{failed:,}** failed. "
        f"**{total_api_calls:,}** Google calls, **{cache_hits:,}** cache hits, "
        f"**{fallbacks:,}** fallback resolutions. **{len(result):,}** source rows."
    )

    # Comparison report before internal columns are removed.
    comp = None
    if not skip:
        cidx = had_coords[had_coords].index
        if len(cidx) > 0:
            comp = pd.DataFrame({
                "AddressID": result.loc[cidx, "AddressID"].values,
                "StreetAddress": result.loc[cidx, "StreetAddress"].values,
                "Original_Latitude": orig_lats.values,
                "Original_Longitude": orig_lngs.values,
                "New_Latitude": result.loc[cidx, "Latitude"].values,
                "New_Longitude": result.loc[cidx, "Longitude"].values,
                "GoogleLocationType": result.loc[cidx, "GoogleLocationType"].values,
                "GeoMethod": result.loc[cidx, "GeoMethod"].values,
                "GeoQualityFlag": result.loc[cidx, "GeoQualityFlag"].values,
            })
            dists = []
            for _, r in comp.iterrows():
                if valid_coordinate_pair(r["Original_Latitude"], r["Original_Longitude"]) and valid_coordinate_pair(r["New_Latitude"], r["New_Longitude"]):
                    dists.append(round(haversine_m(float(r["Original_Latitude"]), float(r["Original_Longitude"]),
                                                   float(r["New_Latitude"]), float(r["New_Longitude"])), 2))
                else:
                    dists.append(None)
            comp["Distance_m"] = dists

    return result.drop(columns=job_cols), comp


# =============================================================================
# RECOMMENDATIONS
# =============================================================================

def build_recommendations(result_df, comparison_df=None, has_tiv=False, total_tiv=0):
    distance_lookup = {}
    if comparison_df is not None and len(comparison_df) > 0:
        for _, row in comparison_df.iterrows():
            aid = clean_text(row.get("AddressID", ""))
            dist = row.get("Distance_m")
            if aid and pd.notna(dist):
                distance_lookup[aid] = float(dist)

    recs = []
    for _, row in result_df.iterrows():
        geo_status = clean_text(row.get("GeoStatus", ""))
        if geo_status == "EXISTING_SKIPPED":
            continue

        addr_id = clean_text(row.get("AddressID", ""))
        street = clean_text(row.get("StreetAddress", ""))
        loc_type = clean_text(row.get("GoogleLocationType", ""))
        method = clean_text(row.get("GeoMethod", ""))
        quality_flag = clean_text(row.get("GeoQualityFlag", ""))
        lat = row.get("Latitude")
        lng = row.get("Longitude")

        tiv = float(row.get("_TIV", 0)) if has_tiv and pd.notna(row.get("_TIV")) else None
        pct = (tiv / total_tiv * 100) if tiv and total_tiv > 0 else None
        tiv_note = f" Location TIV: {tiv:,.0f} ({pct:.2f}% of portfolio)." if tiv and tiv > 0 else ""

        dist_m = distance_lookup.get(addr_id)
        dist_km = dist_m / 1000 if dist_m is not None else None
        distance_flag = "red" if dist_km is not None and dist_km >= 50 else "orange" if dist_km is not None and dist_km >= 5 else None

        rec_entry = {
            "AddressID": addr_id, "StreetAddress": street,
            "GoogleLocationType": loc_type, "GeoMethod": method,
            "GeoStatus": geo_status, "GeoQualityFlag": quality_flag,
            "Distance_km": dist_km, "TIV": tiv, "TIV_Pct": pct,
        }

        if geo_status == "NO_SEARCHABLE_ADDRESS":
            rec_entry.update({
                "Category": "Failed - no usable geography", "Priority": 1,
                "Recommendation": "No street, city, admin or postcode information was available to search." + tiv_note,
            })
            recs.append(rec_entry)
        elif geo_status != "OK" or not valid_coordinate_pair(lat, lng):
            rec_entry.update({
                "Category": "Failed", "Priority": 1,
                "Recommendation": f"Geocoding failed ({geo_status or 'unknown status'}). Review only if the location matters materially." + tiv_note,
            })
            recs.append(rec_entry)
        elif distance_flag == "red":
            rec_entry.update({
                "Category": "Large coordinate discrepancy", "Priority": 1,
                "Recommendation": f"New coordinates are {dist_km:,.1f}km from the original. Verify which location is correct." + tiv_note,
            })
            recs.append(rec_entry)
        elif loc_type == "APPROXIMATE":
            rec_entry.update({
                "Category": "Very low precision", "Priority": 2,
                "Recommendation": "Google returned an approximate locality/region-level point. Use only with appropriate caution for catastrophe modelling." + tiv_note,
            })
            recs.append(rec_entry)
        elif distance_flag == "orange":
            rec_entry.update({
                "Category": "Notable coordinate difference", "Priority": 3,
                "Recommendation": f"New coordinates are {dist_km:,.1f}km from the original. Check if the location is material." + tiv_note,
            })
            recs.append(rec_entry)
        elif loc_type in {"GEOMETRIC_CENTER", "RANGE_INTERPOLATED"}:
            meaning = "street/building geometry centre" if loc_type == "GEOMETRIC_CENTER" else "interpolated street position"
            rec_entry.update({
                "Category": "Below rooftop", "Priority": 4,
                "Recommendation": f"Google returned a {meaning}. {quality_flag}".strip() + tiv_note,
            })
            recs.append(rec_entry)
        elif quality_flag:
            rec_entry.update({
                "Category": "Rooftop - source data flag", "Priority": 5,
                "Recommendation": f"Rooftop coordinates achieved. {quality_flag}" + tiv_note,
            })
            recs.append(rec_entry)
        elif method != "primary":
            rec_entry.update({
                "Category": "Rooftop - fallback", "Priority": 5,
                "Recommendation": f"Rooftop coordinates achieved via {method} fallback." + tiv_note,
            })
            recs.append(rec_entry)
        # Clean ROOFTOP primary result with no flags: no review required.

    if not recs:
        return None
    return pd.DataFrame(recs).sort_values("Priority").reset_index(drop=True)


def apply_column_mapping(df, mapping):
    """Create canonical fields only from explicit mappings; preserve colliding source columns safely."""
    out = df.copy()
    for field in ALL_FIELDS:
        src = mapping.get(field, UNMAPPED)
        if field in out.columns and src != field:
            new_name = f"Source_{field}"
            n = 2
            while new_name in out.columns:
                new_name = f"Source_{field}_{n}"
                n += 1
            out = out.rename(columns={field: new_name})
    for field in ALL_FIELDS:
        src = mapping.get(field, UNMAPPED)
        if src != UNMAPPED and src in df.columns:
            out[field] = df[src].values
    return out


def render_geocode_results(res_df, comp_df, run_skip_existing):
    """Render saved results outside the Geocode button event so Streamlit reruns do not erase them."""
    if res_df is None:
        return

    st.subheader("Results")
    st.dataframe(res_df, use_container_width=True)
    st.download_button(
        "📥 Download geocoded CSV", res_df.to_csv(index=False),
        "geocoded_output.csv", "text/csv", key="dl_geocoded"
    )

    # Map preview
    md = res_df[["StreetAddress", "Latitude", "Longitude"]].copy()
    md["Latitude"] = pd.to_numeric(md["Latitude"], errors="coerce")
    md["Longitude"] = pd.to_numeric(md["Longitude"], errors="coerce")
    md = md.dropna(subset=["Latitude", "Longitude"])
    if len(md) > 10000:
        md = md.sample(10000, random_state=42)
        st.caption("Map preview sampled to 10,000 points for browser performance; downloaded results contain all rows.")
    if len(md) > 0:
        st.subheader("Map Preview")
        st.markdown("Hover over a point to see the address.")
        clat, clng = md["Latitude"].mean(), md["Longitude"].mean()
        sp = max(md["Latitude"].max() - md["Latitude"].min(),
                 md["Longitude"].max() - md["Longitude"].min())
        zm = 14 if sp < 0.01 else 11 if sp < 0.1 else 8 if sp < 1 else 5 if sp < 10 else 2
        ps = st.slider("Point size (pixels)", 2, 20, 6, 1, key="result_point_size")
        layer = pdk.Layer(
            "ScatterplotLayer", data=md, get_position=["Longitude", "Latitude"], get_radius=100,
            radius_min_pixels=ps, radius_max_pixels=ps * 3,
            get_fill_color=[65, 105, 225, 180], pickable=True, auto_highlight=True
        )
        tip = {
            "html": "<b>{StreetAddress}</b><br/>Lat: {Latitude}<br/>Lng: {Longitude}",
            "style": {"backgroundColor": "#1a1a2e", "color": "white", "fontSize": "12px"},
        }
        st.pydeck_chart(pdk.Deck(
            layers=[layer],
            initial_view_state=pdk.ViewState(latitude=clat, longitude=clng, zoom=zm, pitch=0),
            tooltip=tip, map_provider="carto", map_style="light"
        ))

    # Recommendations
    has_tiv = "_TIV" in res_df.columns
    total_tiv = res_df["_TIV"].sum() if has_tiv else 0
    rec_df = build_recommendations(res_df, comp_df, has_tiv=has_tiv, total_tiv=total_tiv)

    if rec_df is not None and len(rec_df) > 0:
        st.subheader("Step 5: Recommendations")
        if has_tiv and total_tiv > 0:
            flagged_tiv = rec_df["TIV"].fillna(0).sum()
            clean_tiv = total_tiv - flagged_tiv
            clean_pct = clean_tiv / total_tiv * 100
            st.markdown(
                f"**{clean_pct:.1f}%** of portfolio TIV (**{clean_tiv:,.0f}** of {total_tiv:,.0f}) "
                f"geocoded to ROOFTOP with no flags — no action needed on those."
            )

        st.markdown("The following rows need attention. Clean ROOFTOP primary results are not listed.")
        cat_counts = rec_df["Category"].value_counts()
        for cat in sorted(
            cat_counts.index,
            key=lambda x: rec_df.loc[rec_df["Category"] == x, "Priority"].iloc[0]
        ):
            cat_tiv = ""
            if has_tiv and "TIV" in rec_df.columns:
                cat_total = rec_df.loc[rec_df["Category"] == cat, "TIV"].fillna(0).sum()
                if cat_total > 0:
                    cat_pct = cat_total / total_tiv * 100 if total_tiv > 0 else 0
                    cat_tiv = f" — TIV: {cat_total:,.0f} ({cat_pct:.1f}% of portfolio)"
            st.markdown(f"- {cat}: **{cat_counts[cat]}** row(s){cat_tiv}")

        display_cols = [c for c in rec_df.columns if c != "Priority"]
        st.dataframe(rec_df[display_cols], use_container_width=True)
        st.download_button(
            "📥 Download recommendations CSV", rec_df[display_cols].to_csv(index=False),
            "geocode_recommendations.csv", "text/csv", key="dl_rec"
        )
    else:
        st.success("🎉 All addresses geocoded at ROOFTOP precision — no recommendations needed!")

    if not run_skip_existing:
        if comp_df is not None and len(comp_df) > 0:
            st.subheader("Comparison Report")
            st.markdown("Original vs newly geocoded coordinates, with distance and quality.")
            st.dataframe(comp_df, use_container_width=True)
            st.download_button(
                "📥 Download comparison report", comp_df.to_csv(index=False),
                "geocode_comparison_report.csv", "text/csv", key="dl_comp"
            )
        else:
            st.info("ℹ️ No comparison report — no rows had existing coordinates.")


# =============================================================================
# MAIN APP
# =============================================================================

uploaded = st.file_uploader("Upload your file", type=["csv", "xlsx", "xls", "txt", "tsv"])

if uploaded:
    raw_df, err = read_raw_file(uploaded)
    if err:
        st.error(err)
        st.stop()

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

    # Template presets
    TEMPLATES = {
        "Custom (map everything manually)": {
            "fields": ALL_FIELDS,
            "description": "Full control — map each field yourself."
        },
        "Terrorism 4020": {
            "fields": ["StreetAddress", "CityName", "PostalCode", "CountryCode"],
            "description": "StreetAddress, CityName, PostalCode, CountryCode → returns Lat/Lng + quality."
        },
        "Full RiskLink export": {
            "fields": [OPTIONAL_ID, "StreetAddress", "Latitude", "Longitude",
                       "CityName", "Admin2Name", "Admin1Name", "PostalCode", "CountryCode"],
            "description": "All fields including AddressID, coordinates, and admin levels."
        },
        "Coordinates only (re-geocode)": {
            "fields": ["StreetAddress", "Latitude", "Longitude", "CountryCode"],
            "description": "Re-geocode existing data — StreetAddress + existing coords + country."
        },
        "Minimal (street address only)": {
            "fields": ["StreetAddress"],
            "description": "Just the street address — geocode with no constraints."
        },
    }

    template = st.selectbox("Template", list(TEMPLATES.keys()), index=1,
                            help="Pick a preset to show only the fields your team uses, or choose Custom for full control.")
    st.caption(TEMPLATES[template]["description"])

    active_fields = TEMPLATES[template]["fields"]

    st.markdown("Match each field to a column from your file.")
    opts = [UNMAPPED] + avail
    mapping = {}

    # Always map active fields
    # Group them into rows of 3-4
    field_groups = [active_fields[i:i+4] for i in range(0, len(active_fields), 4)]
    for group in field_groups:
        cols = st.columns(len(group))
        for col_ui, field in zip(cols, group):
            idx = opts.index(guess_column(field, avail)) if guess_column(field, avail) in opts else 0
            with col_ui:
                label = f"{field}" if field in CORE_FIELDS else f"{field} (opt)"
                mapping[field] = st.selectbox(label, opts, idx, key=f"m_{field}")

    # Set unmapped for fields not in the active template
    for field in ALL_FIELDS:
        if field not in mapping:
            mapping[field] = UNMAPPED

    st.markdown("**Value fields (optional — for TIV context in recommendations):**")
    st.caption("Select columns with insured values. They will be summed into a Total Insured Value per row.")
    value_columns = st.multiselect("Value columns", options=avail, default=[], key="m_values")

    st.markdown("**Your column mappings:**")
    lines = []
    for f in ALL_FIELDS:
        s = mapping.get(f, UNMAPPED)
        lines.append(f"- **{f}** ← `{s}`" if s != UNMAPPED else f"- **{f}** ← *(not mapped)*")
    if value_columns:
        lines.append(f"- **TIV** ← `{', '.join(value_columns)}`")
    st.markdown("\n".join(lines))

    if mapping.get("StreetAddress") == UNMAPPED:
        st.warning("**StreetAddress** must be mapped to continue.")
        st.stop()

    mapped_optional = [f for f in LOCATION_FIELDS if mapping.get(f) != UNMAPPED]
    has_id = mapping.get(OPTIONAL_ID) != UNMAPPED
    has_coords = mapping.get("Latitude") != UNMAPPED and mapping.get("Longitude") != UNMAPPED

    if not mapped_optional:
        st.info("ℹ️ No location fields mapped — geocoding will rely on the street address alone.")
    if not has_id:
        st.info("ℹ️ No AddressID mapped — row numbers will be used as identifiers.")
    if not has_coords:
        st.info("ℹ️ No Latitude/Longitude mapped — all rows will be geocoded.")

    mapped_vals = [c for c in mapping.values() if c != UNMAPPED]
    if len(mapped_vals) != len(set(mapped_vals)):
        seen, dupes = set(), set()
        for c in mapped_vals:
            (dupes if c in seen else seen).add(c)
        st.error(f"Column **{', '.join(dupes)}** mapped to multiple fields.")
        st.stop()

    df_m = apply_column_mapping(df_h, mapping)
    for field in LOCATION_FIELDS:
        if field not in df_m.columns:
            df_m[field] = ""
    if "StreetAddress" not in df_m.columns:
        df_m["StreetAddress"] = ""
    if "AddressID" not in df_m.columns:
        df_m["AddressID"] = [f"ROW_{i+1}" for i in range(len(df_m))]
    for field in COORD_FIELDS:
        if field not in df_m.columns:
            df_m[field] = ""

    if value_columns:
        df_m["_TIV"] = 0.0
        for vc in value_columns:
            if vc in df_m.columns:
                df_m["_TIV"] += pd.to_numeric(df_m[vc], errors="coerce").fillna(0)
        total_portfolio_tiv = df_m["_TIV"].sum()
        st.caption(f"Total portfolio value: **{total_portfolio_tiv:,.0f}** across {len(value_columns)} value column(s).")

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

    has_err = False
    if val["warnings"]:
        st.markdown("#### ⚠️ Warnings")
        for w in val["warnings"]:
            st.warning(w)
    if val["flagged_rows"]:
        st.markdown("#### 🔍 Flagged Rows")
        for lbl, fdf in val["flagged_rows"].items():
            with st.expander(f"{lbl} ({len(fdf)} rows)"):
                st.dataframe(fdf, use_container_width=True)
    if not val["warnings"]:
        st.success("Preflight complete — no obvious source-data issues detected.")
    else:
        st.caption("Preflight flags are informational. Geocoding continues automatically; only genuinely unresolved rows need review afterward.")

    # Step 4: Geocode
    st.divider()
    st.subheader("Step 4: Geocode")
    if st.button("Geocode", disabled=not api_key, type="primary"):
        if not api_key:
            st.error("Enter your API key in the sidebar.")
        else:
            res_df, comp_df = process_dataframe(df_m, api_key, target_rps, max_workers, skip_existing)
            if res_df is not None:
                st.session_state["_last_geocode_results"] = res_df
                st.session_state["_last_geocode_comparison"] = comp_df
                st.session_state["_last_geocode_skip_existing"] = skip_existing

    # Results persist across normal Streamlit reruns (map slider, download clicks, sidebar changes).
    render_geocode_results(
        st.session_state.get("_last_geocode_results"),
        st.session_state.get("_last_geocode_comparison"),
        st.session_state.get("_last_geocode_skip_existing", True),
    )

else:
    st.markdown(
        """
        ### How to use
        1. Paste your **Google Geocoding API key** in the sidebar.
        2. Upload a file — CSV, Excel (.xlsx / .xls), or delimited text (.txt / .tsv).
        3. **Select the header row** — pick which row contains your column names.
        4. **Map your columns** — only `StreetAddress` is required. Optionally map `Latitude`/`Longitude`, `AddressID`, location fields, and value columns for richer output.
        5. Review the **automatic preflight summary**. Flags do not block processing.
        6. Click **Geocode** — unique locations are processed concurrently, source country is treated as soft evidence, and fallbacks run only when needed.
        7. Review only the **recommendations** for genuinely weak or unresolved rows.
        8. Download the results and comparison report.

        Any additional columns in your file are preserved as-is in the output.
        """
    )