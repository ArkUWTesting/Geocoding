import streamlit as st
import pandas as pd
import requests
import time
import math
import re
import threading
import hashlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import pydeck as pdk

st.set_page_config(page_title="Ark Batch Geocoder", page_icon="G", layout="wide")
st.title("Ark Batch Geocoder")
st.caption("Fast batch geocoding with progressive geographic fallbacks, explicit quality levels, and auditable failure reasons.")
st.caption(f"Build {APP_VERSION}")

# Ark Google Cloud project quota: 6,000 Geocoding v3 requests/minute.
GOOGLE_QPM_QUOTA = 6000
GOOGLE_QPM_DEFAULT = 5400
APP_VERSION = "v5-progressive-fallbacks"

# Session state
for key, default in {
    "_geocode_cache": {},
    "_confirm_clear": False,
    "_last_geocode_results": None,
    "_last_geocode_comparison": None,
    "_last_geocode_skip_existing": True,
    "_last_input_signature": None,
    "_active_file_signature": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

st.sidebar.header("Google API")
api_key = st.sidebar.text_input(
    "Geocoding API key", type="password",
    help="Used only for this Streamlit session. The app does not write the key to disk."
)
if api_key:
    st.sidebar.success("API key entered")
else:
    st.sidebar.warning("Enter an API key before starting a run.")

st.sidebar.header("Processing")
skip_existing = st.sidebar.checkbox(
    "Keep valid existing coordinates", value=True,
    help="Rows with a valid latitude/longitude pair are left unchanged. Invalid or incomplete coordinates are geocoded again."
)
target_qpm = st.sidebar.slider(
    "Google queries per minute", 300, GOOGLE_QPM_QUOTA, GOOGLE_QPM_DEFAULT, 100,
    help=(
        f"Ark's approved Geocoding v3 quota is {GOOGLE_QPM_QUOTA:,} queries/minute. "
        "This is a rolling 60-second limit across primary searches, fallbacks, and retries. "
        "Selecting the full quota maximises speed but leaves no headroom for quota-window differences."
    )
)
st.sidebar.caption(
    f"Run ceiling: {target_qpm:,}/min. Project ceiling: {GOOGLE_QPM_QUOTA:,}/min."
)
if target_qpm >= GOOGLE_QPM_QUOTA:
    st.sidebar.warning("Full quota selected: fastest possible setting, but any Google quota-window jitter can cause temporary OVER_QUERY_LIMIT responses. The app will back off and retry automatically.")
elif target_qpm >= 5700:
    st.sidebar.info("Near-full quota selected. This is fast and still leaves a small amount of headroom.")

with st.sidebar.expander("Fallback policy", expanded=True):
    st.caption("Fallbacks run only for unresolved locations and are deduplicated across the file.")
    fallback_postal = st.checkbox("Use postcode / ZIP centroids", value=True)
    fallback_city = st.checkbox("Use city centroids", value=True)
    fallback_admin = st.checkbox("Use county/state/region centroids", value=True)
    fallback_country = st.checkbox(
        "Use country centroids as a last resort", value=False,
        help="Usually too coarse for catastrophe modelling and risky when source country codes are unreliable."
    )

with st.sidebar.expander("Advanced performance", expanded=False):
    max_workers = st.slider(
        "Concurrent workers", 8, 160, 96, 8,
        help=(
            "How many location jobs can be in progress at once. Workers share the same rolling QPM limiter, "
            "so increasing this does not bypass the Google quota."
        )
    )
    st.caption("96 workers is a sensible starting point for a 6,000-QPM project quota.")

with st.sidebar.expander("Session cache", expanded=False):
    cache_display = st.empty()
    cache_display.caption(f"{len(st.session_state['_geocode_cache']):,} successful lookups cached in this session.")
    if not st.session_state["_confirm_clear"]:
        if st.button("Clear session cache", key="clear_cache_btn"):
            st.session_state["_confirm_clear"] = True
            st.rerun()
    else:
        st.warning("Clear all cached geocodes from this Streamlit session?")
        c_yes, c_no = st.columns(2)
        with c_yes:
            if st.button("Clear", type="primary", key="confirm_clear_cache"):
                st.session_state["_geocode_cache"] = {}
                st.session_state["_confirm_clear"] = False
                st.rerun()
        with c_no:
            if st.button("Cancel", key="cancel_clear_cache"):
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

CACHE_VERSION = "v5-progressive-centroids"
_thread_local = threading.local()


class RollingQpmLimiter:
    """Thread-safe rolling-60-second limiter with shared quota back-pressure.

    Every real outbound HTTP request is registered here, including retries. The rolling
    window is the source of truth; a small derived spacing is also used to avoid needless
    micro-bursts while still allowing the user to choose any QPM target up to the project quota.
    """
    def __init__(self, qpm_limit, label="run"):
        self.qpm_limit = max(1, int(qpm_limit))
        self.label = label
        self.window_seconds = 60.0
        self.timestamps = deque()
        self.condition = threading.Condition()
        self.next_smoothed = time.monotonic()
        self.total_requests = 0
        self.peak_rolling_qpm = 0
        self.over_query_limit_events = 0
        self.cooldown_until = 0.0
        self._oql_streak = 0
        self._last_oql = 0.0

    def _purge(self, now):
        cutoff = now - self.window_seconds
        while self.timestamps and self.timestamps[0] <= cutoff:
            self.timestamps.popleft()

    def wait(self):
        """Wait until one request slot is available, then register that request."""
        while True:
            with self.condition:
                now = time.monotonic()
                self._purge(now)

                if now < self.cooldown_until:
                    self.condition.wait(timeout=max(0.01, self.cooldown_until - now))
                    continue

                if len(self.timestamps) >= self.qpm_limit:
                    wait_for = max(0.01, self.timestamps[0] + self.window_seconds - now + 0.01)
                    self.condition.wait(timeout=wait_for)
                    continue

                # Smooth the chosen QPM across the minute. The rolling window above remains
                # the hard cap; this simply avoids releasing large bursts at once.
                interval = self.window_seconds / self.qpm_limit
                if now < self.next_smoothed:
                    self.condition.wait(timeout=max(0.001, self.next_smoothed - now))
                    continue

                self.timestamps.append(now)
                self.next_smoothed = max(now, self.next_smoothed) + interval
                self.total_requests += 1
                self.peak_rolling_qpm = max(self.peak_rolling_qpm, len(self.timestamps))
                return

    def report_over_query_limit(self):
        """Pause all workers sharing this limiter when Google says the quota is saturated."""
        with self.condition:
            now = time.monotonic()
            self.over_query_limit_events += 1
            if now - self._last_oql <= 10.0:
                self._oql_streak = min(self._oql_streak + 1, 5)
            else:
                self._oql_streak = 1
            self._last_oql = now
            cooldown = min(12.0, 0.75 * (2 ** (self._oql_streak - 1)))
            self.cooldown_until = max(self.cooldown_until, now + cooldown)
            self.condition.notify_all()

    def report_success(self):
        with self.condition:
            now = time.monotonic()
            if self._last_oql and now - self._last_oql > 15.0:
                self._oql_streak = 0

    def snapshot(self):
        with self.condition:
            now = time.monotonic()
            self._purge(now)
            return {
                "current_rolling_qpm": len(self.timestamps),
                "peak_rolling_qpm": self.peak_rolling_qpm,
                "total_requests": self.total_requests,
                "over_query_limit_events": self.over_query_limit_events,
                "cooldown_remaining": max(0.0, self.cooldown_until - now),
                "qpm_limit": self.qpm_limit,
            }


@st.cache_resource
def get_project_quota_limiter():
    """One 6,000-QPM guard shared by all Streamlit sessions in this app process."""
    return RollingQpmLimiter(GOOGLE_QPM_QUOTA, label="project")


class CombinedLimiter:
    """Apply both the user-selected run limit and the shared project limit."""
    def __init__(self, run_qpm):
        self.run = RollingQpmLimiter(run_qpm, label="run")
        self.project = get_project_quota_limiter()

    def wait(self):
        # Local limit first, then the shared project ceiling.
        self.run.wait()
        self.project.wait()

    def report_over_query_limit(self):
        self.run.report_over_query_limit()
        self.project.report_over_query_limit()

    def report_success(self):
        self.run.report_success()
        self.project.report_success()

    def snapshot(self):
        snap = self.run.snapshot()
        project = self.project.snapshot()
        snap.update({
            "project_current_rolling_qpm": project["current_rolling_qpm"],
            "project_peak_rolling_qpm": project["peak_rolling_qpm"],
            "project_oql_events": project["over_query_limit_events"],
        })
        return snap


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

def result_component_any(result, comp_types):
    for comp_type in comp_types:
        long_name, short_name = result_component(result, comp_type)
        if long_name or short_name:
            return long_name, short_name
    return "", ""


def text_equivalent(a, b):
    a = re.sub(r"[^a-z0-9]+", " ", clean_text(a).casefold()).strip()
    b = re.sub(r"[^a-z0-9]+", " ", clean_text(b).casefold()).strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


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
    """Score only genuine returned component matches; missing Google components never count as matches."""
    score = 0
    if country_code:
        _, short = result_component(result, "country")
        if short and short.casefold() == clean_text(country_code).casefold():
            score += 10
    if admin1:
        long_name, short_name = result_component(result, "administrative_area_level_1")
        if (long_name and text_equivalent(admin1, long_name)) or (short_name and text_equivalent(admin1, short_name)):
            score += 5
    if admin2:
        long_name, short_name = result_component(result, "administrative_area_level_2")
        if (long_name and text_equivalent(admin2, long_name)) or (short_name and text_equivalent(admin2, short_name)):
            score += 3
    if city:
        long_name, short_name = result_component_any(
            result, ["locality", "postal_town", "sublocality", "administrative_area_level_3"]
        )
        if (long_name and text_equivalent(city, long_name)) or (short_name and text_equivalent(city, short_name)):
            score += 3
    if postal_code:
        long_name, short_name = result_component(result, "postal_code")
        if (long_name and postal_equivalent(postal_code, long_name)) or (short_name and postal_equivalent(postal_code, short_name)):
            score += 4
    return score


def single_geocode_call(address, key, components=None, country_code=None, admin1=None,
                        admin2=None, city=None, postal_code=None, limiter=None, retries=4):
    """Execute one Google strategy with bounded retries and exact request accounting."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": key}
    if components:
        params["components"] = components

    last_status = "UNKNOWN"
    last_error = ""
    request_count = 0
    quota_events = 0
    status_history = []

    def failure_payload(status):
        return {
            "lat": None, "lng": None, "status": status, "location_type": None,
            "match_score": 0, "formatted_address": "", "place_id": "",
            "google_country": "", "google_postal": "", "google_city": "",
            "google_admin1": "", "google_admin2": "", "result_types": [],
            "partial_match": False, "error_message": last_error,
            "api_calls": request_count, "quota_events": quota_events,
            "status_history": status_history[:],
        }

    for attempt in range(retries + 1):
        if limiter:
            limiter.wait()
        request_count += 1
        try:
            resp = _http_session().get(url, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "UNKNOWN")
            last_status = status
            last_error = clean_text(data.get("error_message", ""))
            status_history.append(status)

            if status == "OK" and data.get("results"):
                if limiter:
                    limiter.report_success()
                scored = []
                for pos, result in enumerate(data["results"]):
                    loc_type = result.get("geometry", {}).get("location_type", "UNKNOWN")
                    precision = LOCATION_TYPE_RANK.get(loc_type, 0)
                    match_score = score_result_against_constraints(
                        result, country_code, admin1, admin2, city, postal_code)
                    scored.append((match_score, precision, -pos, result))
                scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
                match_score, _, _, best = scored[0]
                loc = best.get("geometry", {}).get("location", {})
                loc_type = best.get("geometry", {}).get("location_type", "UNKNOWN")
                _, google_country = result_component(best, "country")
                google_postal, _ = result_component(best, "postal_code")
                google_city, _ = result_component_any(
                    best, ["locality", "postal_town", "sublocality", "administrative_area_level_3"])
                google_admin1, _ = result_component(best, "administrative_area_level_1")
                google_admin2, _ = result_component(best, "administrative_area_level_2")
                return {
                    "lat": loc.get("lat"), "lng": loc.get("lng"), "status": "OK",
                    "location_type": loc_type, "match_score": match_score,
                    "formatted_address": best.get("formatted_address", ""),
                    "place_id": best.get("place_id", ""),
                    "google_country": google_country.upper(),
                    "google_postal": google_postal,
                    "google_city": google_city,
                    "google_admin1": google_admin1,
                    "google_admin2": google_admin2,
                    "result_types": list(best.get("types", [])),
                    "partial_match": bool(best.get("partial_match", False)),
                    "error_message": "", "api_calls": request_count,
                    "quota_events": quota_events, "status_history": status_history[:],
                }

            if status == "OVER_QUERY_LIMIT":
                quota_events += 1
                if limiter:
                    limiter.report_over_query_limit()
                if attempt < retries:
                    continue
                return failure_payload(status)

            if status == "UNKNOWN_ERROR" and attempt < retries:
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
                continue

            return failure_payload(status)

        except requests.Timeout:
            last_status, last_error = "TIMEOUT", "Google did not respond within 12 seconds."
        except requests.ConnectionError:
            last_status, last_error = "CONNECTION_ERROR", "The app could not connect to Google's Geocoding API."
        except requests.RequestException as exc:
            last_status, last_error = "HTTP_ERROR", str(exc)
        except Exception as exc:
            last_status, last_error = "ERROR", str(exc)

        status_history.append(last_status)
        if attempt < retries:
            time.sleep(min(4.0, 0.5 * (2 ** attempt)))

    return failure_payload(last_status)


def _query_parts(*values):
    parts = []
    seen = set()
    for value in values:
        value = clean_text(value)
        if value and value.casefold() not in seen:
            parts.append(value)
            seen.add(value.casefold())
    return ", ".join(parts)


def infer_granularity(geo, requested="STREET"):
    types = set(geo.get("result_types") or [])
    if requested != "STREET":
        return requested
    if types & {"street_address", "premise", "subpremise", "route", "establishment", "point_of_interest"}:
        return "STREET"
    if "postal_code" in types:
        return "POSTAL_CODE"
    if types & {"locality", "postal_town", "sublocality", "administrative_area_level_3"}:
        return "CITY"
    if "administrative_area_level_2" in types:
        return "ADMIN2"
    if "administrative_area_level_1" in types:
        return "ADMIN1"
    if "country" in types:
        return "COUNTRY"
    if geo.get("location_type") in {"ROOFTOP", "RANGE_INTERPOLATED"}:
        return "STREET"
    return "APPROXIMATE"

def returned_area_granularity(geo):
    """Describe the level Google actually returned, not the level we hoped to get."""
    types = set(geo.get("result_types") or [])
    if "postal_code" in types:
        return "POSTAL_CODE"
    if types & {"locality", "postal_town", "sublocality", "administrative_area_level_3"}:
        return "CITY"
    if "administrative_area_level_2" in types:
        return "ADMIN2"
    if "administrative_area_level_1" in types:
        return "ADMIN1"
    if "country" in types:
        return "COUNTRY"
    return "APPROXIMATE"


def _admin_matches(source_value, google_value):
    """Missing Google admin components are neutral; an explicit contradiction is not."""
    source_value = clean_text(source_value)
    google_value = clean_text(google_value)
    return not source_value or not google_value or text_equivalent(source_value, google_value)


def assess_area_candidate(stage, geo, job):
    """Return (accepted, actual_granularity, rejection_reason) for an area fallback.

    A query may contain a postcode but Google may only resolve the city. We preserve that
    distinction rather than overstating precision. Source country remains soft evidence because
    RMS exports can contain systematically incorrect country codes.
    """
    if geo.get("status") != "OK":
        return False, "", f"Google status {geo.get('status', 'UNKNOWN')}"

    granularity = returned_area_granularity(geo)
    city_match = text_equivalent(job.get("_city"), geo.get("google_city"))
    postal_match = postal_equivalent(job.get("_postal"), geo.get("google_postal"))
    admin2_match = text_equivalent(job.get("_admin2"), geo.get("google_admin2"))
    admin1_match = text_equivalent(job.get("_admin1"), geo.get("google_admin1"))
    admin1_consistent = _admin_matches(job.get("_admin1"), geo.get("google_admin1"))
    admin2_consistent = _admin_matches(job.get("_admin2"), geo.get("google_admin2"))

    if stage == "city_postal":
        if granularity == "POSTAL_CODE" and postal_match and (not job.get("_city") or not geo.get("google_city") or city_match):
            return True, "POSTAL_CODE", ""
        if granularity == "CITY" and city_match and admin1_consistent:
            return True, "CITY", ""
        return False, granularity, "Returned area did not consistently match the supplied city/postcode."

    if stage == "postal":
        if postal_match and (not job.get("_city") or not geo.get("google_city") or city_match) and admin1_consistent:
            return True, "POSTAL_CODE", ""
        return False, granularity, "Returned postcode conflicted with the supplied city/state context."

    if stage in {"city_admin1", "city"}:
        if city_match and admin1_consistent:
            return True, "CITY", ""
        return False, granularity, "Returned city conflicted with the supplied city/state context."

    if stage in {"admin2_admin1", "admin2"}:
        if admin2_match and admin1_consistent:
            return True, "ADMIN2", ""
        return False, granularity, "Returned county/district conflicted with the supplied admin context."

    if stage == "admin1":
        if admin1_match:
            return True, "ADMIN1", ""
        return False, granularity, "Returned state/region did not match the supplied value."

    if stage == "country":
        if clean_text(job.get("_cc")).upper() == clean_text(geo.get("google_country")).upper():
            return True, "COUNTRY", ""
        return False, granularity, "Returned country did not match the supplied country code."

    return False, granularity, "Unsupported fallback stage."


def _quality_flags(geo, source_country, source_postal, has_street, granularity=None, method=None):
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
        flags.append("No street address was supplied")
    if granularity and granularity not in {"STREET", "EXISTING"}:
        readable = {
            "POSTAL_CODE": "postcode/ZIP centroid", "CITY": "city centroid",
            "ADMIN2": "county/district centroid", "ADMIN1": "state/region centroid",
            "COUNTRY": "country centroid", "APPROXIMATE": "approximate area point",
        }.get(granularity, granularity.lower())
        flags.append(f"Resolved at {readable} level rather than street level")
    elif geo.get("location_type") in {"GEOMETRIC_CENTER", "APPROXIMATE"}:
        flags.append(f"Google precision: {geo.get('location_type')}")
    if method and method not in {"primary", "existing"} and granularity == "STREET":
        flags.append(f"Street result required {method.replace('_', ' ')} fallback")
    return " | ".join(flags)


def geocode_address(full_address, street_address, key, country_code=None, city=None,
                    admin1=None, admin2=None, postal_code=None, limiter=None):
    """Street-level pass only. Area centroids are handled later in deduplicated batches."""
    full_address = clean_text(full_address)
    street_address = clean_text(street_address)
    city, admin1, admin2, postal_code = map(clean_text, (city, admin1, admin2, postal_code))
    country_code = clean_text(country_code).upper()

    if not street_address:
        return {
            "lat": None, "lng": None, "status": "NO_STREET_FOR_PRIMARY", "location_type": None,
            "method": "primary", "granularity": "", "fallback": False,
            "detail": "No street address was supplied; area fallbacks will be tried.",
            "api_calls": 0, "formatted_address": "", "place_id": "", "google_country": "",
            "google_postal": "", "google_city": "", "google_admin1": "", "google_admin2": "",
            "result_types": [], "partial_match": False, "quality_flag": "",
            "error_message": "", "quota_events": 0, "status_history": ["primary:NO_STREET"],
            "rejected_candidates": 0,
        }

    score_kwargs = {
        "country_code": None,  # source country is intentionally soft evidence
        "admin1": admin1 or None,
        "admin2": admin2 or None,
        "city": city or None,
        "postal_code": postal_code or None,
    }

    query_candidates = [
        ("primary", full_address),
        ("street_city", _query_parts(street_address, city, admin1)),
        ("street_postal", _query_parts(street_address, postal_code)),
        ("street_only", street_address),
    ]
    queries = []
    seen = set()
    for method, query in query_candidates:
        query = clean_text(query)
        if query and query.casefold() not in seen:
            queries.append((method, query))
            seen.add(query.casefold())

    candidates = []
    api_calls = 0
    quota_events = 0
    status_history = []
    last_error = ""
    statuses = []

    for method, query in queries:
        r = single_geocode_call(query, key, limiter=limiter, **score_kwargs)
        api_calls += int(r.get("api_calls", 0))
        quota_events += int(r.get("quota_events", 0))
        status = r.get("status", "UNKNOWN")
        statuses.append(status)
        status_history.extend([f"{method}:{x}" for x in r.get("status_history", [])])
        if r.get("error_message"):
            last_error = r.get("error_message", "")
        if status == "OK":
            r["query_used"] = query
            candidates.append((method, r))
            if r.get("location_type") in {"ROOFTOP", "RANGE_INTERPOLATED"} and not r.get("partial_match"):
                # Stop once the street search has produced a strong result consistent with at least
                # one supplied geography field, or there is no geography context to check against.
                has_context = bool(city or admin1 or admin2 or postal_code)
                if not has_context or r.get("match_score", 0) > 0:
                    break

    if not candidates:
        return {
            "lat": None, "lng": None, "status": statuses[-1] if statuses else "ZERO_RESULTS",
            "location_type": None, "method": "primary", "granularity": "", "fallback": False,
            "detail": "Street-level strategies did not resolve the location.", "api_calls": api_calls,
            "formatted_address": "", "place_id": "", "google_country": "", "google_postal": "",
            "google_city": "", "google_admin1": "", "google_admin2": "", "result_types": [],
            "partial_match": False, "quality_flag": "", "error_message": last_error,
            "quota_events": quota_events, "status_history": status_history, "rejected_candidates": 0,
        }

    best_method, best = max(
        candidates,
        key=lambda item: (item[1].get("match_score", 0), LOCATION_TYPE_RANK.get(item[1].get("location_type"), 0))
    )
    has_context = bool(city or admin1 or admin2 or postal_code)
    if has_context and best.get("match_score", 0) <= 0:
        return {
            "lat": None, "lng": None, "status": "GEOGRAPHY_MISMATCH", "location_type": None,
            "method": "primary", "granularity": "", "fallback": False,
            "detail": "Google returned a street candidate, but it did not match the supplied city/postcode/admin geography, so it was rejected.",
            "api_calls": api_calls, "formatted_address": best.get("formatted_address", ""),
            "place_id": best.get("place_id", ""), "google_country": best.get("google_country", ""),
            "google_postal": best.get("google_postal", ""), "google_city": best.get("google_city", ""),
            "google_admin1": best.get("google_admin1", ""), "google_admin2": best.get("google_admin2", ""),
            "result_types": best.get("result_types", []), "partial_match": best.get("partial_match", False),
            "quality_flag": "Rejected street candidate because geography did not match", "error_message": last_error,
            "quota_events": quota_events, "status_history": status_history + ["primary:REJECTED_GEOGRAPHY"],
            "rejected_candidates": 1,
        }

    granularity = infer_granularity(best, "STREET")
    flag = _quality_flags(best, country_code, postal_code, True, granularity, best_method)
    return {
        **best,
        "method": best_method,
        "granularity": granularity,
        "fallback": best_method != "primary" or granularity != "STREET",
        "addr_only_better": best_method == "street_only",
        "detail": f"Resolved using {best_method.replace('_', ' ')} strategy.",
        "quality_flag": flag,
        "api_calls": api_calls,
        "quota_events": quota_events,
        "status_history": status_history,
        "rejected_candidates": 0,
        "error_reason": "",
        "action": (
            "Use as an area-level result. Correct the source address and rerun if street-level accuracy is required."
            if granularity != "STREET" else ""
        ),
    }


def failure_reason_and_action(status, job, error_message="", stages_tried=None, rejected_candidates=0):
    status = clean_text(status) or "UNKNOWN"
    stages = ", ".join(stages_tried or [])
    street = clean_text(job.get("_street", ""))
    city = clean_text(job.get("_city", ""))
    postal = clean_text(job.get("_postal", ""))
    admin1 = clean_text(job.get("_admin1", ""))
    admin2 = clean_text(job.get("_admin2", ""))

    if status == "NO_SEARCHABLE_ADDRESS":
        return (
            "No mapped street, postcode, city, county/district, or state/region value is available for this row.",
            "Supply at least a street address or one usable geography field, then rerun the row."
        )
    if status == "REQUEST_DENIED":
        detail = f" Google message: {error_message}" if error_message else ""
        return (
            "Google rejected the request or API key." + detail,
            "Check that Geocoding API v3 is enabled, billing is active, and the API key restrictions allow this app."
        )
    if status == "OVER_QUERY_LIMIT":
        return (
            "Google continued returning OVER_QUERY_LIMIT after automatic global back-off and retries.",
            "Rerun the failed rows with a lower queries-per-minute setting, or confirm the 6,000-QPM project quota is active."
        )
    if status in {"TIMEOUT", "CONNECTION_ERROR", "HTTP_ERROR", "UNKNOWN_ERROR"}:
        detail = f" Last message: {error_message}" if error_message else ""
        return (
            f"The lookup failed because of a transient Google/network error ({status})." + detail,
            "Rerun the unresolved rows. If it repeats, check network access and Google API status."
        )
    if status == "GEOGRAPHY_MISMATCH" or rejected_candidates:
        return (
            "Google returned one or more candidates, but they conflicted with the supplied geography and were rejected to avoid a likely false coordinate." + (f" Last check: {error_message}" if error_message else ""),
            "Check the street, postcode, city and admin fields for contradictions. Correct the source geography and rerun; broader enabled fallbacks are already attempted automatically."
        )
    if status == "INVALID_REQUEST":
        return (
            "Google considered the generated query invalid, usually because the mapped location fields were empty or malformed.",
            "Check the column mapping and source values for this row."
        )
    if status == "NO_STREET_FOR_PRIMARY":
        supplied = [name for name, value in [("postcode", postal), ("city", city), ("county/district", admin2), ("state/region", admin1)] if value]
        return (
            f"No street address was supplied, and no enabled area fallback resolved the available {', '.join(supplied) if supplied else 'geography'}.",
            "Enable the relevant postcode/city/admin fallback or supply a street address, then rerun the row."
        )
    if status in {"ZERO_RESULTS", "NO_ACCEPTABLE_FALLBACK"}:
        supplied = []
        if street: supplied.append("street")
        if postal: supplied.append("postcode")
        if city: supplied.append("city")
        if admin2: supplied.append("county/district")
        if admin1: supplied.append("state/region")
        supplied_text = ", ".join(supplied) or "no usable geography"
        tried_text = f" Strategies tried: {stages}." if stages else ""
        return (
            f"Google could not resolve the supplied {supplied_text} to an acceptable location.{tried_text}",
            "Check for typos or missing geography. If the row is material, correct the source data and rerun it."
        )
    if status == "WORKER_ERROR":
        return (
            f"The app encountered an internal worker error. {error_message}".strip(),
            "Rerun the failed rows. If the same row fails again, capture its values and the error text for investigation."
        )
    return (
        f"Geocoding ended with status {status}." + (f" {error_message}" if error_message else ""),
        "Review the source address/geography and rerun the row."
    )


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


def guess_header_row(raw_df, max_rows=15):
    """Pick the most likely header row using known field aliases and basic header shape."""
    known = {
        "streetaddress", "street", "streetname", "address", "city", "cityname", "postalcode",
        "postcode", "zip", "zipcode", "cntrycode", "countrycode", "country", "latitude", "longitude",
        "lat", "lng", "lon", "admin1", "admin2", "state", "county", "addressid", "locnum"
    }
    best_row, best_score = 0, float("-inf")
    for i in range(min(max_rows, len(raw_df))):
        values = [clean_text(v) for v in raw_df.iloc[i].tolist()]
        nonblank = [v for v in values if v]
        if not nonblank:
            continue
        lowered = [re.sub(r"[^a-z0-9]+", "", v.casefold()) for v in nonblank]
        alias_hits = sum(1 for v in lowered if v in known)
        unique_ratio = len(set(lowered)) / max(1, len(lowered))
        numeric_like = sum(1 for v in nonblank if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", v))
        score = alias_hits * 10 + len(nonblank) * 0.25 + unique_ratio * 2 - numeric_like * 0.5
        if score > best_score:
            best_row, best_score = i, score
    return best_row


# =============================================================================
# VALIDATION
# =============================================================================

def validate_dataframe(df, skip):
    """Fast, non-blocking preflight with actionable source-data diagnostics."""
    warnings = []
    flagged_rows = {}
    total_rows = len(df)

    street = df["StreetAddress"].map(clean_text)
    city = df["CityName"].map(clean_text) if "CityName" in df.columns else pd.Series("", index=df.index)
    admin1 = df["Admin1Name"].map(clean_text) if "Admin1Name" in df.columns else pd.Series("", index=df.index)
    admin2 = df["Admin2Name"].map(clean_text) if "Admin2Name" in df.columns else pd.Series("", index=df.index)
    postal = df["PostalCode"].map(clean_text) if "PostalCode" in df.columns else pd.Series("", index=df.index)
    cc = df["CountryCode"].map(clean_text).str.upper() if "CountryCode" in df.columns else pd.Series("", index=df.index)

    searchable = (street != "") | (city != "") | (admin1 != "") | (admin2 != "") | (postal != "")
    blank_street = street == ""

    lat_num = pd.to_numeric(df["Latitude"], errors="coerce")
    lng_num = pd.to_numeric(df["Longitude"], errors="coerce")
    coord_valid = lat_num.between(-90, 90) & lng_num.between(-180, 180)
    any_coord_text = df["Latitude"].map(clean_text).ne("") | df["Longitude"].map(clean_text).ne("")
    invalid_coords = any_coord_text & ~coord_valid
    unsearchable = ~searchable

    if blank_street.any():
        warnings.append(
            f"{int(blank_street.sum()):,} row(s) have no street address. They will start at postcode/city/admin fallback level instead of failing automatically."
        )
    if invalid_coords.any():
        warnings.append(
            f"{int(invalid_coords.sum()):,} row(s) contain incomplete or out-of-range coordinates. They will be geocoded again even when existing coordinates are kept."
        )
        flagged_rows["Invalid existing coordinates"] = df.loc[invalid_coords].head(200)
    if unsearchable.any():
        warnings.append(
            f"{int(unsearchable.sum()):,} row(s) contain no usable street or geography fields. These are the only rows guaranteed to remain unresolved."
        )
        flagged_rows["No searchable geography"] = df.loc[unsearchable].head(200)

    nonblank_cc = cc[cc != ""]
    if len(nonblank_cc):
        top_cc = nonblank_cc.value_counts().index[0]
        top_n = int((nonblank_cc == top_cc).sum())
        if top_n / len(nonblank_cc) >= 0.95:
            warnings.append(
                f"CountryCode is overwhelmingly {top_cc} ({top_n:,}/{len(nonblank_cc):,}). Country remains soft evidence and is not used as a hard Google restriction."
            )

    # Four-digit postcodes are not automatically changed because they may be valid outside the US.
    four_digit = postal.str.fullmatch(r"\d{4}", na=False)
    if four_digit.any():
        warnings.append(
            f"{int(four_digit.sum()):,} row(s) have four-digit numeric postcodes. The app preserves them and lets Google normalise leading zeroes where appropriate rather than assuming they are US ZIP codes."
        )

    needs = searchable & (~coord_valid if skip else True)
    keys = pd.DataFrame({
        "street": street, "city": city, "admin1": admin1, "admin2": admin2, "postal": postal
    }, index=df.index)
    unique_jobs = int(keys.loc[needs].drop_duplicates().shape[0])

    return {
        "errors": [], "warnings": warnings, "flagged_rows": flagged_rows,
        "stats": {
            "total_rows": total_rows,
            "unique_addresses": int(keys.loc[searchable].drop_duplicates().shape[0]),
            "blank_addresses": int(blank_street.sum()),
            "already_geocoded": int((searchable & coord_valid).sum()),
            "to_geocode": unique_jobs,
            "unsearchable": int(unsearchable.sum()),
            "four_digit_postcodes": int(four_digit.sum()),
        }
    }


# =============================================================================
# GEOCODING
# =============================================================================

def process_dataframe(df, key, target_qpm, max_workers, skip, fallback_options):
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
    result["_full_addr"] = result.apply(
        lambda r: _query_parts(r["_street"], r["_city"], r["_admin2"], r["_admin1"], r["_postal"]), axis=1
    )

    searchable = result[["_street", "_city", "_admin1", "_admin2", "_postal"]].apply(
        lambda row: any(clean_text(x) for x in row), axis=1
    )
    coord_valid = result.apply(lambda r: valid_coordinate_pair(r["Latitude"], r["Longitude"]), axis=1)
    had_coords = searchable & coord_valid
    orig_lats = result.loc[had_coords, "Latitude"].copy()
    orig_lngs = result.loc[had_coords, "Longitude"].copy()
    needs = searchable & (~coord_valid if skip else True)

    output_defaults = {
        "GoogleLocationType": "", "GeoMethod": "", "GeoGranularity": "", "GeoStatus": "",
        "GeoAttempts": 0, "GeoQuotaEvents": 0, "GeoStatusHistory": "", "GeoQualityFlag": "",
        "GeoErrorReason": "", "GeoAction": "", "GeoQueryUsed": "", "GeoFallbackUsed": False,
        "GeoRejectedCandidates": 0, "GoogleFormattedAddress": "", "GooglePlaceID": "",
        "GoogleCountryCode": "", "GooglePostalCode": "", "GoogleCity": "",
        "GoogleAdmin1": "", "GoogleAdmin2": "", "GooglePartialMatch": False, "AddrOnlyBetter": False,
    }
    for col, default in output_defaults.items():
        result[col] = default

    result.loc[had_coords & skip, "GeoStatus"] = "EXISTING_SKIPPED"
    result.loc[had_coords & skip, "GeoMethod"] = "existing"
    result.loc[had_coords & skip, "GeoGranularity"] = "EXISTING"
    result.loc[~searchable, "GeoStatus"] = "NO_SEARCHABLE_ADDRESS"

    job_cols = ["_full_addr", "_street", "_city", "_admin1", "_admin2", "_postal", "_cc"]
    geo_sub = result.loc[needs, job_cols].copy()
    dedupe_cols = ["_full_addr", "_street", "_city", "_admin1", "_admin2", "_postal"]
    jobs_df = geo_sub.drop_duplicates(subset=dedupe_cols)
    jobs = jobs_df.to_dict("records")
    total = len(jobs)

    if total == 0:
        # Fill explicit reasons for unsearchable rows even when there is nothing to call Google for.
        for idx in result.index[result["GeoStatus"].eq("NO_SEARCHABLE_ADDRESS")]:
            reason, action = failure_reason_and_action("NO_SEARCHABLE_ADDRESS", {
                "_street": result.at[idx, "_street"], "_city": result.at[idx, "_city"],
                "_postal": result.at[idx, "_postal"], "_admin1": result.at[idx, "_admin1"],
                "_admin2": result.at[idx, "_admin2"],
            })
            result.at[idx, "GeoErrorReason"] = reason
            result.at[idx, "GeoAction"] = action
        st.info("No rows require geocoding.")
        return result.drop(columns=job_cols), None

    with st.expander("Search examples", expanded=False):
        st.caption("Primary street-level search strings. Area fallbacks are generated only for unresolved locations.")
        for job in jobs[:5]:
            st.code(job["_full_addr"] or "(no street-level query)", language=None)

    limiter = CombinedLimiter(target_qpm)
    with st.spinner("Checking Google API access..."):
        test = single_geocode_call("10 Downing Street, London", key, limiter=limiter, retries=0)
        if test["status"] == "REQUEST_DENIED":
            msg = test.get("error_message") or "Google rejected the API key."
            st.error(
                f"Google API access check failed: {msg}\n\n"
                "Check that Geocoding API v3 is enabled, billing is active, and this key's restrictions permit the request."
            )
            return None, None
        if test["status"] not in {"OK", "ZERO_RESULTS"}:
            st.warning(f"API access check returned {test['status']}. The run will continue, but network/quota conditions may affect results.")

    geo_cache = st.session_state["_geocode_cache"]
    final_results = {}
    primary_jobs = {}
    cache_hits = 0
    stage_stats = {}
    progress = st.progress(0, text=f"Street-level pass: 0 of {total:,} unique locations")
    status_area = st.empty()

    def primary_cache_key(job):
        return (CACHE_VERSION, "primary", job["_full_addr"], job["_street"], job["_city"], job["_admin1"], job["_admin2"], job["_postal"])

    for job in jobs:
        ck = primary_cache_key(job)
        primary_jobs[ck] = job

    pending = []
    for ck, job in primary_jobs.items():
        cached = geo_cache.get(ck)
        if cached and cached.get("status") == "OK":
            final_results[ck] = cached
            cache_hits += 1
        else:
            pending.append((ck, job))

    def run_primary(job):
        return geocode_address(
            job["_full_addr"], job["_street"], key,
            country_code=job.get("_cc") or None, city=job.get("_city") or None,
            admin1=job.get("_admin1") or None, admin2=job.get("_admin2") or None,
            postal_code=job.get("_postal") or None, limiter=limiter,
        )

    completed = cache_hits
    primary_requests = 0
    primary_quota = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_primary, job): (ck, job) for ck, job in pending}
        last_ui = time.monotonic()
        for fut in as_completed(futures):
            ck, job = futures[fut]
            try:
                geo = fut.result()
            except Exception as exc:
                geo = {
                    "lat": None, "lng": None, "status": "WORKER_ERROR", "location_type": None,
                    "method": "primary", "granularity": "", "fallback": False, "detail": str(exc),
                    "api_calls": 0, "formatted_address": "", "place_id": "", "google_country": "",
                    "google_postal": "", "google_city": "", "google_admin1": "", "google_admin2": "",
                    "result_types": [], "partial_match": False, "quality_flag": "", "error_message": str(exc),
                    "quota_events": 0, "status_history": ["primary:WORKER_ERROR"], "rejected_candidates": 0,
                }
            final_results[ck] = geo
            primary_requests += int(geo.get("api_calls", 0))
            primary_quota += int(geo.get("quota_events", 0))
            if geo.get("status") == "OK":
                geo_cache[ck] = geo
            completed += 1
            now = time.monotonic()
            if now - last_ui >= 0.15 or completed == total:
                snap = limiter.snapshot()
                progress.progress(completed / total, text=f"Street-level pass: {completed:,} of {total:,} unique locations")
                status_area.caption(
                    f"{snap['total_requests']:,} Google requests | rolling minute {snap['current_rolling_qpm']:,}/{target_qpm:,} | "
                    f"peak {snap['peak_rolling_qpm']:,} | quota responses {snap['over_query_limit_events']:,} | cache hits {cache_hits:,}"
                )
                last_ui = now

    stage_stats["Street search"] = {"lookups": len(pending), "requests": primary_requests, "cache_hits": cache_hits}

    # Accumulators are tracked per original unique location job. Shared centroid lookups are counted once globally,
    # while each row receives the full audit history explaining which strategies were tried for it.
    accum = {}
    for ck, job in primary_jobs.items():
        g = final_results[ck]
        accum[ck] = {
            "api_calls": int(g.get("api_calls", 0)),
            "quota_events": int(g.get("quota_events", 0)),
            "status_history": list(g.get("status_history", [])),
            "rejected_candidates": int(g.get("rejected_candidates", 0)),
            "stages_tried": ["street search"],
            "last_error": g.get("error_message", ""),
            "last_status": g.get("status", ""),
        }

    def unresolved_keys():
        return [ck for ck, g in final_results.items() if g.get("status") != "OK"]

    def fallback_spec(stage, job):
        city, postal, admin1, admin2, cc = job["_city"], job["_postal"], job["_admin1"], job["_admin2"], job["_cc"]
        specs = {
            "city_postal": (bool(city and postal), _query_parts(city, postal, admin1), "POSTAL_CODE", {"city": city, "postal_code": postal, "admin1": admin1 or None}),
            "postal": (bool(postal), _query_parts(postal, admin1), "POSTAL_CODE", {"postal_code": postal, "admin1": admin1 or None}),
            "city_admin1": (bool(city and admin1), _query_parts(city, admin1), "CITY", {"city": city, "admin1": admin1}),
            "city": (bool(city), city, "CITY", {"city": city}),
            "admin2_admin1": (bool(admin2), _query_parts(admin2, admin1), "ADMIN2", {"admin2": admin2, "admin1": admin1 or None}),
            "admin2": (bool(admin2), admin2, "ADMIN2", {"admin2": admin2}),
            "admin1": (bool(admin1), admin1, "ADMIN1", {"admin1": admin1}),
            "country": (bool(cc), cc, "COUNTRY", {"country_code": cc}),
        }
        return specs[stage]

    method_names = {
        "city_postal": "postcode_city_centroid", "postal": "postcode_centroid",
        "city_admin1": "city_admin1_centroid", "city": "city_centroid",
        "admin2_admin1": "admin2_admin1_centroid", "admin2": "admin2_centroid",
        "admin1": "admin1_centroid", "country": "country_centroid",
    }
    stage_labels = {
        "city_postal": "Postcode + city fallback", "postal": "Postcode fallback",
        "city_admin1": "City + state/region fallback", "city": "City fallback",
        "admin2_admin1": "County/district + state/region fallback", "admin2": "County/district fallback",
        "admin1": "State/region fallback", "country": "Country fallback",
    }

    stages = []
    if fallback_options.get("postal"):
        stages += ["city_postal", "postal"]
    if fallback_options.get("city"):
        stages += ["city_admin1", "city"]
    if fallback_options.get("admin"):
        stages += ["admin2_admin1", "admin2", "admin1"]
    if fallback_options.get("country"):
        stages += ["country"]

    for stage in stages:
        unresolved = unresolved_keys()
        if not unresolved:
            break

        stage_to_primary = {}
        unique_lookups = {}
        for ck in unresolved:
            job = primary_jobs[ck]
            eligible, query, granularity, score_kwargs = fallback_spec(stage, job)
            if not eligible or not query:
                continue
            fallback_key = (CACHE_VERSION, "fallback", stage, query.casefold())
            stage_to_primary[ck] = fallback_key
            if fallback_key not in unique_lookups:
                unique_lookups[fallback_key] = {
                    "query": query, "granularity": granularity, "score_kwargs": score_kwargs, "example_job": job
                }
            accum[ck]["stages_tried"].append(stage_labels[stage].lower())

        if not unique_lookups:
            continue

        progress.progress(0, text=f"{stage_labels[stage]}: preparing {len(unique_lookups):,} deduplicated lookup(s)")
        stage_results = {}
        stage_cache_hits = 0
        stage_pending = []
        for fck, spec in unique_lookups.items():
            cached = geo_cache.get(fck)
            if cached and cached.get("status") == "OK":
                stage_results[fck] = cached
                stage_cache_hits += 1
            else:
                stage_pending.append((fck, spec))

        def run_area(spec):
            return single_geocode_call(spec["query"], key, limiter=limiter, **spec["score_kwargs"])

        stage_requests = 0
        done = stage_cache_hits
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(run_area, spec): (fck, spec) for fck, spec in stage_pending}
            last_ui = time.monotonic()
            for fut in as_completed(futures):
                fck, spec = futures[fut]
                try:
                    geo = fut.result()
                except Exception as exc:
                    geo = {
                        "lat": None, "lng": None, "status": "WORKER_ERROR", "location_type": None,
                        "formatted_address": "", "place_id": "", "google_country": "", "google_postal": "",
                        "google_city": "", "google_admin1": "", "google_admin2": "", "result_types": [],
                        "partial_match": False, "error_message": str(exc), "api_calls": 0, "quota_events": 0,
                        "status_history": ["WORKER_ERROR"], "match_score": 0,
                    }
                geo["query_used"] = spec["query"]
                stage_results[fck] = geo
                stage_requests += int(geo.get("api_calls", 0))
                if geo.get("status") == "OK":
                    geo_cache[fck] = geo
                done += 1
                now = time.monotonic()
                if now - last_ui >= 0.15 or done == len(unique_lookups):
                    snap = limiter.snapshot()
                    progress.progress(done / len(unique_lookups), text=f"{stage_labels[stage]}: {done:,} of {len(unique_lookups):,}")
                    status_area.caption(
                        f"{snap['total_requests']:,} Google requests | rolling minute {snap['current_rolling_qpm']:,}/{target_qpm:,} | "
                        f"peak {snap['peak_rolling_qpm']:,} | quota responses {snap['over_query_limit_events']:,}"
                    )
                    last_ui = now

        accepted = 0
        rejected = 0
        for ck, fck in stage_to_primary.items():
            geo = stage_results.get(fck)
            if not geo:
                continue
            acc = accum[ck]
            acc["api_calls"] += int(geo.get("api_calls", 0))
            acc["quota_events"] += int(geo.get("quota_events", 0))
            acc["status_history"].extend([f"{stage}:{x}" for x in geo.get("status_history", [])])
            if geo.get("error_message"):
                acc["last_error"] = geo.get("error_message", "")
            acc["last_status"] = geo.get("status", acc.get("last_status", ""))

            job = primary_jobs[ck]
            is_accepted, actual_granularity, rejection_reason = assess_area_candidate(stage, geo, job)
            if is_accepted:
                accepted += 1
                granularity = actual_granularity
                method = method_names[stage]
                fallback_action = (
                    "Use the fallback coordinates as an area-level location. Correct the source street/postcode and rerun "
                    "if street-level accuracy is required for this exposure."
                )
                final_geo = {
                    **geo,
                    "method": method,
                    "granularity": granularity,
                    "fallback": True,
                    "addr_only_better": False,
                    "detail": f"Street-level search was unresolved; resolved using {stage_labels[stage].lower()}.",
                    "api_calls": acc["api_calls"], "quota_events": acc["quota_events"],
                    "status_history": acc["status_history"], "rejected_candidates": acc["rejected_candidates"],
                    "error_reason": "", "action": fallback_action,
                }
                final_geo["quality_flag"] = _quality_flags(
                    final_geo, job.get("_cc"), job.get("_postal"), bool(job.get("_street")), granularity, method
                )
                final_results[ck] = final_geo
            elif geo.get("status") == "OK":
                rejected += 1
                acc["rejected_candidates"] += 1
                acc["last_status"] = "GEOGRAPHY_MISMATCH"
                acc["status_history"].append(f"{stage}:REJECTED_GEOGRAPHY")
                if rejection_reason:
                    acc["last_error"] = rejection_reason

        stage_stats[stage_labels[stage]] = {
            "lookups": len(unique_lookups), "requests": stage_requests, "cache_hits": stage_cache_hits,
            "accepted": accepted, "rejected": rejected,
        }

    # Finalise unresolved unique jobs with a precise reason/action.
    for ck in unresolved_keys():
        job = primary_jobs[ck]
        previous = final_results[ck]
        acc = accum[ck]
        status = acc.get("last_status") or previous.get("status", "ZERO_RESULTS")
        if acc["rejected_candidates"] > 0 and status in {"ZERO_RESULTS", "NO_STREET_FOR_PRIMARY", "GEOGRAPHY_MISMATCH", "OK"}:
            status = "GEOGRAPHY_MISMATCH"
        reason, action = failure_reason_and_action(
            status, job, acc["last_error"], acc["stages_tried"], acc["rejected_candidates"]
        )
        final_results[ck] = {
            **previous,
            "status": status,
            "method": "failed",
            "granularity": "",
            "fallback": False,
            "api_calls": acc["api_calls"], "quota_events": acc["quota_events"],
            "status_history": acc["status_history"], "rejected_candidates": acc["rejected_candidates"],
            "error_reason": reason, "action": action,
        }

    progress.progress(1.0, text="Geocoding and geographic fallbacks complete")

    # Apply unique results back to source rows in one hash-based join.
    lookup_rows = []
    geo_by_key = {}
    for _, job_series in jobs_df.iterrows():
        job = job_series.to_dict()
        ck = primary_cache_key(job)
        geo = final_results.get(ck)
        if not geo:
            continue
        key_tuple = tuple(job[c] for c in dedupe_cols)
        geo_by_key[key_tuple] = geo
        lookup_rows.append({
            **{c: job[c] for c in dedupe_cols},
            "_GeoStatus": geo.get("status", ""), "_GeoAttempts": int(geo.get("api_calls", 0)),
            "_GeoQuotaEvents": int(geo.get("quota_events", 0)),
            "_GeoStatusHistory": " > ".join(map(str, geo.get("status_history", []))),
            "_GeoMethod": geo.get("method", ""), "_GeoGranularity": geo.get("granularity", ""),
            "_GeoQueryUsed": geo.get("query_used", ""), "_GeoFallbackUsed": bool(geo.get("fallback", False)),
            "_GeoRejectedCandidates": int(geo.get("rejected_candidates", 0)),
            "_GoogleLocationType": geo.get("location_type") or "",
            "_GoogleFormattedAddress": geo.get("formatted_address", ""), "_GooglePlaceID": geo.get("place_id", ""),
            "_GoogleCountryCode": geo.get("google_country", ""), "_GooglePostalCode": geo.get("google_postal", ""),
            "_GoogleCity": geo.get("google_city", ""), "_GoogleAdmin1": geo.get("google_admin1", ""),
            "_GoogleAdmin2": geo.get("google_admin2", ""), "_GooglePartialMatch": bool(geo.get("partial_match", False)),
            "_AddrOnlyBetter": bool(geo.get("addr_only_better", False)), "_GeoLat": geo.get("lat"), "_GeoLng": geo.get("lng"),
            "_GeoErrorReason": geo.get("error_reason", ""), "_GeoAction": geo.get("action", ""),
        })

    if lookup_rows:
        lookup = pd.DataFrame(lookup_rows).set_index(dedupe_cols)
        target_rows = result.loc[needs, dedupe_cols]
        target_keys = pd.MultiIndex.from_frame(target_rows)
        matched = lookup.reindex(target_keys)
        matched.index = target_rows.index

        assignments = {
            "GeoStatus": "_GeoStatus", "GeoAttempts": "_GeoAttempts", "GeoQuotaEvents": "_GeoQuotaEvents",
            "GeoStatusHistory": "_GeoStatusHistory", "GeoMethod": "_GeoMethod", "GeoGranularity": "_GeoGranularity",
            "GeoQueryUsed": "_GeoQueryUsed", "GeoFallbackUsed": "_GeoFallbackUsed",
            "GeoRejectedCandidates": "_GeoRejectedCandidates", "GoogleLocationType": "_GoogleLocationType",
            "GoogleFormattedAddress": "_GoogleFormattedAddress", "GooglePlaceID": "_GooglePlaceID",
            "GoogleCountryCode": "_GoogleCountryCode", "GooglePostalCode": "_GooglePostalCode",
            "GoogleCity": "_GoogleCity", "GoogleAdmin1": "_GoogleAdmin1", "GoogleAdmin2": "_GoogleAdmin2",
            "GooglePartialMatch": "_GooglePartialMatch", "AddrOnlyBetter": "_AddrOnlyBetter",
            "GeoErrorReason": "_GeoErrorReason", "GeoAction": "_GeoAction",
        }
        for out_col, in_col in assignments.items():
            vals = matched[in_col]
            if out_col in {"GeoAttempts", "GeoQuotaEvents", "GeoRejectedCandidates"}:
                result.loc[needs, out_col] = vals.fillna(0).astype(int).values
            elif out_col in {"GeoFallbackUsed", "GooglePartialMatch", "AddrOnlyBetter"}:
                result.loc[needs, out_col] = vals.fillna(False).astype(bool).values
            else:
                result.loc[needs, out_col] = vals.fillna("").values

        ok_coords = matched["_GeoStatus"].eq("OK") & pd.to_numeric(matched["_GeoLat"], errors="coerce").notna() & pd.to_numeric(matched["_GeoLng"], errors="coerce").notna()
        ok_idx = matched.index[ok_coords]
        result.loc[ok_idx, "Latitude"] = matched.loc[ok_idx, "_GeoLat"].values
        result.loc[ok_idx, "Longitude"] = matched.loc[ok_idx, "_GeoLng"].values

        for idx in target_rows.index:
            key_tuple = tuple(result.at[idx, c] for c in dedupe_cols)
            geo = geo_by_key.get(key_tuple)
            if not geo:
                continue
            result.at[idx, "GeoQualityFlag"] = _quality_flags(
                geo, result.at[idx, "_cc"], result.at[idx, "_postal"], bool(result.at[idx, "_street"]),
                geo.get("granularity"), geo.get("method")
            )

    # Explicit errors for rows that were never searchable.
    for idx in result.index[result["GeoStatus"].eq("NO_SEARCHABLE_ADDRESS")]:
        reason, action = failure_reason_and_action("NO_SEARCHABLE_ADDRESS", {
            "_street": result.at[idx, "_street"], "_city": result.at[idx, "_city"], "_postal": result.at[idx, "_postal"],
            "_admin1": result.at[idx, "_admin1"], "_admin2": result.at[idx, "_admin2"],
        })
        result.at[idx, "GeoErrorReason"] = reason
        result.at[idx, "GeoAction"] = action

    final_snap = limiter.snapshot()
    unique_success = sum(1 for g in final_results.values() if g.get("status") == "OK")
    unique_failed = total - unique_success
    stage_lines = []
    for name, stats in stage_stats.items():
        extra = ""
        if "accepted" in stats:
            extra = f", {stats['accepted']:,} resolved"
        stage_lines.append(f"{name}: {stats['lookups']:,} lookup(s), {stats['requests']:,} request(s){extra}")
    status_area.markdown(
        f"**{unique_success:,}** unique locations resolved; **{unique_failed:,}** unresolved. "
        f"**{final_snap['total_requests']:,}** actual Google requests; peak rolling minute "
        f"**{final_snap['peak_rolling_qpm']:,}/{target_qpm:,}**; **{final_snap['over_query_limit_events']:,}** quota response(s).\n\n"
        + "  \n".join(stage_lines)
    )

    comp = None
    if not skip:
        cidx = had_coords[had_coords].index
        if len(cidx) > 0:
            comp = pd.DataFrame({
                "AddressID": result.loc[cidx, "AddressID"].values,
                "StreetAddress": result.loc[cidx, "StreetAddress"].values,
                "Original_Latitude": orig_lats.values, "Original_Longitude": orig_lngs.values,
                "New_Latitude": result.loc[cidx, "Latitude"].values, "New_Longitude": result.loc[cidx, "Longitude"].values,
                "GeoGranularity": result.loc[cidx, "GeoGranularity"].values,
                "GoogleLocationType": result.loc[cidx, "GoogleLocationType"].values,
                "GeoMethod": result.loc[cidx, "GeoMethod"].values,
                "GeoQualityFlag": result.loc[cidx, "GeoQualityFlag"].values,
            })
            dists = []
            for _, r in comp.iterrows():
                if valid_coordinate_pair(r["Original_Latitude"], r["Original_Longitude"]) and valid_coordinate_pair(r["New_Latitude"], r["New_Longitude"]):
                    dists.append(round(haversine_m(float(r["Original_Latitude"]), float(r["Original_Longitude"]), float(r["New_Latitude"]), float(r["New_Longitude"])), 2))
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
    priority_by_granularity = {"COUNTRY": 2, "ADMIN1": 2, "ADMIN2": 3, "CITY": 3, "POSTAL_CODE": 4, "APPROXIMATE": 4}
    label_by_granularity = {
        "COUNTRY": "Country centroid", "ADMIN1": "State/region centroid", "ADMIN2": "County/district centroid",
        "CITY": "City centroid", "POSTAL_CODE": "Postcode/ZIP centroid", "APPROXIMATE": "Approximate area result",
    }

    for _, row in result_df.iterrows():
        status = clean_text(row.get("GeoStatus", ""))
        if status == "EXISTING_SKIPPED":
            continue
        granularity = clean_text(row.get("GeoGranularity", ""))
        addr_id = clean_text(row.get("AddressID", ""))
        tiv = float(row.get("_TIV", 0)) if has_tiv and pd.notna(row.get("_TIV")) else None
        pct = (tiv / total_tiv * 100) if tiv and total_tiv > 0 else None
        dist_m = distance_lookup.get(addr_id)
        dist_km = dist_m / 1000 if dist_m is not None else None

        entry = {
            "AddressID": addr_id,
            "StreetAddress": clean_text(row.get("StreetAddress", "")),
            "CityName": clean_text(row.get("CityName", "")),
            "PostalCode": clean_text(row.get("PostalCode", "")),
            "GeoStatus": status,
            "GeoGranularity": granularity,
            "GeoMethod": clean_text(row.get("GeoMethod", "")),
            "GeoQualityFlag": clean_text(row.get("GeoQualityFlag", "")),
            "GeoErrorReason": clean_text(row.get("GeoErrorReason", "")),
            "GeoAction": clean_text(row.get("GeoAction", "")),
            "Distance_km": dist_km, "TIV": tiv, "TIV_Pct": pct,
        }

        if status != "OK" or not valid_coordinate_pair(row.get("Latitude"), row.get("Longitude")):
            entry.update({"Category": "Unresolved", "Priority": 1, "Recommendation": entry["GeoAction"]})
            recs.append(entry)
        elif dist_km is not None and dist_km >= 50:
            entry.update({"Category": "Large coordinate discrepancy", "Priority": 1,
                          "Recommendation": f"New coordinates are {dist_km:,.1f} km from the original. Verify which location is correct."})
            recs.append(entry)
        elif granularity in priority_by_granularity:
            entry.update({"Category": label_by_granularity[granularity], "Priority": priority_by_granularity[granularity],
                          "Recommendation": f"Coordinates are usable but only at {label_by_granularity[granularity].lower()} level. Review if street-level accuracy is material."})
            recs.append(entry)
        elif dist_km is not None and dist_km >= 5:
            entry.update({"Category": "Notable coordinate difference", "Priority": 3,
                          "Recommendation": f"New coordinates are {dist_km:,.1f} km from the original. Check if the difference is material."})
            recs.append(entry)
        elif clean_text(row.get("GoogleLocationType", "")) in {"GEOMETRIC_CENTER", "RANGE_INTERPOLATED"}:
            entry.update({"Category": "Below rooftop", "Priority": 5,
                          "Recommendation": "Street-level coordinates were returned below rooftop precision. Review only where exact location is material."})
            recs.append(entry)
        elif clean_text(row.get("GeoQualityFlag", "")):
            entry.update({"Category": "Source data flag", "Priority": 6,
                          "Recommendation": clean_text(row.get("GeoQualityFlag", ""))})
            recs.append(entry)

    if not recs:
        return None
    return pd.DataFrame(recs).sort_values(["Priority", "Category"]).reset_index(drop=True)


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
    if res_df is None:
        return

    st.divider()
    st.header("Results")

    status = res_df["GeoStatus"].fillna("")
    valid_coords = res_df.apply(lambda r: valid_coordinate_pair(r.get("Latitude"), r.get("Longitude")), axis=1)
    resolved = status.isin(["OK", "EXISTING_SKIPPED"]) & valid_coords
    unresolved = ~resolved
    gran = res_df["GeoGranularity"].fillna("")

    fallback_mask = res_df.get("GeoFallbackUsed", pd.Series(False, index=res_df.index)).fillna(False).astype(bool) & resolved
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Rows with coordinates", f"{int(resolved.sum()):,}", f"{(resolved.mean()*100 if len(res_df) else 0):.1f}%")
    m2.metric("Street level", f"{int((gran == 'STREET').sum()):,}")
    m3.metric("Postcode / ZIP", f"{int((gran == 'POSTAL_CODE').sum()):,}")
    m4.metric("City / admin centroid", f"{int(gran.isin(['CITY','ADMIN2','ADMIN1']).sum()):,}")
    m5.metric("Fallback-resolved", f"{int(fallback_mask.sum()):,}")
    m6.metric("Unresolved", f"{int(unresolved.sum()):,}")

    d1, d2, d3 = st.columns([1, 1, 1])
    with d1:
        st.download_button(
            "Download full geocoded CSV", res_df.to_csv(index=False),
            "geocoded_output.csv", "text/csv", type="primary", key="dl_geocoded"
        )
    with d2:
        unresolved_df = res_df.loc[unresolved].copy()
        if len(unresolved_df):
            st.download_button(
                "Download unresolved rows", unresolved_df.to_csv(index=False),
                "geocode_unresolved.csv", "text/csv", key="dl_unresolved"
            )
    with d3:
        fallback_df = res_df.loc[fallback_mask].copy()
        if len(fallback_df):
            st.download_button(
                "Download fallback-resolved rows", fallback_df.to_csv(index=False),
                "geocode_fallback_resolved.csv", "text/csv", key="dl_fallback"
            )

    if unresolved.any():
        st.error(
            f"{int(unresolved.sum()):,} row(s) still have no usable coordinates. "
            "Each unresolved row includes GeoErrorReason and GeoAction so the next step is explicit."
        )
        failure_summary = (
            res_df.loc[unresolved, ["GeoStatus", "GeoErrorReason"]]
            .fillna("")
            .groupby(["GeoStatus", "GeoErrorReason"], dropna=False)
            .size().reset_index(name="Rows").sort_values("Rows", ascending=False)
        )
        st.dataframe(failure_summary, use_container_width=True, hide_index=True)
        error_cols = [c for c in [
            "AddressID", "StreetAddress", "CityName", "PostalCode", "CountryCode", "GeoStatus",
            "GeoErrorReason", "GeoAction", "GeoStatusHistory", "GeoRejectedCandidates"
        ] if c in res_df.columns]
        st.dataframe(res_df.loc[unresolved, error_cols], use_container_width=True, hide_index=True)

    # Quality mix makes centroid use visible instead of silently blending it into rooftop coordinates.
    st.subheader("Resolution quality")
    quality = (
        res_df.assign(_Resolution=res_df["GeoGranularity"].replace("", "UNRESOLVED"))
        .groupby("_Resolution", dropna=False).size().rename("Rows").reset_index()
        .rename(columns={"_Resolution": "Resolution"}).sort_values("Rows", ascending=False)
    )
    quality["Percent"] = (quality["Rows"] / len(res_df) * 100).round(1)
    st.dataframe(quality, use_container_width=True, hide_index=True)

    has_tiv = "_TIV" in res_df.columns
    total_tiv = res_df["_TIV"].sum() if has_tiv else 0
    rec_df = build_recommendations(res_df, comp_df, has_tiv=has_tiv, total_tiv=total_tiv)
    if rec_df is not None and len(rec_df):
        st.subheader("Review queue")
        st.caption("Only unresolved, lower-granularity, materially moved, or otherwise flagged rows appear here.")
        counts = rec_df.groupby(["Priority", "Category"]).size().reset_index(name="Rows").sort_values(["Priority", "Rows"], ascending=[True, False])
        st.dataframe(counts[["Category", "Rows"]], use_container_width=True, hide_index=True)
        display_cols = [c for c in rec_df.columns if c != "Priority"]
        with st.expander(f"Open review queue ({len(rec_df):,} rows)", expanded=False):
            st.dataframe(rec_df[display_cols], use_container_width=True, hide_index=True)
        st.download_button(
            "Download review queue", rec_df[display_cols].to_csv(index=False),
            "geocode_review_queue.csv", "text/csv", key="dl_rec"
        )
    else:
        st.success("No rows require review.")

    with st.expander("Full result preview", expanded=False):
        st.dataframe(res_df.head(1000), use_container_width=True, hide_index=True)
        if len(res_df) > 1000:
            st.caption("Preview limited to the first 1,000 rows. The download contains the full dataset.")

    md_cols = [c for c in ["AddressID", "StreetAddress", "CityName", "PostalCode", "GeoGranularity", "Latitude", "Longitude"] if c in res_df.columns]
    md = res_df[md_cols].copy()
    md["Latitude"] = pd.to_numeric(md["Latitude"], errors="coerce")
    md["Longitude"] = pd.to_numeric(md["Longitude"], errors="coerce")
    md = md.dropna(subset=["Latitude", "Longitude"])
    if len(md):
        with st.expander("Map preview", expanded=False):
            if len(md) > 10000:
                md = md.sample(10000, random_state=42)
                st.caption("Map sampled to 10,000 points for browser performance. Downloads retain every row.")
            clat, clng = md["Latitude"].mean(), md["Longitude"].mean()
            span = max(md["Latitude"].max() - md["Latitude"].min(), md["Longitude"].max() - md["Longitude"].min())
            zoom = 14 if span < 0.01 else 11 if span < 0.1 else 8 if span < 1 else 5 if span < 10 else 2
            point_size = st.slider("Map point size", 2, 16, 5, 1, key="result_point_size")
            layer = pdk.Layer(
                "ScatterplotLayer", data=md, get_position=["Longitude", "Latitude"], get_radius=100,
                radius_min_pixels=point_size, radius_max_pixels=point_size * 3,
                get_fill_color=[65, 105, 225, 180], pickable=True, auto_highlight=True
            )
            tooltip = {
                "html": "<b>{StreetAddress}</b><br/>{CityName} {PostalCode}<br/>Resolution: {GeoGranularity}<br/>Lat: {Latitude}<br/>Lng: {Longitude}",
                "style": {"backgroundColor": "#1a1a2e", "color": "white", "fontSize": "12px"},
            }
            st.pydeck_chart(pdk.Deck(
                layers=[layer], initial_view_state=pdk.ViewState(latitude=clat, longitude=clng, zoom=zoom, pitch=0),
                tooltip=tooltip, map_provider="carto", map_style="light"
            ))

    if not run_skip_existing:
        st.subheader("Coordinate comparison")
        if comp_df is not None and len(comp_df):
            st.dataframe(comp_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download coordinate comparison", comp_df.to_csv(index=False),
                "geocode_comparison_report.csv", "text/csv", key="dl_comp"
            )
        else:
            st.info("No original valid coordinate pairs were available for comparison.")


# =============================================================================
# MAIN APP
# =============================================================================

st.header("1. Upload")
uploaded = st.file_uploader(
    "Exposure file", type=["csv", "xlsx", "xls", "txt", "tsv"],
    help="CSV, Excel, TSV, or delimited text. Source columns not used for geocoding are preserved in the output."
)

if uploaded:
    file_bytes = uploaded.getvalue()
    file_signature = hashlib.sha256(file_bytes).hexdigest()
    if st.session_state.get("_active_file_signature") != file_signature:
        st.session_state["_active_file_signature"] = file_signature
        st.session_state["_last_geocode_results"] = None
        st.session_state["_last_geocode_comparison"] = None
        st.session_state["_last_input_signature"] = None

    raw_df, err = read_raw_file(uploaded)
    if err:
        st.error(f"The file could not be read. {err} Check the file type, delimiter, and whether the file opens normally in Excel/text editor.")
        st.stop()

    detected_header = guess_header_row(raw_df)
    st.success(
        f"Loaded {uploaded.name}. Detected encoding: {raw_df.attrs.get('encoding', 'n/a')}. "
        f"Likely header row: {detected_header}."
    )
    with st.expander("Raw file preview and header selection", expanded=detected_header != 0):
        preview = min(15, len(raw_df))
        disp = raw_df.head(preview).copy()
        disp.index = [f"Row {i}" for i in range(preview)]
        st.dataframe(disp, use_container_width=True)
        header_row = st.number_input(
            "Header row number", 0, max(0, len(raw_df) - 2), int(detected_header), 1,
            help="Change this only if the highlighted/detected row is not the actual column header."
        )
    if detected_header == 0:
        header_row = 0 if 'header_row' not in locals() else header_row

    df_h, h_err = apply_header(raw_df, int(header_row))
    if h_err:
        st.error(f"Header selection is invalid: {h_err} Choose the row containing unique, non-blank column names.")
        st.stop()
    avail = list(df_h.columns)
    st.caption(f"{len(df_h):,} data rows and {len(avail):,} source columns after the selected header.")

    st.header("2. Map fields")
    TEMPLATES = {
        "Terrorism 4020": {
            "fields": ["StreetAddress", "CityName", "PostalCode", "CountryCode"],
            "description": "Recommended for the Terrorism 4020 RMS-style files. Street, city, postcode and country are auto-detected where possible."
        },
        "Full RiskLink export": {
            "fields": [OPTIONAL_ID, "StreetAddress", "Latitude", "Longitude", "CityName", "Admin2Name", "Admin1Name", "PostalCode", "CountryCode"],
            "description": "Use all available address, admin, identifier and coordinate fields."
        },
        "Coordinates only (re-geocode)": {
            "fields": ["StreetAddress", "Latitude", "Longitude", "CityName", "PostalCode", "CountryCode"],
            "description": "Compare new Google coordinates with existing coordinates."
        },
        "Minimal": {
            "fields": ["StreetAddress", "CityName", "PostalCode"],
            "description": "Use whatever street/city/postcode information is available. StreetAddress is not mandatory."
        },
        "Custom": {"fields": ALL_FIELDS, "description": "Expose every canonical field for manual mapping."},
    }
    template = st.selectbox("Mapping template", list(TEMPLATES.keys()), index=0)
    st.caption(TEMPLATES[template]["description"])

    opts = [UNMAPPED] + avail
    mapping = {}
    active_fields = TEMPLATES[template]["fields"]
    mapping_guesses = {field: guess_column(field, avail) for field in active_fields}
    searchable_guess_count = sum(
        mapping_guesses.get(f) != UNMAPPED
        for f in ["StreetAddress", "CityName", "PostalCode", "Admin2Name", "Admin1Name"]
        if f in active_fields
    )
    needs_mapping_attention = searchable_guess_count == 0 or (
        "StreetAddress" in active_fields and mapping_guesses.get("StreetAddress") == UNMAPPED
    )
    with st.expander("Review column mapping", expanded=needs_mapping_attention):
        field_groups = [active_fields[i:i + 4] for i in range(0, len(active_fields), 4)]
        for group in field_groups:
            cols = st.columns(len(group))
            for col_ui, field in zip(cols, group):
                guessed = mapping_guesses[field]
                idx = opts.index(guessed) if guessed in opts else 0
                with col_ui:
                    mapping[field] = st.selectbox(field, opts, idx, key=f"m_{field}")
        st.caption("Fields not shown by the selected template are ignored unless you switch to Custom.")
    mapped_summary = [f"{field} ← {mapping_guesses[field]}" for field in active_fields if mapping_guesses.get(field) != UNMAPPED]
    if mapped_summary:
        st.caption("Initial auto-detection: " + " · ".join(mapped_summary))

    for field in ALL_FIELDS:
        if field not in mapping:
            mapping[field] = UNMAPPED

    mapped_vals = [c for c in mapping.values() if c != UNMAPPED]
    if len(mapped_vals) != len(set(mapped_vals)):
        seen, dupes = set(), set()
        for c in mapped_vals:
            if c in seen:
                dupes.add(c)
            seen.add(c)
        st.error(
            f"A source column is mapped more than once: {', '.join(sorted(dupes))}. "
            "Map each source column to only one canonical field."
        )
        st.stop()

    searchable_mapped = any(mapping.get(f) != UNMAPPED for f in ["StreetAddress", "CityName", "PostalCode", "Admin2Name", "Admin1Name"])
    if not searchable_mapped:
        st.error("No searchable location field is mapped. Map at least StreetAddress, CityName, PostalCode, Admin2Name, or Admin1Name.")
        st.stop()

    value_columns = st.multiselect(
        "Value/TIV columns (optional)", options=avail, default=[],
        help="Selected columns are summed into _TIV so review queues can show portfolio materiality."
    )

    df_m = apply_column_mapping(df_h, mapping)
    for field in LOCATION_FIELDS + ["StreetAddress"]:
        if field not in df_m.columns:
            df_m[field] = ""
    if "AddressID" not in df_m.columns:
        df_m["AddressID"] = [f"ROW_{i + 1}" for i in range(len(df_m))]
    for field in COORD_FIELDS:
        if field not in df_m.columns:
            df_m[field] = ""

    if value_columns:
        df_m["_TIV"] = 0.0
        for vc in value_columns:
            df_m["_TIV"] += pd.to_numeric(df_m[vc], errors="coerce").fillna(0)
        st.caption(f"Portfolio value from selected columns: {df_m['_TIV'].sum():,.0f}")

    with st.expander("Mapped data preview", expanded=False):
        st.dataframe(df_m.head(20), use_container_width=True, hide_index=True)

    st.header("3. Preflight")
    val = validate_dataframe(df_m, skip_existing)
    stats = val["stats"]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Source rows", f"{stats['total_rows']:,}")
    c2.metric("Unique jobs", f"{stats['to_geocode']:,}")
    c3.metric("Valid existing", f"{stats['already_geocoded']:,}")
    c4.metric("No street", f"{stats['blank_addresses']:,}")
    c5.metric("Unsearchable", f"{stats['unsearchable']:,}")

    if val["warnings"]:
        st.warning(f"Preflight found {len(val['warnings'])} source-data condition(s). They do not block the run.")
        with st.expander("Preflight details", expanded=False):
            for warning in val["warnings"]:
                st.markdown(f"- {warning}")
            for label, fdf in val["flagged_rows"].items():
                st.markdown(f"**{label}**")
                st.dataframe(fdf, use_container_width=True, hide_index=True)
    else:
        st.success("Preflight found no obvious source-data issues.")

    fallback_options = {
        "postal": fallback_postal,
        "city": fallback_city,
        "admin": fallback_admin,
        "country": fallback_country,
    }
    enabled_fallbacks = [name for name, enabled in [
        ("postcode/ZIP", fallback_postal), ("city", fallback_city), ("county/state/region", fallback_admin), ("country", fallback_country)
    ] if enabled]
    st.caption(
        f"Unresolved street searches will fall back through: {', '.join(enabled_fallbacks) if enabled_fallbacks else 'no area-centroid fallbacks'}. "
        "Fallback lookups are deduplicated across the file."
    )
    if enabled_fallbacks:
        st.info(
            "Fallbacks progressively trade precision for coverage. The app records the level actually returned by Google, "
            "so a city result from a city+postcode query is labelled CITY rather than POSTAL_CODE. Stronger results are never replaced by coarser fallbacks."
        )

    signature_payload = repr((
        file_signature, int(header_row), tuple(sorted(mapping.items())), tuple(value_columns), skip_existing,
        tuple(sorted(fallback_options.items()))
    )).encode("utf-8")
    input_signature = hashlib.sha256(signature_payload).hexdigest()

    st.header("4. Run")
    b1, b2 = st.columns([1, 2])
    primary_floor_min = (stats['to_geocode'] / max(1, target_qpm))
    with b1:
        start_run = st.button(
            f"Geocode {stats['to_geocode']:,} unique location{'s' if stats['to_geocode'] != 1 else ''}",
            disabled=not api_key or stats['to_geocode'] == 0, type="primary", use_container_width=True
        )
    with b2:
        st.caption(
            f"QPM ceiling {target_qpm:,} · workers {max_workers} · theoretical first-pass floor ~{primary_floor_min:.1f} min before network latency/fallbacks. "
            f"Existing valid coordinates are {'kept' if skip_existing else 're-geocoded'}."
        )

    if start_run:
        if not api_key:
            st.error("Enter the Google Geocoding API key in the sidebar before starting.")
        else:
            # Avoid leaving old output visible beneath a new run.
            st.session_state["_last_geocode_results"] = None
            st.session_state["_last_geocode_comparison"] = None
            res_df, comp_df = process_dataframe(
                df_m, api_key, target_qpm, max_workers, skip_existing, fallback_options
            )
            if res_df is not None:
                st.session_state["_last_geocode_results"] = res_df
                st.session_state["_last_geocode_comparison"] = comp_df
                st.session_state["_last_geocode_skip_existing"] = skip_existing
                st.session_state["_last_input_signature"] = input_signature

    saved_results = st.session_state.get("_last_geocode_results")
    if saved_results is not None and st.session_state.get("_last_input_signature") != input_signature:
        st.info("The file, mapping, fallback policy, or existing-coordinate setting has changed since the last run. Previous results are hidden to avoid showing stale output.")
    else:
        render_geocode_results(
            saved_results,
            st.session_state.get("_last_geocode_comparison"),
            st.session_state.get("_last_geocode_skip_existing", True),
        )

else:
    st.info("Upload an exposure file to begin. The app auto-detects common RMS/Terrorism 4020 columns and the likely header row; only genuinely ambiguous mapping needs human input.")
    st.markdown(
        """
        **Workflow**

        1. Upload the file and confirm the detected header.
        2. Review the auto-mapped address fields.
        3. Check the non-blocking preflight summary.
        4. Run geocoding. Strong street results stop immediately; unresolved locations progressively fall back to postcode, city and admin centroids according to the sidebar policy.
        5. Download the full output or work only from the explicit unresolved/review queues.

        The output always records the resolution level (`GeoGranularity`), method, Google status history, quality flags, and actionable failure reason where applicable.
        """
    )
