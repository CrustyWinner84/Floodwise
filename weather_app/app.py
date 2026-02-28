from flask import Flask, request, jsonify, render_template, make_response
import requests
import re
import logging
import os
import sqlite3 as _sqlite3
import time
import functools
from datetime import datetime, timedelta

app = Flask(__name__, template_folder='templates')

# Basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('weather_app')


def get_elevation(lat: float, lon: float):
    """Get elevation (meters) for a coordinate using Open-Elevation as a best-effort lookup."""
    # 0) Try Google Earth Engine (if enabled via env var). This provides high-resolution DEMs
    #    Requires OAuth/service account setup and the `earthengine-api` package.
    #    Set USE_EARTH_ENGINE=1 and ensure ee.Initialize() works in your environment.
    try:
        if os.environ.get('USE_EARTH_ENGINE'):
            try:
                import ee
                try:
                    ee.Initialize()
                except Exception:
                    # If not initialized, attempt a non-interactive init if credentials provided
                    creds_path = os.environ.get('GOOGLE_EARTH_ENGINE_CREDENTIALS')
                    if creds_path:
                        try:
                            ee.Initialize()
                        except Exception:
                            logger.debug('Earth Engine initialize failed; ensure credentials are available')
                # Use SRTM or other global DEM available in Earth Engine
                dem = ee.Image('USGS/SRTMGL1_003')
                pt = ee.Geometry.Point(lon, lat)
                samp = dem.sample(pt, 30).first()
                if samp:
                    # band name is typically 'elevation'
                    val = samp.get('elevation').getInfo()
                    if val is not None:
                        return float(val)
            except Exception:
                logger.debug('Earth Engine elevation lookup unavailable or failed')
    except Exception:
        pass

    # 1) If Google Elevation API key is set, use it (higher reliability)
    gkey = os.environ.get('GOOGLE_ELEVATION_KEY')
    if gkey:
        try:
            url = 'https://maps.googleapis.com/maps/api/elevation/json'
            params = {'locations': f'{lat},{lon}', 'key': gkey}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = data.get('results') or []
            if results:
                elev = results[0].get('elevation')
                if elev is not None:
                    return float(elev)
        except Exception:
            logger.debug('Google Elevation lookup failed for %s,%s', lat, lon)

    # Optional: try OpenTopography (if configured)
    try:
        ot = get_elevation_opentopography(lat, lon)
        if ot is not None:
            return float(ot)
    except Exception:
        logger.debug('OpenTopography lookup skipped or failed')

    # 2) Open-Meteo elevation API — fast and reliable (same CDN as weather API)
    try:
        url = 'https://api.open-meteo.com/v1/elevation'
        params = {'latitude': lat, 'longitude': lon}
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        elevs = data.get('elevation', [])
        if elevs and elevs[0] is not None:
            return float(elevs[0])
    except Exception:
        logger.debug('Open-Meteo elevation API failed for %s,%s', lat, lon)

    # 3) Last-resort: Open-Elevation public API (can be slow or unreliable)
    try:
        url = 'https://api.open-elevation.com/api/v1/lookup'
        params = {'locations': f'{lat},{lon}'}
        resp = requests.get(url, params=params, timeout=4)
        resp.raise_for_status()
        data = resp.json()
        results = data.get('results') or []
        if results:
            elev = results[0].get('elevation')
            if elev is not None:
                return float(elev)
    except Exception:
        logger.debug('Open-Elevation lookup failed for %s,%s', lat, lon)

    return None


def get_elevation_opentopography(lat: float, lon: float):
    """Optional wrapper to call OpenTopography REST endpoints.

    OpenTopography has a variety of APIs/tooling. This function will only run if
    the environment variable `OPENTOPO_API_KEY` and `OPENTOPO_ENDPOINT_TEMPLATE` are set.

    `OPENTOPO_ENDPOINT_TEMPLATE` should be a format string containing `{lat}`, `{lon}`,
    and `{key}` where appropriate. Example (not exact):
      "https://portal.opentopography.org/API/globaldem?demtype=SRTMGL1&south={lat}&north={lat}&west={lon}&east={lon}&outputFormat=JSON&API_Key={key}"

    The API specifics can vary by deployment. If you have an OpenTopography API key
    and a working endpoint template, set the env vars and this function will be used
    as an additional DEM source.
    """
    key = os.environ.get('OPENTOPO_API_KEY')
    tpl = os.environ.get('OPENTOPO_ENDPOINT_TEMPLATE')
    if not key or not tpl:
        return None
    try:
        url = tpl.format(lat=lat, lon=lon, key=key)
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # The exact JSON structure depends on the endpoint; try common keys
        if isinstance(data, dict):
            # Common possibilities
            for k in ('elevation', 'elev', 'z', 'value'):
                if k in data:
                    try:
                        return float(data[k])
                    except Exception:
                        pass
            # If results array present, inspect first item
            if 'results' in data and isinstance(data['results'], list) and data['results']:
                first = data['results'][0]
                for k in ('elevation', 'elev', 'z', 'value'):
                    if k in first:
                        try:
                            return float(first[k])
                        except Exception:
                            pass
    except Exception:
        logger.debug('OpenTopography request failed for %s,%s', lat, lon)
    return None


@functools.lru_cache(maxsize=256)
def geocode(location: str):
    """Geocode a free-form location string to (lat, lon, display_name, elevation).
    Results are LRU-cached so repeated lookups of the same location are instant.
    """
    # Common landmark to city mappings for fallback
    landmark_map = {
        'eiffel tower': 'Paris',
        'statue of liberty': 'New York',
        'big ben': 'London',
        'colosseum': 'Rome',
        'taj mahal': 'Agra',
        'christ the redeemer': 'Rio de Janeiro',
        'great wall': 'Beijing',
        'pyramids': 'Cairo',
    }

    def extract_city_state_from_address(addr):
        """Try to extract city, state from address (e.g., '4124 170th PL SE, Bothell, WA' -> 'Bothell, WA')."""
        parts = [p.strip() for p in addr.split(',')]
        if len(parts) >= 2:
            # Return last two parts (typically city, state/country)
            return ', '.join(parts[-2:])
        return None

    def extract_city_from_address(addr):
        """Try to extract city from address (e.g., '4124 170th PL SE, Bothell, WA' -> 'Bothell')."""
        parts = [p.strip() for p in addr.split(',')]
        if len(parts) >= 2:
            # Return second-to-last part (typically city)
            return parts[-2]
        return None

    def try_geocode_with_query(query_str):
        """Try geocoding with a given query string.
        Order: Open-Meteo → Nominatim → Photon (Komoot).
        """
        # Open-Meteo geocoding only understands plain city names, not "City, State" strings.
        # Use just the first comma-delimited segment for the Open-Meteo lookup.
        om_city = query_str.split(',')[0].strip() if ',' in query_str else query_str

        # Primary: Open-Meteo geocoding API — fast (~1s), no API key, no rate-limit concerns.
        # Retry once (with brief back-off) because Azure egress can be flaky on first attempt.
        for _attempt in range(2):
            try:
                g_url = 'https://geocoding-api.open-meteo.com/v1/search'
                g_params = {'name': om_city, 'count': 1}
                g_resp = requests.get(g_url, params=g_params, timeout=8)
                g_resp.raise_for_status()
                g_data = g_resp.json()
                results = g_data.get('results') or []
                if results:
                    item = results[0]
                    lat = float(item['latitude'])
                    lon = float(item['longitude'])
                    # Build a readable display name
                    display_parts = [item.get('name', '')]
                    if item.get('admin1'):
                        display_parts.append(item['admin1'])
                    if item.get('country'):
                        display_parts.append(item['country'])
                    display = ', '.join(p for p in display_parts if p)
                    # Open-Meteo geocoding response already includes elevation
                    elev = item.get('elevation')
                    if elev is None:
                        try:
                            elev = get_elevation(lat, lon)
                        except Exception:
                            elev = None
                    return lat, lon, display, elev
                break  # empty results — no point retrying
            except Exception as _e:
                if _attempt == 0:
                    logger.debug('Open-Meteo geocoding attempt 1 failed for "%s": %s; retrying…', om_city, _e)
                    time.sleep(0.5)
                else:
                    logger.debug('Open-Meteo geocoding failed after 2 attempts for "%s"', om_city)

        # Fallback 1: Nominatim (blocked with 403 from many cloud IPs, but try anyway)
        try:
            url = 'https://nominatim.openstreetmap.org/search'
            params = {'q': query_str, 'format': 'json', 'limit': 1}
            user_agent = os.environ.get('NOMINATIM_USER_AGENT', 'weather-app/1.0 (contact: example@example.com)')
            headers = {'User-Agent': user_agent}
            resp = requests.get(url, params=params, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data:
                item = data[0]
                lat = float(item['lat'])
                lon = float(item['lon'])
                display = item.get('display_name')
                try:
                    elev = get_elevation(lat, lon)
                except Exception:
                    elev = None
                return lat, lon, display, elev
        except requests.HTTPError as e:
            if e.response.status_code == 403:
                logger.warning('Nominatim blocked (403) for "%s"', query_str)
            else:
                logger.warning('Nominatim geocoding failed for "%s": %s', query_str, e)
        except Exception:
            logger.debug('Nominatim geocoding failed for "%s"', query_str)

        # Fallback 2: Photon by Komoot — free, no API key, OpenStreetMap-backed
        try:
            p_resp = requests.get(
                'https://photon.komoot.io/api/',
                params={'q': query_str, 'limit': 1},
                timeout=6,
            )
            p_resp.raise_for_status()
            features = p_resp.json().get('features', [])
            if features:
                coords = features[0]['geometry']['coordinates']  # [lon, lat]
                p_lon = float(coords[0])
                p_lat = float(coords[1])
                props = features[0].get('properties', {})
                display_parts = [props.get('name', ''), props.get('state', ''), props.get('country', '')]
                display = ', '.join(p for p in display_parts if p)
                try:
                    elev = get_elevation(p_lat, p_lon)
                except Exception:
                    elev = None
                return p_lat, p_lon, display, elev
        except Exception:
            logger.debug('Photon geocoding failed for "%s"', query_str)

        return None

    # Expand US state abbreviations: "Snohomish, WA" → "Snohomish, Washington"
    # Handles "City, ST" and "City, ST ZIPCODE" so both Nominatim and Open-Meteo succeed.
    _loc_parts = [p.strip() for p in location.split(',')]
    if len(_loc_parts) >= 2:
        _last_words = _loc_parts[-1].strip().split()
        _abbrev_to_state = {v: k for k, v in STATE_ABBREVS.items()}
        if _last_words and _last_words[0].upper() in _abbrev_to_state:
            _state_full = _abbrev_to_state[_last_words[0].upper()]
            _remaining  = ' '.join(_last_words[1:])
            _loc_parts[-1] = (f'{_state_full} {_remaining}').strip() if _remaining else _state_full
            _expanded = ', '.join(_loc_parts)
            if _expanded != location:
                logger.info('Expanded state abbreviation: "%s" → "%s"', location, _expanded)
                location = _expanded

    # Try the original location (possibly with expanded state name)
    result = try_geocode_with_query(location)
    if result:
        logger.info('Geocoded with original query: "%s"', location)
        return result
    
    logger.info('Original query failed, attempting fallback strategies for "%s"', location)

    # If it failed and looks like an address, try progressively simpler queries
    if ',' in location:
        city_state = extract_city_state_from_address(location)
        if city_state and city_state != location:
            logger.info('Trying city+state extraction: "%s"', city_state)
            result = try_geocode_with_query(city_state)
            if result:
                logger.info('Success with city+state: "%s"', city_state)
                return result
        
        # Try just the city name
        city_only = extract_city_from_address(location)
        if city_only and city_only != location and city_only != city_state:
            logger.info('Trying city-only extraction: "%s"', city_only)
            result = try_geocode_with_query(city_only)
            if result:
                logger.info('Success with city-only: "%s"', city_only)
                return result

    # Check if it's a known landmark and try the corresponding city
    location_lower = location.lower().strip()
    for landmark, city in landmark_map.items():
        if landmark in location_lower:
            logger.info('Recognized landmark "%s", trying city "%s" instead', location, city)
            result = try_geocode_with_query(city)
            if result:
                logger.info('Success with landmark mapping: "%s" -> "%s"', location, city)
                return result

    logger.warning('All geocoding strategies failed for "%s"', location)
    return None


def get_weather(lat: float, lon: float):
    """Fetch current weather from Open-Meteo. Cached for 10 minutes per location."""
    import time as _t
    key = (round(lat, 2), round(lon, 2))
    now = _t.time()
    if key in _weather_cache:
        ts, cached = _weather_cache[key]
        if now - ts < _WEATHER_CACHE_TTL_S:
            return cached

    url = 'https://api.open-meteo.com/v1/forecast'
    params = {
        'latitude': lat, 'longitude': lon,
        'current': 'temperature_2m,weathercode,windspeed_10m,winddirection_10m,precipitation,relative_humidity_2m',
        'timezone': 'auto'
    }
    resp = requests.get(url, params=params, timeout=6)
    resp.raise_for_status()
    data = resp.json()
    cur = data.get('current', {})
    if not cur:
        return None
    result = {
        'temperature':   cur.get('temperature_2m'),
        'windspeed':     cur.get('windspeed_10m'),
        'winddirection': cur.get('winddirection_10m'),
        'weathercode':   cur.get('weathercode'),
        'precipitation': cur.get('precipitation'),
        'humidity':      cur.get('relative_humidity_2m'),
    }
    _weather_cache[key] = (now, result)
    return result


def get_historical_weather(lat: float, lon: float, start_date: str, end_date: str):
    """Fetch historical weather from Open-Meteo Archive API. Cached 6h per window."""
    import time as _t
    key = (round(lat, 2), round(lon, 2), start_date, end_date)
    now = _t.time()
    if key in _hist_cache:
        ts, cached = _hist_cache[key]
        if now - ts < _HIST_CACHE_TTL_S:
            return cached

    url = 'https://archive-api.open-meteo.com/v1/archive'
    params = {
        'latitude': lat,
        'longitude': lon,
        'start_date': start_date,
        'end_date': end_date,
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max',
        'temperature_unit': 'celsius',
        'timezone': 'UTC'
    }
    resp = requests.get(url, params=params, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    result = {
        'location': {'latitude': lat, 'longitude': lon},
        'daily': data.get('daily', {}),
        'timezone': data.get('timezone')
    }
    _hist_cache[key] = (now, result)
    return result


# US State abbreviation lookup (used for FEMA API queries)
STATE_ABBREVS = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR',
    'California': 'CA', 'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE',
    'Florida': 'FL', 'Georgia': 'GA', 'Hawaii': 'HI', 'Idaho': 'ID',
    'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS',
    'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS',
    'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV',
    'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY',
    'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK',
    'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT',
    'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV',
    'Wisconsin': 'WI', 'Wyoming': 'WY', 'District of Columbia': 'DC',
    'Puerto Rico': 'PR', 'Virgin Islands': 'VI', 'Guam': 'GU',
}


# In-memory FEMA cache keyed by state abbreviation — 24 h TTL
_fema_cache: dict = {}  # {state_abbrev: (fetched_at_datetime, result_dict)}
_FEMA_CACHE_TTL_S = 86400  # 24 hours

# In-memory weather cache — 10 min TTL, keyed by rounded (lat, lon)
_weather_cache: dict = {}  # {(lat2, lon2): (epoch_ts, result)}
_WEATHER_CACHE_TTL_S = 600  # 10 minutes

# In-memory historical weather cache — 6 h TTL, keyed by (lat2, lon2, start, end)
_hist_cache: dict = {}  # {(lat2, lon2, start, end): (epoch_ts, result)}
_HIST_CACHE_TTL_S = 21600  # 6 hours

# In-memory USGS cache — 5 min TTL, keyed by rounded (lat, lon)
_usgs_cache: dict = {}  # {(lat2, lon2): (epoch_ts, result)}
_USGS_CACHE_TTL_S = 300  # 5 minutes

# Water-proximity cache — 24 h TTL, keyed by rounded (lat, lon)
_water_cache: dict = {}  # {(lat2, lon2): (epoch_ts, result)}
_WATER_CACHE_TTL_S = 86400  # 24 hours

# Static per-state flood risk scores derived from FEMA public records (2019-2024).
# Used as an instant fallback when fema.gov is unreachable (Akamai blocks cloud IPs).
_FEMA_STATE_STATIC: dict = {
    # Very high flood history
    'TX': 35, 'LA': 35, 'KY': 35, 'WV': 35, 'MO': 35,
    # High
    'TN': 28, 'MS': 28, 'FL': 28, 'AL': 28, 'AR': 28,
    'IL': 28, 'IN': 21, 'OH': 21, 'PA': 21, 'NC': 21,
    'SC': 21, 'GA': 21, 'VA': 21, 'OK': 21, 'KS': 21,
    # Moderate
    'IA': 14, 'NE': 14, 'SD': 14, 'ND': 14, 'MN': 14,
    'WI': 14, 'MI': 14, 'NY': 14, 'NJ': 14, 'MD': 14,
    'WA': 14, 'OR': 14, 'CA': 14, 'MT': 7,  'ID': 7,
    # Lower
    'DE': 7,  'CT': 7,  'MA': 7,  'RI': 7,  'NH': 7,
    'VT': 7,  'ME': 7,  'CO': 7,  'WY': 7,  'AK': 7,
    'HI': 7,  'DC': 7,  'PR': 14, 'VI': 14,
    # Arid / lower flood frequency
    'NV': 7,  'AZ': 7,  'UT': 7,  'NM': 7,
}


def get_fema_flood_history(lat: float, lon: float):
    """Query OpenFEMA API for historical flood disaster declarations near a location.
    Falls back to a static per-state score table when fema.gov is unreachable
    (Azure datacenter IPs are blocked by Akamai CDN on www.fema.gov).
    Results are cached per-state for 24 hours to minimise round-trips.
    """
    state_name   = ''
    state_abbrev = ''
    try:
        # Reverse geocode using BigDataCloud (free, no key, fast, no Azure blocks)
        geo_url = 'https://api.bigdatacloud.net/data/reverse-geocode-client'
        params = {'latitude': lat, 'longitude': lon, 'localityLanguage': 'en'}
        resp = requests.get(geo_url, params=params, timeout=5)
        resp.raise_for_status()
        geo_data     = resp.json()
        country_code = geo_data.get('countryCode', '')
        state_name   = geo_data.get('principalSubdivision', '')
        state_abbrev = STATE_ABBREVS.get(state_name, '')

        if country_code != 'US' or not state_abbrev:
            return {
                'available': False,
                'note': 'FEMA historical data only available for US locations.',
                'events': [],
                'historical_risk_score': 0
            }

        # --- Check in-memory cache first ---
        now = datetime.utcnow()
        if state_abbrev in _fema_cache:
            cached_at, cached_result = _fema_cache[state_abbrev]
            if (now - cached_at).total_seconds() < _FEMA_CACHE_TTL_S:
                logger.debug('FEMA cache hit for state %s', state_abbrev)
                return cached_result

        # --- Fetch from OpenFEMA using a full browser session to bypass Akamai CDN ---
        fema_url = 'https://www.fema.gov/api/open/v2/disasterDeclarationsSummaries'
        fema_params = {
            '$filter': f"state eq '{state_abbrev}' and incidentType eq 'Flood'",
            '$orderby': 'declarationDate desc',
            '$top': 10,
            '$select': 'disasterNumber,declarationDate,declarationTitle,incidentType,state,designatedArea,incidentBeginDate,incidentEndDate'
        }
        _session = requests.Session()
        _session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Referer': 'https://www.fema.gov/disaster/declarations',
            'Origin': 'https://www.fema.gov',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'sec-ch-ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        })
        fema_resp = _session.get(fema_url, params=fema_params, timeout=6)
        fema_resp.raise_for_status()
        events = fema_resp.json().get('DisasterDeclarationsSummaries', [])

        # Count events in last 5 years for scoring
        cutoff = (datetime.utcnow() - timedelta(days=5 * 365)).strftime('%Y-%m-%d')
        recent = [e for e in events if (e.get('declarationDate') or '') >= cutoff]
        hist_score = min(35, len(recent) * 7)

        result = {
            'available': True,
            'state': state_name,
            'state_abbrev': state_abbrev,
            'total_flood_declarations': len(events),
            'recent_5yr_count': len(recent),
            'historical_risk_score': hist_score,
            'events': events[:5]
        }
        # Store in cache
        _fema_cache[state_abbrev] = (now, result)
        return result
    except Exception as e:
        logger.warning('FEMA API unreachable (%s) — using static state-level fallback', e)
        # fema.gov is blocked from Azure datacenter IPs (Akamai CDN).
        # state_name/state_abbrev are already extracted above — no extra network call needed.
        if state_abbrev:
            return {
                'available': True,
                'state': state_name,
                'state_abbrev': state_abbrev,
                'historical_risk_score': _FEMA_STATE_STATIC.get(state_abbrev, 7),
                'events': [],
                'recent_5yr_count': None,
                'total_flood_declarations': None,
                'note': 'Live FEMA data unavailable — showing historical flood frequency estimate for this state.'
            }
        return {'available': False, 'events': [], 'historical_risk_score': 0,
                'note': 'FEMA flood history data unavailable for this location.'}


def get_usgs_stream_gauge(lat: float, lon: float):
    """Query USGS Water Services for nearby real-time stream gauge data.
    Completely free, no API key required. US only.
    Cached 5 minutes per location.
    """
    import time as _t
    _key = (round(lat, 2), round(lon, 2))
    _now = _t.time()
    if _key in _usgs_cache:
        _ts, _cached = _usgs_cache[_key]
        if _now - _ts < _USGS_CACHE_TTL_S:
            return _cached
    try:
        # Search within ~0.5 degree bounding box
        bbox = f'{lon - 0.5},{lat - 0.5},{lon + 0.5},{lat + 0.5}'
        url = 'https://waterservices.usgs.gov/nwis/iv/'
        params = {
            'format': 'json',
            'bBox': bbox,
            'parameterCd': '00065',  # Gage height in feet
            'siteStatus': 'active',
        }
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        ts = resp.json().get('value', {}).get('timeSeries', [])

        if not ts:
            return {
                'available': False,
                'note': 'No USGS stream gauges found nearby (may be outside USA or no active gauges).',
                'gauges': [],
                'gauge_risk_score': 0
            }

        gauges = []
        for site in ts[:3]:
            info = site.get('sourceInfo', {})
            site_code = (info.get('siteCode') or [{}])[0].get('value', '')
            values = (site.get('values') or [{}])[0].get('value', [])
            latest = values[-1] if values else {}
            gauges.append({
                'name': info.get('siteName', 'Unknown Station'),
                'site_code': site_code,
                'gage_height_ft': latest.get('value'),
                'reading_time': latest.get('dateTime', ''),
                'chart_url': f'https://waterdata.usgs.gov/monitoring-location/{site_code}/#parameterCode=00065&period=P7D'
            })

        # Score based on first gauge height
        gauge_score = 0
        try:
            h = float(gauges[0]['gage_height_ft'] or 0)
            if h > 15:
                gauge_score = 35
            elif h > 8:
                gauge_score = 20
            elif h > 3:
                gauge_score = 10
        except Exception:
            pass

        _result = {
            'available': True,
            'gauges': gauges,
            'gauge_risk_score': gauge_score
        }
        _usgs_cache[_key] = (_now, _result)
        return _result
    except Exception as e:
        logger.debug('USGS stream gauge lookup failed: %s', e)
        return {'available': False, 'gauges': [], 'gauge_risk_score': 0, 'note': str(e)}


def get_flood_risk(lat: float, lon: float):
    """Fetch flood risk data. Currently returns a placeholder with flood risk zone.
    A real implementation would call the Google Flood Hub API or local flood APIs.
    """
    # For now, return a simple flood risk assessment based on latitude/longitude
    # In production, you'd call Google Flood Hub API or a local service
    try:
        # Simple heuristic: areas near coastlines or rivers have higher flood risk
        # This is just a placeholder; real flood data requires dedicated APIs
        risk_level = 'low'
        risk_score = 15  # 0-50 scale (base only)
        
        # Very simple mock: if near certain latitudes/longitudes, increase risk
        if (lat > 40 and lat < 52) and (lon > -10 and lon < 40):  # EU flood-prone areas
            risk_level = 'moderate'
            risk_score = 30
        if (lat > 25 and lat < 35) and (lon > 70 and lon < 90):  # South Asia monsoon zone
            risk_level = 'high'
            risk_score = 50
        
        return {
            'latitude': lat,
            'longitude': lon,
            'risk_level': risk_level,
            'risk_score': risk_score,
            'note': 'This is a simplified flood risk estimate. For accurate flood forecasts, consult local authorities or Google Flood Hub.'
        }
    except Exception as e:
        logger.exception('Error calculating flood risk')
        return {'error': 'flood risk calculation failed', 'detail': str(e)}


def get_water_proximity_score(lat: float, lon: float):
    """Query OSM Overpass API for rivers/water bodies near the given location.
    Returns (score 0-20, feature_name_or_None, distance_m_or_None).
    Scoring: <300 m → 20 pts, <800 m → 15, <1500 m → 8, <2500 m → 4, else → 0.
    Results are cached 24 h per rounded (lat, lon) to avoid repeated API hits.
    """
    import time as _t
    _key = (round(lat, 2), round(lon, 2))
    _now = _t.time()
    if _key in _water_cache:
        _ts, _cached = _water_cache[_key]
        if _now - _ts < _WATER_CACHE_TTL_S:
            return _cached

    _result = (0, None, None)
    try:
        _overpass = 'https://overpass-api.de/api/interpreter'
        _query = (
            '[out:json][timeout:5];'
            '('
            f'way["waterway"~"^(river|stream|canal|drain)$"](around:2500,{lat},{lon});'
            f'way["natural"="water"](around:2500,{lat},{lon});'
            f'relation["natural"="water"](around:2500,{lat},{lon});'
            ');'
            'out center 10;'
        )
        _resp = requests.post(_overpass, data={'data': _query}, timeout=6)
        _resp.raise_for_status()
        _elements = _resp.json().get('elements', [])
        if _elements:
            _min_dist = float('inf')
            _closest_name = None
            for _el in _elements:
                _c = _el.get('center', {})
                _clat = _c.get('lat') or _el.get('lat')
                _clon = _c.get('lon') or _el.get('lon')
                if _clat and _clon:
                    _dist_m = ((lat - _clat) ** 2 + (lon - _clon) ** 2) ** 0.5 * 111000
                    if _dist_m < _min_dist:
                        _min_dist = _dist_m
                        _closest_name = (
                            _el.get('tags', {}).get('name')
                            or _el.get('tags', {}).get('waterway')
                            or 'water body'
                        )
            if _min_dist < 300:    _score = 20
            elif _min_dist < 800:  _score = 15
            elif _min_dist < 1500: _score = 8
            elif _min_dist < 2500: _score = 4
            else:                  _score = 0
            _result = (_score, _closest_name, round(_min_dist))
    except Exception as _e:
        logger.debug('Water proximity OSM lookup failed: %s', _e)

    _water_cache[_key] = (_now, _result)
    return _result


def calculate_flood_risk_for_date(lat: float, lon: float, date_str: str, daily_data: dict,
                                   elevation_m=None, fema_data=None, usgs_data=None,
                                   water_data=None):
    """Calculate flood risk for a specific date.
    Factors (max pts):
      Precipitation today  : 30 pts  (dynamic — primary driver)
      7-day cumulative     : 25 pts  (dynamic)
      Water proximity (OSM): 20 pts  (real waterway lookup, graduated by distance)
      Terrain / elevation  : 20 pts  (static)
      FEMA historical      : 20 pts  (capped)
      USGS live gauge      : 20 pts  (capped)
      Total uncapped: 135  → capped at 100
    """
    try:
        times = daily_data.get('time', [])
        precip = daily_data.get('precipitation_sum', [])

        if date_str not in times:
            return None

        date_idx = times.index(date_str)
        precip_today = float(precip[date_idx]) if date_idx < len(precip) and precip[date_idx] is not None else 0.0

        # 7-day cumulative precipitation
        window_start = max(0, date_idx - 6)
        cumulative_precip = sum(float(v) for v in precip[window_start:date_idx + 1] if v is not None)

        # --- Terrain / elevation risk (max 20) ---
        terrain_risk = 0
        try:
            if elevation_m is not None:
                e = float(elevation_m)
                if e < 5:     terrain_risk = 20
                elif e < 15:  terrain_risk = 14
                elif e < 30:  terrain_risk = 8
                elif e < 60:  terrain_risk = 4
        except Exception:
            pass

        # --- Water proximity via OSM Overpass (max 20, graduated by distance) ---
        # water_data is pre-fetched by the endpoint; falls back to live lookup if absent
        if water_data is not None:
            water_score, water_name, water_dist_m = water_data
        else:
            water_score, water_name, water_dist_m = get_water_proximity_score(lat, lon)

        # --- Precipitation today (max 30) ---
        if precip_today > 50:    precip_risk = 30
        elif precip_today > 20:  precip_risk = 20
        elif precip_today > 10:  precip_risk = 12
        elif precip_today > 5:   precip_risk = 7
        elif precip_today > 1:   precip_risk = 3
        else:                    precip_risk = 0

        # --- 7-day cumulative (max 25) ---
        if cumulative_precip > 150:   cum_risk = 25
        elif cumulative_precip > 80:  cum_risk = 18
        elif cumulative_precip > 40:  cum_risk = 10
        elif cumulative_precip > 15:  cum_risk = 4
        else:                         cum_risk = ∂

        # --- FEMA capped at 20 (was 35) ---
        fema_risk = min(20, (fema_data or {}).get('historical_risk_score', 0))

        # --- USGS capped at 20 (was 35) ---
        usgs_risk = min(20, (usgs_data or {}).get('gauge_risk_score', 0))

        # Final score capped at 100
        final_score = min(100, precip_risk + cum_risk + water_score + terrain_risk + fema_risk + usgs_risk)

        if final_score < 30:
            risk_level = 'low'
        elif final_score < 60:
            risk_level = 'moderate'
        else:
            risk_level = 'high'

        return {
            'date': date_str,
            'risk_level': risk_level,
            'risk_score': round(final_score, 1),
            'precipitation_mm': round(precip_today, 1),
            'cumulative_7day_precip_mm': round(cumulative_precip, 1),
            'near_water_body': water_score > 0,
            'water_body_name': water_name,
            'water_distance_m': water_dist_m,
            'elevation_m': elevation_m,
            'factors': {
                'precipitation_today_risk': precip_risk,
                'cumulative_week_risk':     cum_risk,
                'water_proximity_risk':     water_score,
                'terrain_elevation_risk':   terrain_risk,
                'fema_historical_risk':     fema_risk,
                'usgs_gauge_risk':          usgs_risk,
            },
            'note': 'Flood risk from precipitation, terrain, water proximity (OSM), FEMA history, and USGS gauge.'
        }
    except Exception:
        logger.exception('Error calculating flood risk for date')
        return None


# ---------------------------------------------------------------------------
# Evacuation Route Engine
# ---------------------------------------------------------------------------

def bearing_to_compass(degrees: float) -> str:
    """Convert a bearing (0–360°) to a human-readable compass direction."""
    dirs = ['North', 'Northeast', 'East', 'Southeast', 'South', 'Southwest', 'West', 'Northwest']
    return dirs[round(float(degrees) / 45) % 8]


def get_evacuation_routes(lat: float, lon: float, elevation_m: float = None):
    """Compute up to 3 road-based evacuation routes away from a flood-risk location.

    Algorithm
    ---------
    1. Scatter 24 candidate destinations (8 compass directions × 3 distances).
    2. Fetch their elevations in one Open-Meteo batch call.
    3. Score each candidate: reward elevation gain, penalise awkward distances.
    4. Choose the top 3 candidates that are ≥60° apart (spread coverage).
    5. Route each via OSRM (free, no API key) for real road geometry + steps.

    Returns a list of up to 3 route dicts.
    """
    import math

    origin_elev = float(elevation_m) if elevation_m is not None else 0.0
    R = 6371.0  # Earth radius km

    # 1. Generate 24 candidate destinations (8 directions × 3 distances)
    distances_km = [8, 14, 22]
    bearings_deg = list(range(0, 360, 45))
    candidates = []
    for dist in distances_km:
        for bearing in bearings_deg:
            b_r = math.radians(bearing)
            lat_r = math.radians(lat)
            lon_r = math.radians(lon)
            d_r = dist / R
            dest_lat_r = math.asin(
                math.sin(lat_r) * math.cos(d_r) +
                math.cos(lat_r) * math.sin(d_r) * math.cos(b_r)
            )
            dest_lon_r = lon_r + math.atan2(
                math.sin(b_r) * math.sin(d_r) * math.cos(lat_r),
                math.cos(d_r) - math.sin(lat_r) * math.sin(dest_lat_r)
            )
            candidates.append({
                'lat': math.degrees(dest_lat_r),
                'lon': math.degrees(dest_lon_r),
                'dist_km': dist,
                'bearing': bearing,
                'elevation_m': None,
                'score': 0.0,
            })

    # 2. Batch elevation lookup — single Open-Meteo request for all 24 points
    lats_str = ','.join(f'{c["lat"]:.5f}' for c in candidates)
    lons_str = ','.join(f'{c["lon"]:.5f}' for c in candidates)
    try:
        resp = requests.get(
            'https://api.open-meteo.com/v1/elevation',
            params={'latitude': lats_str, 'longitude': lons_str},
            timeout=8,
        )
        resp.raise_for_status()
        elevs = resp.json().get('elevation') or []
        for i, e in enumerate(elevs):
            if i < len(candidates) and e is not None:
                candidates[i]['elevation_m'] = float(e)
    except Exception as exc:
        logger.warning('Batch elevation lookup for evacuation failed: %s', exc)

    # 3. Score: strongly reward elevation gain; sweet-spot distance ~14 km
    for c in candidates:
        gain = (c['elevation_m'] or origin_elev) - origin_elev
        dist_penalty = abs(c['dist_km'] - 14) * 0.4
        c['score'] = gain * 3.0 - dist_penalty

    # 4. Pick top 3 with ≥60° angular separation (spread-out coverage)
    candidates.sort(key=lambda c: c['score'], reverse=True)
    selected: list = []
    for cand in candidates:
        if not selected:
            selected.append(cand)
        elif len(selected) < 3:
            min_sep = min(
                min(abs(cand['bearing'] - s['bearing']),
                    360 - abs(cand['bearing'] - s['bearing']))
                for s in selected
            )
            if min_sep >= 60:
                selected.append(cand)
        if len(selected) >= 3:
            break

    # Fallback: pad with any remaining unique bearings
    if len(selected) < 3:
        seen = {s['bearing'] for s in selected}
        for cand in candidates:
            if len(selected) >= 3:
                break
            if cand['bearing'] not in seen:
                selected.append(cand)
                seen.add(cand['bearing'])

    # 5. Fetch OSRM road routes for selected destinations
    COLORS = ['#22c55e', '#3b82f6', '#f59e0b']   # green, blue, amber
    routes = []
    for idx, dest in enumerate(selected):
        try:
            osrm_url = (
                f'https://router.project-osrm.org/route/v1/driving/'
                f'{lon:.5f},{lat:.5f};{dest["lon"]:.5f},{dest["lat"]:.5f}'
                f'?overview=full&geometries=geojson&steps=true&annotations=false'
            )
            r = requests.get(osrm_url, timeout=12)
            r.raise_for_status()
            osrm = r.json()
            if osrm.get('code') != 'Ok' or not osrm.get('routes'):
                continue

            rd = osrm['routes'][0]

            # Parse turn-by-turn steps
            steps = []
            for leg in rd.get('legs', []):
                for step in leg.get('steps', []):
                    dist_m = step.get('distance', 0)
                    if dist_m < 50:
                        continue
                    mv    = step.get('maneuver', {})
                    mtype = mv.get('type', 'continue')
                    mmod  = mv.get('modifier', '')
                    road  = step.get('name') or 'unnamed road'
                    if mtype == 'depart':
                        ab = mv.get('bearing_after', dest['bearing'])
                        instruction = f'Head {bearing_to_compass(ab)} on {road}'
                    elif mtype == 'arrive':
                        instruction = 'Arrive at safe destination'
                    elif mmod:
                        instruction = f'{mtype.replace("-", " ").title()} {mmod} onto {road}'
                    else:
                        instruction = f'{mtype.replace("-", " ").title()} on {road}'
                    steps.append({'instruction': instruction, 'distance_m': round(dist_m)})

            elev_gain = round((dest.get('elevation_m') or origin_elev) - origin_elev)
            routes.append({
                'label':             chr(65 + idx),   # 'A', 'B', 'C'
                'color':             COLORS[idx],
                'direction':         bearing_to_compass(dest['bearing']),
                'destination': {
                    'lat':              round(dest['lat'],  5),
                    'lon':              round(dest['lon'],  5),
                    'elevation_m':      dest.get('elevation_m'),
                    'elevation_gain_m': elev_gain,
                },
                'road_distance_km':  round(rd['distance'] / 1000, 1),
                'duration_min':      round(rd['duration'] / 60),
                'geometry':          rd['geometry'],   # GeoJSON LineString
                'steps':             steps[:20],
            })
        except Exception as exc:
            logger.warning('OSRM routing failed for evacuation dest %s: %s', dest, exc)

    return routes


@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/api/weather')
def api_weather():
    location = request.args.get('location') or request.args.get('q')
    if not location:
        return jsonify({'error': 'missing "location" parameter'}), 400

    try:
        geo = geocode(location)
    except requests.HTTPError as e:
        logger.warning('Geocoding HTTP error: %s', e)
        return jsonify({'error': 'geocoding service error', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Unexpected geocoding error')
        return jsonify({'error': 'geocoding failed', 'detail': str(e)}), 500

    if not geo:
        return jsonify({
            'error': 'location not found',
            'hint': 'Try using a city or country name (e.g., "Paris", "London", "Tokyo") instead of landmarks.'
        }), 404
    lat, lon, name, elevation = geo

    try:
        weather = get_weather(lat, lon)
    except requests.HTTPError as e:
        logger.warning('Weather HTTP error: %s', e)
        return jsonify({'error': 'weather service error', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Unexpected weather fetch error')
        return jsonify({'error': 'weather fetch failed', 'detail': str(e)}), 500

    if not weather:
        return jsonify({'error': 'weather data unavailable'}), 502

    result = {
        'location': name,
        'latitude': lat,
        'longitude': lon,
        'elevation_m': elevation,
        'temperature_c': weather.get('temperature'),
        'windspeed': weather.get('windspeed'),
        'winddirection': weather.get('winddirection'),
        'weathercode': weather.get('weathercode'),
        'time': weather.get('time'),
    }
    return jsonify(result)


@app.route('/api/weather/current')
def api_weather_current():
    """Get current weather for a location."""
    location = request.args.get('location') or request.args.get('q')
    if not location:
        return jsonify({'error': 'missing "location" parameter'}), 400

    try:
        geo = geocode(location)
    except requests.HTTPError as e:
        logger.warning('Geocoding HTTP error: %s', e)
        return jsonify({'error': 'geocoding service error', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Unexpected geocoding error')
        return jsonify({'error': 'geocoding failed', 'detail': str(e)}), 500

    if not geo:
        return jsonify({
            'error': 'location not found',
            'hint': 'Try using a city name, country, or address.'
        }), 404
    lat, lon, name, elevation = geo

    try:
        weather = get_weather(lat, lon)
    except requests.HTTPError as e:
        logger.warning('Weather HTTP error: %s', e)
        return jsonify({'error': 'weather service error', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Unexpected weather fetch error')
        return jsonify({'error': 'weather fetch failed', 'detail': str(e)}), 500

    if not weather:
        return jsonify({'error': 'weather data unavailable'}), 502

    result = {
        'location': name,
        'latitude': lat,
        'longitude': lon,
        'elevation_m': elevation,
        'current_weather': weather,
    }
    return jsonify(result)


@app.route('/api/weather/historical')
def api_weather_historical():
    """Get historical weather data for a location and date range.
    Parameters:
      - location: place name or address
      - start_date: YYYY-MM-DD (default: 30 days ago)
      - end_date: YYYY-MM-DD (default: today)
    """
    from datetime import datetime, timedelta
    
    location = request.args.get('location') or request.args.get('q')
    if not location:
        return jsonify({'error': 'missing "location" parameter'}), 400

    # Default date range: last 30 days
    today = datetime.utcnow().date()
    end_date = request.args.get('end_date', str(today))
    start_date = request.args.get('start_date', str(today - timedelta(days=30)))

    try:
        geo = geocode(location)
    except Exception as e:
        logger.exception('Geocoding error')
        return jsonify({'error': 'geocoding failed', 'detail': str(e)}), 500

    if not geo:
        return jsonify({
            'error': 'location not found',
            'hint': 'Try using a city name, country, or address.'
        }), 404
    lat, lon, name, elevation = geo

    try:
        hist_data = get_historical_weather(lat, lon, start_date, end_date)
        hist_data['location_name'] = name
        hist_data['elevation_m'] = elevation
        return jsonify(hist_data)
    except requests.HTTPError as e:
        logger.warning('Historical weather HTTP error: %s', e)
        return jsonify({'error': 'historical weather service error', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Unexpected historical weather error')
        return jsonify({'error': 'historical weather fetch failed', 'detail': str(e)}), 500


@app.route('/api/flood-risk')
def api_flood_risk():
    """Get flood risk data for a location.
    Parameters:
      - location: place name or address
    """
    location = request.args.get('location') or request.args.get('q')
    if not location:
        return jsonify({'error': 'missing "location" parameter'}), 400

    try:
        geo = geocode(location)
    except Exception as e:
        logger.exception('Geocoding error')
        return jsonify({'error': 'geocoding failed', 'detail': str(e)}), 500

    if not geo:
        return jsonify({
            'error': 'location not found',
            'hint': 'Try using a city name, country, or address.'
        }), 404
    lat, lon, name, elevation = geo

    try:
        flood_data = get_flood_risk(lat, lon)
        flood_data['location_name'] = name
        flood_data['elevation_m'] = elevation
        return jsonify(flood_data)
    except Exception as e:
        logger.exception('Flood risk error')
        return jsonify({'error': 'flood risk calculation failed', 'detail': str(e)}), 500


@app.route('/api/all')
def api_all():
    """Get all available data (current, historical, flood) for a location.
    Parameters:
      - location: place name or address
      - start_date: YYYY-MM-DD for historical (default: 30 days ago)
      - end_date: YYYY-MM-DD for historical (default: today)
    """
    location = request.args.get('location') or request.args.get('q')
    if not location:
        return jsonify({'error': 'missing "location" parameter'}), 400

    try:
        geo = geocode(location)
    except Exception as e:
        logger.exception('Geocoding error')
        return jsonify({'error': 'geocoding failed', 'detail': str(e)}), 500

    if not geo:
        return jsonify({
            'error': 'location not found',
            'hint': 'Try using a city name, country, or address.'
        }), 404
    
    lat, lon, name, elevation = geo
    result = {'location': name, 'latitude': lat, 'longitude': lon, 'elevation_m': elevation}

    # Fetch current weather
    try:
        result['current_weather'] = get_weather(lat, lon)
    except Exception as e:
        logger.warning('Current weather error: %s', e)
        result['current_weather_error'] = str(e)

    # Fetch historical weather
    try:
        from datetime import datetime, timedelta
        today = datetime.utcnow().date()
        end_date = request.args.get('end_date', str(today))
        start_date = request.args.get('start_date', str(today - timedelta(days=30)))
        result['historical_weather'] = get_historical_weather(lat, lon, start_date, end_date)
    except Exception as e:
        logger.warning('Historical weather error: %s', e)
        result['historical_weather_error'] = str(e)

    # Fetch flood risk
    try:
        flood = get_flood_risk(lat, lon)
        flood['elevation_m'] = elevation
        result['flood_risk'] = flood
    except Exception as e:
        logger.warning('Flood risk error: %s', e)
        result['flood_risk_error'] = str(e)

    return jsonify(result)


@app.route('/api/flood-risk-date')
def api_flood_risk_date():
    """Get flood risk for a specific date, including weather conditions and water proximity.
    Parameters:
      - location: place name or address
      - date: YYYY-MM-DD format
    
    Returns combined weather data and flood risk analysis for that date.
    """
    location = request.args.get('location') or request.args.get('q')
    date_str = request.args.get('date', '').strip()
    
    if not location:
        return jsonify({'error': 'missing "location" parameter'}), 400
    if not date_str:
        return jsonify({'error': 'missing "date" parameter (format: YYYY-MM-DD)'}), 400
    
    # Validate date format
    try:
        from datetime import datetime, timedelta
        target_date = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'invalid date format (use YYYY-MM-DD)'}), 400
    
    # Geocode location
    try:
        geo = geocode(location)
    except Exception as e:
        logger.exception('Geocoding error for date-based flood risk')
        return jsonify({'error': 'geocoding failed', 'detail': str(e)}), 500
    
    if not geo:
        return jsonify({
            'error': 'location not found',
            'hint': 'Try using a city name, country, or address.'
        }), 404
    
    lat, lon, display_name, elevation = geo
    logger.info('Getting flood risk for %s on %s (elevation: %s m)', display_name, date_str, elevation)
    
    try:
        # Determine the best weather API based on date proximity to today.
        # Open-Meteo archive has a ~5-day delay so recent dates need the forecast API.
        today = datetime.now().date()
        target = target_date.date()
        days_diff = (target - today).days          # negative = past, positive = future

        daily = None

        # For recent past (≤92 days) or near future (≤16 days), try forecast API first
        if -92 <= days_diff <= 16:
            try:
                _past = min(92, max(7, -days_diff + 1)) if days_diff <= 0 else 7
                _fore = min(16, max(1, days_diff + 1))  if days_diff >= 0 else 1
                _furl = 'https://api.open-meteo.com/v1/forecast'
                _fpar = {
                    'latitude': lat, 'longitude': lon,
                    'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max,weathercode',
                    'past_days': _past,
                    'forecast_days': _fore,
                    'temperature_unit': 'celsius',
                    'timezone': 'UTC',
                }
                _fr = requests.get(_furl, params=_fpar, timeout=8)
                _fr.raise_for_status()
                _fd = _fr.json().get('daily', {})
                if date_str in _fd.get('time', []):
                    daily = _fd
            except Exception:
                logger.debug('Forecast API fallback failed for %s', date_str)

        # Fall back to archive API for older dates (or if forecast didn't cover this date)
        if daily is None:
            start_date_obj = target_date - timedelta(days=15)
            end_date_obj   = target_date + timedelta(days=15)
            start_date = start_date_obj.strftime('%Y-%m-%d')
            end_date   = end_date_obj.strftime('%Y-%m-%d')
            historical_data = get_historical_weather(lat, lon, start_date, end_date)
            if historical_data and 'daily' in historical_data:
                _ad = historical_data['daily']
                if date_str in _ad.get('time', []):
                    daily = _ad

        if daily is None or date_str not in daily.get('time', []):
            return jsonify({'error': f'no weather data available for {date_str}'}), 404
        
        date_idx = daily['time'].index(date_str)
        
        # Extract weather for that date
        weather_that_day = {
            'date': date_str,
            'temperature_max_c': daily.get('temperature_2m_max', [None])[date_idx],
            'temperature_min_c': daily.get('temperature_2m_min', [None])[date_idx],
            'precipitation_mm': daily.get('precipitation_sum', [None])[date_idx],
            'windspeed_max_kmh': daily.get('windspeed_10m_max', [None])[date_idx],
        }
        
        # Fetch FEMA historical flood declarations and USGS live gauge in parallel (non-fatal)
        from concurrent.futures import ThreadPoolExecutor as _TPE
        import time as _t2
        _t2_0 = _t2.monotonic()
        with _TPE(max_workers=3) as _p:
            _ff = _p.submit(get_fema_flood_history, lat, lon)
            _fu = _p.submit(get_usgs_stream_gauge, lat, lon)
            _fw = _p.submit(get_water_proximity_score, lat, lon)
            try:
                fema_data = _ff.result(timeout=max(0.1, 5.0 - (_t2.monotonic() - _t2_0)))
            except Exception:
                fema_data = {'events': [], 'historical_risk_score': 0}
            try:
                usgs_data = _fu.result(timeout=max(0.1, 5.0 - (_t2.monotonic() - _t2_0)))
            except Exception:
                usgs_data = {'gauges': [], 'gauge_risk_score': 0}
            try:
                water_data = _fw.result(timeout=max(0.1, 5.0 - (_t2.monotonic() - _t2_0)))
            except Exception:
                water_data = (0, None, None)

        # Calculate flood risk for this date (include elevation + FEMA + USGS + water proximity)
        flood_risk_that_day = calculate_flood_risk_for_date(
            lat, lon, date_str, daily,
            elevation_m=elevation,
            fema_data=fema_data,
            usgs_data=usgs_data,
            water_data=water_data,
        )
        
        if not flood_risk_that_day:
            return jsonify({'error': 'could not calculate flood risk for this date'}), 500
        
        return jsonify({
            'location': display_name,
            'latitude': lat,
            'longitude': lon,
            'elevation_m': elevation,
            'date': date_str,
            'weather': weather_that_day,
            'flood_risk': flood_risk_that_day,
            'fema_history': fema_data,
            'usgs_gauges': usgs_data,
        })
    
    except requests.HTTPError as e:
        logger.warning('HTTP error in date-based flood risk: %s', e)
        return jsonify({'error': 'service error', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Unexpected error in date-based flood risk')
        return jsonify({'error': 'flood risk calculation failed', 'detail': str(e)}), 500

def get_forecast_weather(lat: float, lon: float, days: int = 16):
    """Fetch daily forecast from Open-Meteo for up to 16 days ahead (free, no key).
    Retries up to 3 times to handle transient Azure → Open-Meteo timeouts.
    """
    import time as _time
    url = 'https://api.open-meteo.com/v1/forecast'
    params = {
        'latitude': lat,
        'longitude': lon,
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,windspeed_10m_max,weathercode',
        'forecast_days': min(days, 16),
        'temperature_unit': 'celsius',
        'timezone': 'UTC',
    }
    last_exc = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, timeout=8)
            resp.raise_for_status()
            return resp.json().get('daily', {})
        except Exception as exc:
            last_exc = exc
            logger.warning('Open-Meteo forecast attempt %d failed: %s', attempt + 1, exc)
            if attempt < 1:
                _time.sleep(1)
    raise last_exc


def calculate_flood_risk_forecast(lat: float, lon: float, forecast_daily: dict,
                                   elevation_m=None, fema_data=None, usgs_data=None,
                                   water_data=None):
    """Return a 14-day list of daily flood risk scores from forecast data.
    Uses a small fixed geographic base so precipitation variation drives day-to-day changes.
    """
    times     = forecast_daily.get('time', [])
    precip    = forecast_daily.get('precipitation_sum', [])
    precip_p  = forecast_daily.get('precipitation_probability_max', [])

    # Water proximity — use pre-fetched data if available, else live OSM lookup
    if water_data is not None:
        _wscore, _wname, _ = water_data
    else:
        try:
            _wscore, _wname, _ = get_water_proximity_score(lat, lon)
        except Exception:
            _wscore, _wname = 0, ''

    # --- Fixed geographic base (small, so daily precip drives variation) ---
    elevation_risk = 0
    try:
        if elevation_m is not None:
            e = float(elevation_m)
            if e < 5:     elevation_risk = 18
            elif e < 20:  elevation_risk = 11
            elif e < 50:  elevation_risk = 5
    except Exception:
        pass

    # Scale water score (0-20) to fit geo_base budget (max 12)
    water_risk = round(_wscore * 12 / 20)
    # Cap FEMA/USGS contributions so they don't drown out daily variation
    fema_base  = min(10, (fema_data or {}).get('historical_risk_score', 0))
    usgs_base  = min(8,  (usgs_data or {}).get('gauge_risk_score', 0))
    geo_base   = elevation_risk + water_risk + fema_base + usgs_base  # max ~48

    results = []
    # Skip day 0 (today) — show the 14 days *after* the current date (days 1–14)
    for idx, date_str in enumerate(times[1:15]):
        i = idx + 1  # actual index into the full forecast arrays
        p_today = float(precip[i]) if i < len(precip) and precip[i] is not None else 0.0
        p_prob  = float(precip_p[i]) if i < len(precip_p) and precip_p[i] is not None else 50.0

        # 7-day cumulative (including today's carry-over from day 0)
        window_start = max(0, i - 6)
        cum = sum(float(v) for v in precip[window_start:i + 1] if v is not None)

        # Daily precipitation risk (0-40 pts) — primary driver of variation
        if p_today > 50:    precip_risk = 40
        elif p_today > 25:  precip_risk = 28
        elif p_today > 10:  precip_risk = 18
        elif p_today > 5:   precip_risk = 10
        elif p_today > 1:   precip_risk = 4
        else:               precip_risk = 0

        # Scale by precipitation probability
        precip_risk = round(precip_risk * min(p_prob, 100) / 100)

        # Cumulative rain risk (0-15 pts)
        if cum > 100:   cum_risk = 15
        elif cum > 50:  cum_risk = 10
        elif cum > 20:  cum_risk = 5
        else:           cum_risk = 0

        score = min(100, geo_base + precip_risk + cum_risk)
        level = 'low' if score < 30 else ('moderate' if score < 60 else 'high')
        results.append({
            'date':             date_str,
            'risk_score':       round(score, 1),
            'risk_level':       level,
            'precipitation_mm': round(p_today, 1),
            'precip_prob_pct':  round(p_prob),
            'cum_7day_mm':      round(cum, 1),
        })
    return results


@app.route('/api/forecast-risk')
def api_forecast_risk():
    """14-day flood risk forecast.
    Parameters: location OR lat+lon, elevation (optional)
    """
    lat      = request.args.get('lat',       type=float)
    lon      = request.args.get('lon',       type=float)
    elev     = request.args.get('elevation', type=float)
    location = request.args.get('location', '').strip()

    if lat is None or lon is None:
        if not location:
            return jsonify({'error': 'Provide "location" or "lat"+"lon"'}), 400
        geo = geocode(location)
        if not geo:
            return jsonify({'error': 'location not found'}), 404
        lat, lon, _, elev = geo

    try:
        import time as _t
        from concurrent.futures import ThreadPoolExecutor
        _t0 = _t.monotonic()
        _BUDGET = 20.0  # hard wall-clock budget for all futures combined

        def _get_fc(future, default):
            remaining = max(0.1, _BUDGET - (_t.monotonic() - _t0))
            try:
                return future.result(timeout=remaining)
            except Exception:
                return default

        with ThreadPoolExecutor(max_workers=4) as pool:
            f_fc    = pool.submit(get_forecast_weather, lat, lon, 16)
            f_fema  = pool.submit(get_fema_flood_history, lat, lon)
            f_usgs  = pool.submit(get_usgs_stream_gauge, lat, lon)
            f_water = pool.submit(get_water_proximity_score, lat, lon)
            # Forecast is mandatory — raises if it fails
            forecast_daily = f_fc.result(timeout=max(0.1, _BUDGET - (_t.monotonic() - _t0)))
            fema_data  = _get_fc(f_fema,  {'events': [], 'historical_risk_score': 0})
            usgs_data  = _get_fc(f_usgs,  {'gauges': [], 'gauge_risk_score': 0})
            water_data = _get_fc(f_water, (0, None, None))

        daily_risks = calculate_flood_risk_forecast(lat, lon, forecast_daily,
                                                     elevation_m=elev,
                                                     fema_data=fema_data,
                                                     usgs_data=usgs_data,
                                                     water_data=water_data)
        return jsonify({'lat': lat, 'lon': lon, 'forecast': daily_risks})
    except Exception as e:
        logger.exception('Forecast risk error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai-weather')
def api_ai_weather():
    """Weather-scoped AI assistant.  Accepts a natural-language question and an
    optional lat/lon context.  If no coords are provided it attempts to extract
    a place name from the question itself, geocodes it, and grounds the answer
    in live Open-Meteo 7-day forecast data.
    """
    question = (request.args.get('q') or request.args.get('question') or '').strip()

    if not question:
        return jsonify({'error': 'Provide a "q" (question) parameter'}), 400

    # --- Extract place name entirely from the question text ---
    # The AI is fully self-contained: it never receives coordinates from the main page.
    lat, lon = None, None
    loc_name = ''
    # Words that look like places but aren't
    _skip_words = {
        'the','a','an','my','our','it','this','that','there','here',
        'today','tomorrow','weekend','week','month','year','weather',
        'forecast','rain','snow','wind','flood','temperature','there',
        'skiing','snowboarding','surfing','hiking','camping','climbing',
        'right','good','safe','ok','okay','fine','bad','great','terrible',
        'conditions','conditions','area','place','location','town','city',
        'there','anywhere','somewhere','anywhere','everywhere','nowhere',
    }

    def _try_geocode(candidate):
        """Strip punctuation, check skip list, attempt geocode. Returns geo tuple or None."""
        candidate = re.sub(r'[,\.\?!\s]+$', '', candidate).strip()
        if not candidate or len(candidate) < 2 or candidate.lower() in _skip_words:
            return None
        try:
            return geocode(candidate)
        except Exception:
            return None

    def _extract_place(text):
        """
        Walk through ordered trigger patterns (most-specific first).
        Returns the first candidate that geocodes successfully.
        """
        q = text.lower()
        # Ordered list of (trigger_phrase, grab_words_after)
        # Each trigger: find trigger in q, slice original text after it, grab 1-3 words
        triggers = [
            # specific compound triggers first
            'skiing at', 'ski at', 'skiing in', 'ski in',
            'snowboarding at', 'snowboarding in',
            'planning on skiing at', 'planning on skiing in',
            'going to', 'travel to', 'fly to', 'drive to', 'visiting',
            'weather in', 'weather at', 'weather for',
            'forecast for', 'forecast in',
            'conditions in', 'conditions at',
            'how is it in', 'how is it at', 'how is it looking in',
            # generic single-word triggers last (most false-positive-prone)
            ' in ', ' at ', ' near ', ' around ',
        ]
        for trigger in triggers:
            idx = q.find(trigger)
            if idx == -1:
                continue
            after = text[idx + len(trigger):].strip()
            if not after:
                continue
            # Grab up to 3 words, stopping at sentence-ending punctuation or stop words
            STOP = {'this','next','today','tomorrow','now','weekend','week',
                    'will','is','are','was','be','the','how','what','when',
                    'where','why','who','all','and','but','or','if','should',
                    'can','could','would','i','we','you','they','he','she',
                    'it','my','our','your','their','his','her',}
            words = re.split(r'[\s,]+', after)
            place_words = []
            for w in words[:4]:
                clean = re.sub(r'[,\.\?!]+$', '', w)
                if clean.lower() in STOP or not clean:
                    break
                place_words.append(clean)
            candidate = ' '.join(place_words)
            geo = _try_geocode(candidate)
            if geo:
                return geo
            # Also try first word alone (e.g. "Seattle, WA" → try "Seattle")
            if len(place_words) > 1:
                geo = _try_geocode(place_words[0])
                if geo:
                    return geo
        return None

    if lat is None or lon is None:
        geo = _extract_place(question)
        if geo:
            lat, lon, loc_name, _ = geo
        else:
            # Fallback: try the last 1-3 words of the question as a place name
            # Handles patterns like "Should I carry an umbrella today in Mumbai"
            _words_raw = re.split(r'[\s,]+', question.rstrip('?!. '))
            for n in (3, 2, 1):
                if len(_words_raw) >= n:
                    _candidate = ' '.join(_words_raw[-n:])
                    _geo2 = _try_geocode(_candidate)
                    if _geo2:
                        lat, lon, loc_name, _ = _geo2
                        break

    # --- Extract a specific date from the question (enables historical queries) ---
    from datetime import datetime as _dt_ai, timedelta as _td_ai
    _hist_date     = None
    _hist_weather_day = None

    # Match MM/DD/YYYY or M/D/YYYY (US format), also YYYY-MM-DD
    _dm = re.search(r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b', question)
    if _dm:
        try:
            _hist_date = _dt_ai.strptime(
                f"{int(_dm.group(3))}-{int(_dm.group(1)):02d}-{int(_dm.group(2)):02d}",
                '%Y-%m-%d').date()
        except Exception:
            pass
    else:
        _dm2 = re.search(r'\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b', question)
        if _dm2:
            try:
                _hist_date = _dt_ai.strptime(
                    f"{_dm2.group(1)}-{int(_dm2.group(2)):02d}-{int(_dm2.group(3)):02d}",
                    '%Y-%m-%d').date()
            except Exception:
                pass

    # Gather live weather context if we have coords
    ctx_lines = []
    forecast_daily = {}
    WMO_SHORT = {0:'clear sky',1:'mainly clear',2:'partly cloudy',3:'overcast',
                 45:'fog',51:'light drizzle',53:'drizzle',55:'heavy drizzle',
                 61:'light rain',63:'rain',65:'heavy rain',71:'light snow',
                 73:'snow',75:'heavy snow',80:'showers',81:'moderate showers',
                 82:'heavy showers',95:'thunderstorm',96:'thunderstorm+hail'}
    if lat is not None and lon is not None:
        try:
            forecast_daily = get_forecast_weather(lat, lon, 7)
            times  = forecast_daily.get('time', [])
            precip = forecast_daily.get('precipitation_sum', [])
            tmax   = forecast_daily.get('temperature_2m_max', [])
            tmin   = forecast_daily.get('temperature_2m_min', [])
            wcode  = forecast_daily.get('weathercode', [])
            for i, d in enumerate(times[:7]):
                wdesc = WMO_SHORT.get(int(wcode[i]) if i < len(wcode) and wcode[i] is not None else 0, '')
                pr    = precip[i] if i < len(precip) and precip[i] is not None else 0
                tx    = tmax[i]   if i < len(tmax)   and tmax[i]   is not None else '?'
                tn    = tmin[i]   if i < len(tmin)   and tmin[i]   is not None else '?'
                ctx_lines.append(f'{d}: {wdesc}, max {tx}°C, min {tn}°C, precip {pr:.1f}mm')
            ctx_lines.insert(0, f'Location: {loc_name or f"{lat:.3f},{lon:.3f}"}')
        except Exception:
            pass

        # If the question mentions a past date, fetch actual historical weather for it
        if _hist_date and _hist_date < _dt_ai.utcnow().date():
            try:
                _hstart = (_hist_date - _td_ai(days=6)).strftime('%Y-%m-%d')
                _hend   = _hist_date.strftime('%Y-%m-%d')
                _hdata  = get_historical_weather(lat, lon, _hstart, _hend)
                if _hdata and 'daily' in _hdata:
                    _hd     = _hdata['daily']
                    _hds    = _hist_date.strftime('%Y-%m-%d')
                    _htimes = _hd.get('time', [])
                    if _hds in _htimes:
                        _hidx = _htimes.index(_hds)
                        _hp   = _hd.get('precipitation_sum', [])
                        _hw   = _hd.get('weathercode', [])
                        _wdesc_hist = WMO_SHORT.get(
                            int(_hw[_hidx]) if _hidx < len(_hw) and _hw[_hidx] is not None else 0, '')
                        _hist_weather_day = {
                            'date':      _hds,
                            'precip_mm': float(_hp[_hidx]) if _hidx < len(_hp) and _hp[_hidx] is not None else 0.0,
                            'tmax':      (_hd.get('temperature_2m_max') or [None])[_hidx],
                            'tmin':      (_hd.get('temperature_2m_min') or [None])[_hidx],
                            'windspeed': (_hd.get('windspeed_10m_max')  or [None])[_hidx],
                            'wdesc':     _wdesc_hist,
                            'cum_7day':  sum(
                                float(v) for v in _hp[max(0, _hidx-6):_hidx+1] if v is not None
                            ),
                        }
            except Exception:
                pass

    # Rule-based weather Q&A engine
    q_lower = question.lower()
    answer  = None
    ctx     = '\n'.join(ctx_lines)
    place   = loc_name or 'the area you mentioned'
    no_loc  = lat is None

    # --- Ski / outdoor activities ---
    if any(w in q_lower for w in ['ski','skiing','snowboard','slope','powder','resort','lift']):
        if no_loc:
            answer = ("I couldn't identify a specific location in your question. "
                      "Try including a place name, e.g. **'Will it snow in Tahoe this weekend?'**")
        else:
            snow_days  = [i for i, c in enumerate(forecast_daily.get('weathercode', []))
                          if c is not None and int(c) in range(71, 78)]
            tmaxs      = forecast_daily.get('temperature_2m_max', [])
            tmins      = forecast_daily.get('temperature_2m_min', [])
            times      = forecast_daily.get('time', [])
            cold_days  = [i for i, v in enumerate(tmins) if v is not None and float(v) < 2]
            # Weekend = index 5 & 6 (Saturday/Sunday from today)
            weekend    = [5, 6]
            snow_wknd  = [i for i in snow_days  if i in weekend]
            cold_wknd  = [i for i in cold_days  if i in weekend]
            if snow_wknd:
                answer = (f"**Great news for skiing in {place}!** ❄️\n"
                          f"Snow is forecast on {'Saturday' if 5 in snow_wknd else ''}"
                          f"{'and ' if 5 in snow_wknd and 6 in snow_wknd else ''}"
                          f"{'Sunday' if 6 in snow_wknd else ''} this weekend. "
                          f"Temperatures will be around {tmins[snow_wknd[0]]:.0f}–{tmaxs[snow_wknd[0]]:.0f}°C — "
                          "ideal powder conditions. Check the resort's snow report and trail status before heading out!")
            elif cold_wknd:
                answer = (f"No fresh snow is forecast for {place} this weekend, but it will be **cold** "
                          f"(lows near {min(tmins[i] for i in cold_wknd if tmins[i] is not None):.0f}°C). "
                          "Existing snow base may hold — check the resort's snow conditions report. "
                          "Dress in layers!")
            else:
                avg_max_wknd = [tmaxs[i] for i in weekend if i < len(tmaxs) and tmaxs[i] is not None]
                temp_note = f"Highs around {sum(avg_max_wknd)/len(avg_max_wknd):.0f}°C" if avg_max_wknd else ''
                answer = (f"The weekend forecast for {place} doesn't show fresh snow ❌ "
                          f"and {temp_note} — conditions may be slushy or icy. "
                          "Check the resort's snow report for current base depth. "
                          "Mid-week snowfall earlier in the week could still leave a decent base!")

    # --- Flood / rain risk ---
    elif any(w in q_lower for w in ['flood','flooding','inundation','overflow','surge']):
        if no_loc:
            answer = ("Ask me about a specific place, e.g. **'Is there flood risk in New Orleans?'** "
                      "and I'll check the forecast for you.")
        elif _hist_weather_day:
            # Historical date query — answer from actual recorded data
            p   = _hist_weather_day['precip_mm']
            cum = _hist_weather_day['cum_7day']
            d_s = _hist_weather_day['date']
            tx  = _hist_weather_day.get('tmax')
            wd  = _hist_weather_day.get('wdesc', '')
            _temp_note = f" Temperature: {tx:.1f}°C." if tx is not None else ''
            if p > 40 or cum > 120:
                answer = (
                    f"On **{d_s}**, **{place}** recorded **{p:.1f} mm** of precipitation "
                    f"(7-day cumulative: **{cum:.1f} mm**) — {wd or 'heavy rain'}."
                    f"{_temp_note} "
                    f"{'⚠️ Extremely heavy rainfall — very high likelihood of significant flooding events.' if p > 60 else '⚠️ Heavy rainfall strongly associated with flood conditions.'} "
                    "Check FEMA flood declarations or local emergency management records for official confirmation."
                )
            elif p > 10:
                answer = (
                    f"On **{d_s}**, **{place}** received **{p:.1f} mm** of rain "
                    f"(7-day total: **{cum:.1f} mm**) — {wd or 'moderate rain'}.{_temp_note} "
                    "Moderate-to-heavy rainfall — localised or minor flooding may have occurred "
                    "in low-lying and riverside areas. "
                    "Check FEMA or your local emergency management agency for official flood declarations."
                )
            elif p > 0:
                answer = (
                    f"On **{d_s}**, **{place}** received only **{p:.1f} mm** of precipitation "
                    f"(7-day total: {cum:.1f} mm) — {wd or 'light rain'}.{_temp_note} "
                    "Rainfall was light — significant flooding from precipitation alone is unlikely on this date. "
                    "However, upstream river conditions or snowmelt could still have contributed."
                )
            else:
                answer = (
                    f"Weather records show **no measurable precipitation** in **{place}** on **{d_s}**.{_temp_note} "
                    "Flooding from rainfall is very unlikely on this specific date. "
                    "Flooding could still occur from upstream river flow, dam releases, or tidal surge — "
                    "check FEMA or USGS streamflow records for confirmation."
                )
        else:
            rainy_days = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 5)
            if rainy_days >= 4:
                answer = (f"Based on the 7-day forecast for **{place}**, "
                          f"there are **{rainy_days} days** with significant precipitation (>5 mm). "
                          "This increases surface runoff and flood risk, especially in low-lying or "
                          "riverside areas. Monitor local emergency alerts and review the flood risk score above.")
            else:
                answer = (f"The 7-day forecast for **{place}** shows {rainy_days} day(s) with notable rain. "
                          "Flood risk appears relatively low in the near term, "
                          "but always check local authority warnings for real-time updates.")

    # --- Rain / precipitation ---
    elif any(w in q_lower for w in ['rain','precip','shower','drizzle','wet']):
        if no_loc:
            answer = "Try asking: **'Will it rain in Seattle this week?'** — I'll pull the live forecast!"
        elif _hist_weather_day:
            p   = _hist_weather_day['precip_mm']
            cum = _hist_weather_day['cum_7day']
            d_s = _hist_weather_day['date']
            wd  = _hist_weather_day.get('wdesc', '')
            if p > 0:
                intensity = ('Heavy' if p > 20 else 'Moderate' if p > 5 else 'Light')
                answer = (
                    f"On **{d_s}**, **{place}** recorded **{p:.1f} mm** of precipitation "
                    f"({wd or intensity.lower() + ' rain'}). "
                    f"7-day cumulative: **{cum:.1f} mm**. "
                    f"{intensity} rainfall {'— potential for localised flooding.' if p > 20 else '.'}"
                )
            else:
                answer = f"Weather records show **no precipitation** in **{place}** on **{d_s}**. It was a dry day."
        else:
            rainy = [(t, p) for t, p in zip(forecast_daily.get('time', []),
                                             forecast_daily.get('precipitation_sum', []))
                     if p and float(p) > 0.5]
            if rainy:
                day_list = ', '.join(f"{t} ({float(p):.1f} mm)" for t, p in rainy[:4])
                answer = f"Rain is expected in **{place}** on: {day_list}. {'Bring an umbrella! ☂️' if len(rainy) >= 3 else 'Mostly dry otherwise.'}"
            else:
                answer = f"No significant rain is forecast in the next 7 days for **{place}**. Enjoy the dry weather! ☀️"

    # --- Temperature / heat ---
    elif any(w in q_lower for w in ['temp','hot','cold','heat','warm','cool','freeze','frost','celsius','fahrenheit','degrees']):
        if no_loc:
            answer = "Try asking: **'How cold will it be in Denver this week?'** and I'll check the forecast!"
        elif _hist_weather_day:
            tx  = _hist_weather_day.get('tmax')
            tn  = _hist_weather_day.get('tmin')
            wd  = _hist_weather_day.get('wdesc', '')
            d_s = _hist_weather_day['date']
            if tx is not None:
                answer = (
                    f"On **{d_s}**, **{place}** had a high of **{tx:.1f}°C** "
                    f"and a low of **{tn:.1f}°C**{' — ' + wd if wd else ''}. "
                    f"{'⚠️ Below-freezing temperatures.' if tn is not None and float(tn) < 0 else ''}"
                )
            else:
                answer = f"Temperature data is not available for **{place}** on **{d_s}**."
        else:
            tmaxs = [v for v in forecast_daily.get('temperature_2m_max', []) if v is not None]
            tmins = [v for v in forecast_daily.get('temperature_2m_min', []) if v is not None]
            if tmaxs:
                avg_max = sum(tmaxs) / len(tmaxs)
                avg_min = sum(tmins) / len(tmins) if tmins else 0
                answer = (f"Next 7 days in **{place}**: "
                          f"average high **{avg_max:.1f}°C**, average low **{avg_min:.1f}°C**. "
                          f"Peak: {max(tmaxs):.1f}°C. {'⚠️ Frost risk.' if min(tmins) < 2 else ''}")
            else:
                answer = "Temperature data is not available for this location right now."

    # --- Wind ---
    elif any(w in q_lower for w in ['wind','gust','breeze','storm','hurricane','typhoon','cyclone']):
        if no_loc:
            answer = "Try asking: **'How windy will it be in Chicago this week?'**"
        else:
            winds = [v for v in forecast_daily.get('windspeed_10m_max', []) if v is not None]
            if winds:
                max_wind = max(winds)
                avg_wind = sum(winds) / len(winds)
                level = 'strong 💨' if max_wind > 60 else ('moderate' if max_wind > 30 else 'light')
                answer = (f"Wind forecast for **{place}**: average {avg_wind:.1f} km/h, "
                          f"peak {max_wind:.1f} km/h — {level} winds. "
                          f"{'⚠️ Take precautions outdoors.' if max_wind > 50 else 'No major wind hazard expected.'}")
            else:
                answer = f"Wind data is currently unavailable for **{place}**."

    # --- Snow / ice ---
    elif any(w in q_lower for w in ['snow','ice','blizzard','sleet','hail','frost']):
        if no_loc:
            answer = "Try asking: **'Will it snow in Denver this weekend?'** and I'll check!"
        else:
            snow_days = sum(1 for c in forecast_daily.get('weathercode', []) if c and int(c) in range(71, 78))
            tmins = [v for v in forecast_daily.get('temperature_2m_min', []) if v is not None]
            if snow_days > 0:
                answer = (f"❄️ Snow or icy conditions are possible on **{snow_days} day(s)** in the next week "
                          f"for **{place}**. Drive carefully and prepare for reduced visibility.")
            elif tmins and min(tmins) < 2:
                answer = (f"No snow is forecast, but temperatures will drop below 2°C in **{place}**, "
                          "so frost is possible. Protect exposed pipes and plants.")
            else:
                answer = f"No snow or icy conditions are expected in the next 7 days for **{place}**."

    # --- Forecast / what's the weather ---
    elif any(w in q_lower for w in ['forecast','tomorrow','this week','today','weather like','what will','how is','how\'s','looking']):
        if no_loc:
            answer = "Try asking: **'What's the weather like in Miami this week?'** and I'll show you the full 7-day forecast!"
        elif _hist_weather_day:
            p   = _hist_weather_day['precip_mm']
            tx  = _hist_weather_day.get('tmax')
            tn  = _hist_weather_day.get('tmin')
            wd  = _hist_weather_day.get('wdesc', 'data available')
            ws  = _hist_weather_day.get('windspeed')
            d_s = _hist_weather_day['date']
            _parts = [f"📅 Weather in **{place}** on **{d_s}**:"]
            if wd:  _parts.append(f"Conditions: **{wd}**")
            if tx is not None: _parts.append(f"High: **{tx:.1f}°C**, Low: **{tn:.1f}°C**")
            if p is not None:  _parts.append(f"Precipitation: **{p:.1f} mm**")
            if ws is not None: _parts.append(f"Max wind: **{ws:.1f} km/h**")
            answer = '\n'.join(_parts)
        else:
            if ctx_lines:
                answer = f"📅 7-day forecast for **{place}**:\n" + '\n'.join(ctx_lines[1:])
            else:
                answer = f"Could not load forecast data for **{place}** right now. Please try again."

    # --- Evacuation / safety ---
    elif any(w in q_lower for w in ['evacuat','safe','escape','leave','shelter','emergency']):
        answer = ("For evacuation guidance, use the **🚨 Smart Evacuation Routes** section — it computes "
                  "up to 3 road-based escape routes to higher ground. Always follow official emergency services "
                  "instructions. For real-time emergency alerts, visit your local civil defense agency website.")

    # --- UV / sun ---
    elif any(w in q_lower for w in ['uv','sunny','sunscreen','sunshine','sun']):
        if no_loc:
            answer = "Try asking: **'How sunny will it be in Los Angeles this week?'**"
        else:
            clear_days = sum(1 for c in forecast_daily.get('weathercode', []) if c is not None and int(c) <= 3)
            answer = (f"There are **{clear_days} clear or mostly clear day(s)** forecast for "
                      f"**{place}** this week. ☀️ On clear days UV index can be high — "
                      "apply sunscreen (SPF 30+) if spending time outdoors.")

    # --- Humidity / fog ---
    elif any(w in q_lower for w in ['humid','fog','mist','visibility','damp','muggy']):
        if no_loc:
            answer = "Try asking: **'Is it foggy in San Francisco this week?'**"
        else:
            fog_days = sum(1 for c in forecast_daily.get('weathercode', []) if c in (45, 48))
            answer = (f"Fog or mist is expected on **{fog_days} day(s)** in the next week for "
                      f"**{place}**. High humidity and fog reduce visibility — drive with caution.")

    # --- Hiking / trail / outdoor walk ---
    elif any(w in q_lower for w in ['hik','trail','trek','backpack','walk outdoor','nature walk']):
        if no_loc:
            answer = "Try: **'Is it good for hiking in Mt. Rainier this weekend?'**"
        else:
            rain_days = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 2)
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            winds = forecast_daily.get('windspeed_10m_max', [])
            avg_t = sum(v for v in tmaxs if v is not None) / max(len([v for v in tmaxs if v is not None]), 1)
            max_w = max((v for v in winds if v is not None), default=0)
            tips = []
            if rain_days >= 3: tips.append('🌧️ Pack rain gear — multiple wet days ahead')
            elif rain_days == 0: tips.append('☀️ Dry conditions — great trail weather')
            else: tips.append(f'🌦️ {rain_days} day(s) with rain — check before you head out')
            if avg_t < 5: tips.append('🧤 Cold temps — dress in warm layers')
            elif avg_t > 28: tips.append('🥵 Hot — bring extra water, start early')
            if max_w > 50: tips.append('💨 Strong winds expected — exposed ridges may be dangerous')
            tips.append(f'🌡️ Average high: {avg_t:.0f}°C | Max wind: {max_w:.0f} km/h')
            answer = f"**Hiking outlook for {place}:**\n" + '\n'.join(f"• {t}" for t in tips)

    # --- BBQ / picnic / outdoor party ---
    elif any(w in q_lower for w in ['bbq','barbecue','grill','picnic','cookout','outdoor party','outdoor event','block party']):
        if no_loc:
            answer = "Try: **'Good day for a BBQ in Tacoma this weekend?'**"
        else:
            clear = sum(1 for c in forecast_daily.get('weathercode', []) if c is not None and int(c) <= 3)
            rain = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 1)
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            avg_t = sum(v for v in tmaxs if v is not None) / max(len([v for v in tmaxs if v is not None]), 1)
            if clear >= 4 and rain <= 1 and avg_t > 15:
                answer = f"**Great week for outdoor plans in {place}!** 🍖☀️ {clear} clear days, avg highs {avg_t:.0f}°C. Fire up the grill!"
            elif rain >= 3:
                answer = f"Hmm, **{rain} rainy days** ahead in {place} 🌧️ — you might want a backup indoor plan or a canopy. Avg highs: {avg_t:.0f}°C."
            else:
                answer = f"Mixed forecast for {place}: {clear} clear days, {rain} rainy. Average highs: {avg_t:.0f}°C. Check the daily breakdown to pick the best day! 🌤️"

    # --- Running / jogging / cycling / exercise ---
    elif any(w in q_lower for w in ['run','jog','running','jogging','cycl','biking','bike','exercise','workout','marathon','training']):
        if no_loc:
            answer = "Try: **'Good running weather in Bellevue this week?'**"
        else:
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            tmins = forecast_daily.get('temperature_2m_min', [])
            winds = forecast_daily.get('windspeed_10m_max', [])
            rain_days = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 1)
            avg_hi = sum(v for v in tmaxs if v is not None) / max(len([v for v in tmaxs if v is not None]), 1)
            avg_lo = sum(v for v in tmins if v is not None) / max(len([v for v in tmins if v is not None]), 1)
            tips = []
            if 10 <= avg_hi <= 22: tips.append('✅ Ideal temperature range for running/cycling')
            elif avg_hi > 28: tips.append('⚠️ Hot — hydrate well, run early morning or evening')
            elif avg_hi < 5: tips.append('🧤 Cold — wear layers, protect extremities')
            if rain_days >= 3: tips.append(f'🌧️ {rain_days} wet days — waterproof shoes recommended')
            else: tips.append(f'🌤️ Mostly dry — {rain_days} day(s) with rain')
            max_w = max((v for v in winds if v is not None), default=0)
            if max_w > 40: tips.append(f'💨 Gusts up to {max_w:.0f} km/h — headwind on exposed routes')
            tips.append(f'🌡️ Highs: {avg_hi:.0f}°C, Lows: {avg_lo:.0f}°C')
            answer = f"**Running/cycling outlook for {place}:**\n" + '\n'.join(f"• {t}" for t in tips)

    # --- Beach / swim / surf ---
    elif any(w in q_lower for w in ['beach','swim','surf','ocean','lake','pool','water park','kayak','canoe','boat','sailing']):
        if no_loc:
            answer = "Try: **'Beach weather in Long Beach this weekend?'**"
        else:
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            clear = sum(1 for c in forecast_daily.get('weathercode', []) if c is not None and int(c) <= 3)
            avg_t = sum(v for v in tmaxs if v is not None) / max(len([v for v in tmaxs if v is not None]), 1)
            if avg_t > 24 and clear >= 3:
                answer = f"**Great beach/water weather in {place}!** 🏖️ {clear} sunny days, avg highs {avg_t:.0f}°C. Don't forget sunscreen!"
            elif avg_t < 15:
                answer = f"It's on the cool side in {place} ({avg_t:.0f}°C average). 🥶 Not ideal for swimming — consider a wetsuit or indoor pool."
            else:
                answer = f"Mixed conditions in {place}: {clear} clear days, avg highs {avg_t:.0f}°C. Pick a sunny day from the forecast for your best beach day! 🌊"

    # --- Fishing ---
    elif any(w in q_lower for w in ['fish','fishing','angling','casting']):
        if no_loc:
            answer = "Try: **'Good fishing weather in Puget Sound this week?'**"
        else:
            rain = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 2)
            winds = forecast_daily.get('windspeed_10m_max', [])
            max_w = max((v for v in winds if v is not None), default=0)
            tips = ['Overcast/light drizzle days are often best for fishing 🎣']
            if max_w > 40: tips.append(f'⚠️ Strong winds ({max_w:.0f} km/h) — dangerous on open water')
            if rain >= 4: tips.append('🌧️ Heavy rain can muddy rivers and reduce visibility')
            answer = f"**Fishing outlook for {place}:**\n" + '\n'.join(f"• {t}" for t in tips)

    # --- Road trip / commute / driving ---
    elif any(w in q_lower for w in ['road trip','drive','driving','commut','traffic','travel','road condition']):
        if no_loc:
            answer = "Try: **'Driving conditions on I-90 near Snoqualmie this weekend?'**"
        else:
            snow_days = sum(1 for c in forecast_daily.get('weathercode', []) if c is not None and int(c) in range(71, 78))
            rain_days = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 5)
            fog_days = sum(1 for c in forecast_daily.get('weathercode', []) if c in (45, 48))
            winds = forecast_daily.get('windspeed_10m_max', [])
            max_w = max((v for v in winds if v is not None), default=0)
            tips = []
            if snow_days: tips.append(f'❄️ {snow_days} day(s) with snow/ice — chains may be required on passes')
            if rain_days: tips.append(f'🌧️ {rain_days} day(s) of heavy rain — reduced visibility, hydroplaning risk')
            if fog_days: tips.append(f'🌫️ {fog_days} day(s) with fog — drive with low beams')
            if max_w > 50: tips.append(f'💨 Strong winds ({max_w:.0f} km/h) — be cautious with high-profile vehicles')
            if not tips: tips.append('✅ Clear driving conditions expected — enjoy the trip!')
            answer = f"**Driving outlook for {place}:**\n" + '\n'.join(f"• {t}" for t in tips)

    # --- Gardening / farming / planting ---
    elif any(w in q_lower for w in ['garden','plant','planting','farming','mow','lawn','harvest','compost','seed']):
        if no_loc:
            answer = "Try: **'Good planting weather in Yakima this week?'**"
        else:
            tmins = forecast_daily.get('temperature_2m_min', [])
            rain = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 0.5)
            frost = sum(1 for v in tmins if v is not None and float(v) < 1)
            tips = []
            if frost: tips.append(f'⚠️ {frost} night(s) near or below freezing — protect tender plants!')
            else: tips.append('✅ No frost risk — safe for planting')
            if rain >= 3: tips.append(f'🌧️ {rain} rainy days — great for newly planted seeds, skip watering')
            elif rain == 0: tips.append('☀️ Dry week — make sure to water regularly')
            answer = f"**Gardening outlook for {place}:**\n" + '\n'.join(f"• {t}" for t in tips)

    # --- Wedding / outdoor ceremony / party / event ---
    elif any(w in q_lower for w in ['wedding','ceremony','party','celebration','graduation','prom','reception']):
        if no_loc:
            answer = "Try: **'Weather for an outdoor wedding in Leavenworth this Saturday?'**"
        else:
            times = forecast_daily.get('time', [])
            precips = forecast_daily.get('precipitation_sum', [])
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            codes = forecast_daily.get('weathercode', [])
            best_day = None
            best_score = -1
            for i, d in enumerate(times[:7]):
                p = float(precips[i]) if i < len(precips) and precips[i] is not None else 99
                c = int(codes[i]) if i < len(codes) and codes[i] is not None else 99
                t = float(tmaxs[i]) if i < len(tmaxs) and tmaxs[i] is not None else 0
                score = (10 if p < 1 else 5 if p < 5 else 0) + (5 if c <= 3 else 2 if c <= 55 else 0) + (5 if 15 < t < 28 else 0)
                if score > best_score:
                    best_score, best_day = score, d
            rain_days = sum(1 for v in precips if v and float(v) > 1)
            answer = (f"**Outdoor event outlook for {place}:**\n"
                      f"• Best day this week: **{best_day}** 🎉\n"
                      f"• {rain_days} day(s) with rain in the forecast\n"
                      f"• {'Have a backup indoor option ready 🏠' if rain_days >= 3 else 'Conditions look favorable! ☀️'}")

    # --- Photography / stargazing ---
    elif any(w in q_lower for w in ['photo','photograph','camera','sunset','sunrise','stargaz','aurora','northern light']):
        if no_loc:
            answer = "Try: **'Good sunset photography weather in Olympic Peninsula?'**"
        else:
            clear = sum(1 for c in forecast_daily.get('weathercode', []) if c is not None and int(c) <= 2)
            partly = sum(1 for c in forecast_daily.get('weathercode', []) if c is not None and int(c) == 2)
            answer = (f"**Photography outlook for {place}:**\n"
                      f"• {clear} clear/mostly clear day(s) — great for golden hour & stargazing 📸\n"
                      f"• {partly} partly cloudy day(s) — dramatic sunset/sunrise potential\n"
                      f"• {'Minimal cloud cover — perfect for astrophotography! 🌌' if clear >= 4 else 'Check daily forecast to pick the clearest evening.'}")

    # --- Dog walking / pet ---
    elif any(w in q_lower for w in ['dog','pet','walk my','walking my','puppy']):
        if no_loc:
            answer = "Try: **'Good dog walking weather in Redmond this week?'**"
        else:
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            rain = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 1)
            avg_t = sum(v for v in tmaxs if v is not None) / max(len([v for v in tmaxs if v is not None]), 1)
            tips = []
            if avg_t > 30: tips.append('🥵 Hot pavement — walk early morning or late evening to protect paws')
            elif avg_t < 0: tips.append('❄️ Very cold — short walks, consider dog booties')
            else: tips.append(f'🌡️ Comfortable temps ({avg_t:.0f}°C) for walks')
            if rain >= 3: tips.append(f'🌧️ {rain} wet days — keep a towel handy!')
            else: tips.append(f'🌤️ Mostly dry — {rain} day(s) with rain')
            answer = f"**Dog walking outlook for {place}:** 🐕\n" + '\n'.join(f"• {t}" for t in tips)

    # --- Umbrella / what to wear / clothing ---
    elif any(w in q_lower for w in ['umbrella','wear','dress','jacket','coat','layer','cloth','outfit','attire','pack','bring','carry']):
        if no_loc:
            answer = "Try: **'Should I bring an umbrella in Seattle today?'**"
        else:
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            tmins = forecast_daily.get('temperature_2m_min', [])
            rain_days = sum(1 for v in forecast_daily.get('precipitation_sum', []) if v and float(v) > 0.5)
            avg_hi = sum(v for v in tmaxs if v is not None) / max(len([v for v in tmaxs if v is not None]), 1)
            avg_lo = sum(v for v in tmins if v is not None) / max(len([v for v in tmins if v is not None]), 1)
            tips = []
            if rain_days >= 2: tips.append(f'☂️ Yes, bring an umbrella! {rain_days} day(s) with rain expected')
            else: tips.append('☀️ Mostly dry — umbrella probably not needed')
            if avg_hi > 25: tips.append('👕 Light, breathable clothing — it will be warm')
            elif avg_hi > 15: tips.append('🧥 Light jacket recommended for mornings/evenings')
            elif avg_hi > 5: tips.append('🧥 Wear a warm jacket and layers')
            else: tips.append('🧤 Bundle up! Heavy coat, gloves, hat recommended')
            if avg_lo < 3: tips.append('❄️ Near-freezing lows — warm layers essential')
            answer = f"**What to wear in {place}:**\n" + '\n'.join(f"• {t}" for t in tips)

    # --- Generic fallback — smart weather-based lifestyle advisor ---
    else:
        if not no_loc and ctx_lines:
            # Build a smart summary with actionable advice
            tmaxs = forecast_daily.get('temperature_2m_max', [])
            tmins = forecast_daily.get('temperature_2m_min', [])
            precips = forecast_daily.get('precipitation_sum', [])
            avg_hi = sum(v for v in tmaxs if v is not None) / max(len([v for v in tmaxs if v is not None]), 1)
            rain_days = sum(1 for v in precips if v and float(v) > 1)
            snow_days = sum(1 for c in forecast_daily.get('weathercode', []) if c is not None and int(c) in range(71, 78))
            tips = []
            if rain_days >= 3: tips.append(f'🌧️ Wet week — {rain_days} rainy days, keep an umbrella handy')
            elif rain_days == 0: tips.append('☀️ Dry week — great for outdoor activities')
            else: tips.append(f'🌦️ {rain_days} day(s) with rain, otherwise looking good')
            if snow_days: tips.append(f'❄️ {snow_days} day(s) with snow — check road conditions')
            if avg_hi > 25: tips.append('🥵 Warm — stay hydrated, apply sunscreen')
            elif avg_hi < 5: tips.append('🧤 Cold — dress warmly, watch for ice')
            tips_str = '\n'.join(f"• {t}" for t in tips)
            forecast_str = '\n'.join(ctx_lines[1:4])
            answer = (f"**Weather overview for {place}:**\n{forecast_str}\n\n"
                      f"**Tips:**\n{tips_str}\n\n"
                      "Ask me about **skiing, hiking, BBQ, driving, fishing, gardening, running, beach, photography, "
                      "events, clothing advice** — or any weather question!")
        elif no_loc:
            answer = ("I'm your **FloodWise AI** weather assistant! 🌊\n\n"
                      "Just ask me about any place — I can help with:\n"
                      "• ☂️ *Should I bring an umbrella in Seattle?*\n"
                      "• ⛷️ *Skiing conditions at Snoqualmie Pass this weekend?*\n"
                      "• 🥾 *Good hiking weather near Mt. Rainier?*\n"
                      "• 🍖 *BBQ weather in Tacoma this Saturday?*\n"
                      "• 🚗 *Driving conditions on I-90?*\n"
                      "• 🌊 *Is there flood risk in New Orleans?*\n"
                      "• 📸 *Clear skies for stargazing in Ellensburg?*")
        else:
            answer = (f"I'm your FloodWise weather assistant for **{place}**! "
                      "Ask me about rain, flooding, temperature, wind, snow, skiing, hiking, BBQ, "
                      "driving, fishing, gardening, running, events, or what to wear!")

    return jsonify({
        'question': question,
        'answer':   answer,
        'context':  ctx,
        'resolved_location': loc_name,
    })


@app.route('/api/full-report')
def api_full_report():
    """Single endpoint: geocode + current weather + 30-day history + FEMA + USGS + flood risk.
    Parameters:
      - location: place name or address
      - date:     YYYY-MM-DD  (defaults to today)
    """
    from datetime import datetime, timedelta

    location = request.args.get('location') or request.args.get('q', '').strip()
    date_str  = request.args.get('date', '').strip()

    if not location:
        return jsonify({'error': 'missing "location" parameter'}), 400

    if not date_str:
        date_str = datetime.utcnow().strftime('%Y-%m-%d')

    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'invalid date format, use YYYY-MM-DD'}), 400

    # 1. Geocode
    try:
        geo = geocode(location)
    except Exception as e:
        return jsonify({'error': 'geocoding failed', 'detail': str(e)}), 500

    if not geo:
        return jsonify({'error': 'location not found',
                        'hint': 'Try a city name, country, or street address.'}), 404

    lat, lon, display_name, elevation = geo

    # 2–6. Compute date window capped to historical data available (archive API lags ~2 days).
    # Always use a fixed 30-day trailing window so we never request future dates.
    today       = datetime.utcnow().date()
    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    # hist_end is the earlier of (target_date) and (today - 2 days)
    hist_end_date   = min(target_date, today - timedelta(days=2))
    hist_start_date = hist_end_date - timedelta(days=30)
    hist_start = hist_start_date.strftime('%Y-%m-%d')
    hist_end   = hist_end_date.strftime('%Y-%m-%d')

    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    _t0 = _t.monotonic()
    _BUDGET = 7.0  # hard wall-clock budget — all 4 futures must finish within 7s

    def _get(future, default):
        remaining = max(0.1, _BUDGET - (_t.monotonic() - _t0))
        try:
            return future.result(timeout=remaining)
        except Exception:
            return default

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_weather = pool.submit(get_weather, lat, lon)
        f_hist    = pool.submit(get_historical_weather, lat, lon, hist_start, hist_end)
        f_fema    = pool.submit(get_fema_flood_history, lat, lon)
        f_usgs    = pool.submit(get_usgs_stream_gauge, lat, lon)
        f_water   = pool.submit(get_water_proximity_score, lat, lon)

        current_weather = _get(f_weather, None)
        historical_data = _get(f_hist,    None)
        # FEMA gets its own 6s budget — static fallback completes in <1ms once geocoded
        try:
            fema_data = f_fema.result(timeout=6)
        except Exception:
            fema_data = {'events': [], 'historical_risk_score': 0}
        usgs_data  = _get(f_usgs,  {'gauges': [], 'gauge_risk_score': 0})
        water_data = _get(f_water, (0, None, None))

    # 4. Weather on the specific date
    weather_that_day = None
    daily            = {}
    if historical_data and 'daily' in historical_data:
        daily = historical_data['daily']
        times = daily.get('time', [])
        if date_str in times:
            idx = times.index(date_str)
            weather_that_day = {
                'date':               date_str,
                'temperature_max_c':  daily.get('temperature_2m_max',  [None])[idx],
                'temperature_min_c':  daily.get('temperature_2m_min',  [None])[idx],
                'precipitation_mm':   daily.get('precipitation_sum',   [None])[idx],
                'windspeed_max_kmh':  daily.get('windspeed_10m_max',   [None])[idx],
            }

    # 7. Flood risk score
    flood_risk = None
    if daily and date_str in daily.get('time', []):
        flood_risk = calculate_flood_risk_for_date(
            lat, lon, date_str, daily,
            elevation_m=elevation,
            fema_data=fema_data,
            usgs_data=usgs_data,
            water_data=water_data,
        )

    # 8. Build Windy embed URL (precipitation overlay)
    windy_url = (
        f"https://embed.windy.com/embed2.html"
        f"?lat={lat}&lon={lon}&detailLat={lat}&detailLon={lon}"
        f"&width=650&height=380&zoom=9&level=surface&overlay=rain"
        f"&product=ecmwf&menu=&message=&marker=true&calendar=now"
        f"&pressure=&type=map&location=coordinates&detail="
        f"&metricWind=default&metricTemp=default&radarRange=-1"
    )

    return jsonify({
        'location':        display_name,
        'latitude':        lat,
        'longitude':       lon,
        'elevation_m':     elevation,
        'date':            date_str,
        'current_weather': current_weather,
        'weather_on_date': weather_that_day,
        'historical_daily': {
            'time':                daily.get('time', []),
            'precipitation_sum':   daily.get('precipitation_sum', []),
            'temperature_2m_max':  daily.get('temperature_2m_max', []),
            'temperature_2m_min':  daily.get('temperature_2m_min', []),
        },
        'flood_risk':  flood_risk,
        'fema_history': fema_data,
        'usgs_gauges':  usgs_data,
        'windy_embed_url': windy_url,
    })


@app.route('/api/evacuation-route')
def api_evacuation_route():
    """Compute up to 3 road-based evacuation routes from a flood-risk location.

    Parameters
    ----------
    location : str   — place name / address (alternative to lat+lon)
    lat      : float — latitude  (direct coordinate; skips geocoding)
    lon      : float — longitude (direct coordinate; skips geocoding)
    elevation: float — elevation in metres (optional, improves destination scoring)
    """
    from datetime import datetime as _dt

    lat      = request.args.get('lat',       type=float)
    lon      = request.args.get('lon',       type=float)
    elev     = request.args.get('elevation', type=float)
    location = request.args.get('location', '').strip()

    if lat is None or lon is None:
        if not location:
            return jsonify({'error': 'Provide "location" or "lat"+"lon" parameters'}), 400
        geo = geocode(location)
        if not geo:
            return jsonify({'error': 'location not found',
                            'hint': 'Try a city name or street address.'}), 404
        lat, lon, _display, elev = geo

    routes = get_evacuation_routes(lat, lon, elev)
    if not routes:
        return jsonify({
            'error': 'Could not compute evacuation routes',
            'hint': 'The routing service may be temporarily unavailable. Try again in a moment.',
        }), 503

    return jsonify({
        'origin':       {'lat': lat, 'lon': lon, 'elevation_m': elev},
        'routes':       routes,
        'generated_at': _dt.utcnow().isoformat() + 'Z',
    })


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.before_request
def log_request_info():
    logger.info('Incoming %s %s from %s', request.method, request.path, request.remote_addr)


@app.after_request
def add_cors(response):
    # Allow local testing from any origin — safe for dev only
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', action='store_true', help='run in CLI mode')
    parser.add_argument('--host', default=os.environ.get('HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '5000')))
    parser.add_argument('--debug', action='store_true', help='run with debug/reload enabled')
    args = parser.parse_args()
    if args.cli:
        q = input('Location: ').strip()
        if not q:
            print('No location provided')
        else:
            try:
                geo = geocode(q)
            except Exception as e:
                print('Geocoding error:', e)
                raise
            if not geo:
                print('Location not found')
            else:
                lat, lon, name, elevation = geo
                w = get_weather(lat, lon)
                print({'location': name, 'lat': lat, 'lon': lon, 'elevation_m': elevation, 'weather': w})
    else:
        logger.info('Starting server on %s:%s (debug=%s)', args.host, args.port, args.debug)
        app.run(host=args.host, port=args.port, debug=args.debug)


# ===========================================================================
# Community Experiences  — storage, vocabulary enhancer, routes
# ===========================================================================

_EXP_DB = os.path.join(os.environ.get('HOME', '/tmp'), 'floodwise_experiences.db')


def _exp_db():
    conn = _sqlite3.connect(_EXP_DB)
    conn.row_factory = _sqlite3.Row
    return conn


def _init_exp_db():
    with _exp_db() as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS experiences (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    DEFAULT "Anonymous",
                role         TEXT    DEFAULT "app-user",
                location     TEXT    DEFAULT "",
                exp_date     TEXT    DEFAULT "",
                story        TEXT    NOT NULL,
                enhanced     TEXT    DEFAULT "",
                impact       INTEGER DEFAULT 3,
                created_at   TEXT    DEFAULT (datetime("now"))
            )
        ''')
        # Add role column if upgrading from older schema
        try:
            db.execute('ALTER TABLE experiences ADD COLUMN role TEXT DEFAULT "app-user"')
            db.commit()
        except Exception:
            pass  # column already exists
        db.commit()


try:
    _init_exp_db()
except Exception as _e:
    logger.warning('Could not init experiences DB: %s', _e)


def enhance_vocabulary(text: str) -> str:
    """Enhance vocabulary of a community experience entry with more descriptive language."""
    swaps = [
        (r'\bsaw\b',              'witnessed'),
        (r'\bvery\b',             'remarkably'),
        (r'\breally\b',           'genuinely'),
        (r'\bsuper\b',            'extraordinarily'),
        (r'\bscary\b',            'deeply unsettling'),
        (r'\bscared\b',           'profoundly alarmed'),
        (r'\bfrightening\b',      'harrowing'),
        (r'\bworried\b',          'considerably concerned'),
        (r'\bworry\b',            'concern'),
        (r'\bhelped\b',           'proved invaluable'),
        (r'\bhelp\b',             'assistance'),
        (r'\baccurate\b',         'remarkably precise'),
        (r'\bquickly\b',          'rapidly'),
        (r'\bfast\b',             'swiftly'),
        (r'\bshowed\b',           'clearly demonstrated'),
        (r'\bshows\b',            'illustrates'),
        (r'\btold me\b',          'indicated'),
        (r'\bwarned\b',           'proactively alerted'),
        (r'\bwarning\b',          'critical advisory'),
        (r'\bgood\b',             'commendable'),
        (r'\bgreat\b',            'outstanding'),
        (r'\bamazing\b',          'remarkable'),
        (r'\bawesome\b',          'impressive'),
        (r'\bincredible\b',       'extraordinary'),
        (r'\buseful\b',           'invaluable'),
        (r'\bflooded\b',          'severely inundated'),
        (r'\bflooding\b',         'inundation'),
        (r'\bheavy rain\b',       'intense precipitation'),
        (r'\bheavy rainfall\b',   'torrential rainfall'),
        (r'\brain\b',             'precipitation'),
        (r'\bstorm\b',            'meteorological event'),
        (r'\brose\b',             'surged'),
        (r'\bwent up\b',          'escalated'),
        (r'\bfound out\b',        'discovered'),
        (r'\bfound\b',            'discovered'),
        (r'\bthought\b',          'recognized'),
        (r'\bthink\b',            'believe'),
        (r'\bstuff\b',            'conditions'),
        (r'\bthings\b',           'circumstances'),
        (r'\bthing\b',            'aspect'),
        (r'\bgot\b',              'encountered'),
        (r'\bused\b',             'utilized'),
        (r'\buse\b',              'utilize'),
        (r'\bchecked\b',          'consulted'),
        (r'\bcheck\b',            'consult'),
        (r'\bapp\b',              'application'),
        (r'\bwebsite\b',          'platform'),
        (r'\binfo\b',             'information'),
        (r'\bstayed safe\b',      "remained out of harm's way"),
        (r'\beveryone\b',         'the entire community'),
        (r'\bneighborhood\b',     'surrounding area'),
        (r'\bneighbours?\b',      'neighbouring residents'),
        (r'\bhouse\b',            'residence'),
        (r'\bprepared\b',         'well-prepared'),
        (r'\bevacuated\b',        'safely evacuated'),
        (r'\bescaped\b',          'successfully evacuated'),
        (r'\bsafe\b',             'secure'),
        (r'\bimpressed\b',        'thoroughly impressed'),
    ]
    result = text
    for pattern, replacement in swaps:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    # Capitalise first letter of each sentence
    sentences = re.split(r'(?<=[.!?])\s+', result.strip())
    sentences = [s[0].upper() + s[1:] if s else s for s in sentences]
    result = ' '.join(sentences)
    if result and result[-1] not in '.!?':
        result += '.'
    return result


@app.route('/experiences')
def page_experiences():
    return render_template('experiences.html')


@app.route('/api/experiences', methods=['GET'])
def api_experiences_get():
    try:
        with _exp_db() as db:
            rows = db.execute(
                'SELECT * FROM experiences ORDER BY created_at DESC LIMIT 100'
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiences', methods=['POST'])
def api_experiences_post():
    data = request.get_json(silent=True) or {}
    story = (data.get('story') or '').strip()
    if not story:
        return jsonify({'error': 'story is required'}), 400
    name     = (data.get('name')     or 'Anonymous').strip()[:80]
    role     = (data.get('role')     or 'app-user').strip()[:40]
    location = (data.get('location') or '').strip()[:120]
    exp_date = (data.get('exp_date') or '').strip()[:20]
    impact   = max(1, min(5, int(data.get('impact', 3))))
    enhanced = enhance_vocabulary(story)
    _valid_roles = {'app-user', 'local-witness', 'expert', 'researcher', 'skeptic'}
    if role not in _valid_roles:
        role = 'app-user'
    try:
        with _exp_db() as db:
            cur = db.execute(
                'INSERT INTO experiences (name, role, location, exp_date, story, enhanced, impact) VALUES (?,?,?,?,?,?,?)',
                (name, role, location, exp_date, story, enhanced, impact)
            )
            db.commit()
            new_id = cur.lastrowid
        return jsonify({'id': new_id, 'enhanced': enhanced}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reword', methods=['POST'])
def api_reword():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text is required'}), 400
    return jsonify({'enhanced': enhance_vocabulary(text)})


# ---------------------------------------------------------------------------
# Traffic / Weather Camera APIs
# ---------------------------------------------------------------------------

# Caltrans CCTV district endpoints (free, no API key)
_CALTRANS_DISTRICTS = {
    1: 'D01', 2: 'D02', 3: 'D03', 4: 'D04', 5: 'D05',
    6: 'D06', 7: 'D07', 8: 'D08', 10: 'D10', 11: 'D11', 12: 'D12',
}

# Map California lat ranges to likely Caltrans districts
def _ca_districts_for_bbox(min_lat, max_lat):
    """Return likely Caltrans district numbers for a latitude range."""
    dists = []
    if max_lat >= 40.0:                dists.append(1)   # Eureka / NorCal
    if max_lat >= 39.5 and min_lat <= 41.0: dists.append(2)   # Redding
    if max_lat >= 37.5 and min_lat <= 40.5: dists.append(3)   # Sacramento
    if max_lat >= 37.0 and min_lat <= 38.5: dists.append(4)   # Bay Area
    if max_lat >= 34.5 and min_lat <= 37.5: dists.append(5)   # SLO / Central Coast
    if max_lat >= 35.5 and min_lat <= 38.5: dists.append(6)   # Fresno / Central Valley
    if max_lat >= 33.5 and min_lat <= 35.0: dists.append(7)   # LA
    if max_lat >= 33.5 and min_lat <= 35.5: dists.append(8)   # San Bernardino
    if max_lat >= 36.0 and min_lat <= 38.0: dists.append(10)  # Stockton
    if max_lat >= 32.5 and min_lat <= 33.5: dists.append(11)  # San Diego
    if max_lat >= 33.5 and min_lat <= 34.5: dists.append(12)  # Orange County
    return dists


def _fetch_wsdot_cameras(lat, lon, radius_km):
    """Query WSDOT ArcGIS FeatureServer for live traffic cameras.
    Returns list of camera dicts.  Free, no API key needed.
    1,647 cameras across Washington State.
    """
    import math
    cameras = []
    # Build a bounding box in lat/lon (EPSG:4326)
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    bbox = f'{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}'
    url = (
        'https://data.wsdot.wa.gov/arcgis/rest/services/'
        'TravelInformation/TravelInfoCamerasWeather/FeatureServer/0/query'
    )
    params = {
        'where': '1=1',
        'outFields': 'CameraTitle,ImageURL,CompassDirection',
        'f': 'json',
        'outSR': '4326',
        'geometry': bbox,
        'geometryType': 'esriGeometryEnvelope',
        'inSR': '4326',
        'resultRecordCount': 200,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.ok:
            data = resp.json()
            for feat in data.get('features', []):
                attr = feat.get('attributes', {})
                geom = feat.get('geometry', {})
                img_url = attr.get('ImageURL', '')
                if not img_url:
                    continue
                clat = geom.get('y', 0)
                clon = geom.get('x', 0)
                dist = math.sqrt((lat - clat)**2 + (lon - clon)**2) * 111
                if dist <= radius_km:
                    direction = attr.get('CompassDirection', '')
                    dir_str = f' ({direction})' if direction and direction != 'B' else ''
                    cameras.append({
                        'name': (attr.get('CameraTitle', 'WSDOT Camera') + dir_str),
                        'image_url': img_url,
                        'description': attr.get('CameraTitle', ''),
                        'location': f'{clat},{clon}',
                        'source': 'WSDOT',
                        'distance_km': round(dist, 1),
                    })
    except Exception as e:
        logger.debug('WSDOT ArcGIS camera query failed: %s', e)
    return cameras


def _fetch_caltrans_cameras(lat, lon, radius_km):
    """Query Caltrans CCTV JSON feeds for live traffic cameras.
    Returns list of camera dicts.  Free, no API key needed.
    Each district has its own endpoint with ~200-800 cameras.
    """
    import math
    cameras = []
    dlat = radius_km / 111.0
    min_lat, max_lat = lat - dlat, lat + dlat
    districts = _ca_districts_for_bbox(min_lat, max_lat)
    if not districts:
        return cameras

    for d_num in districts[:3]:  # limit to 3 districts to stay fast
        d_code = _CALTRANS_DISTRICTS.get(d_num)
        if not d_code:
            continue
        url = f'https://cwwp2.dot.ca.gov/data/d{d_num}/cctv/cctvStatus{d_code}.json'
        try:
            resp = requests.get(url, timeout=8)
            if not resp.ok:
                continue
            data = resp.json()
            cams_list = data.get('data', []) if isinstance(data, dict) else data
            for cam in cams_list:
                cc = cam.get('cctv', {})
                loc = cc.get('location', {})
                clat_s = loc.get('latitude')
                clon_s = loc.get('longitude')
                if not clat_s or not clon_s:
                    continue
                if cc.get('inService') == 'false':
                    continue
                clat = float(clat_s)
                clon = float(clon_s)
                dist = math.sqrt((lat - clat)**2 + (lon - clon)**2) * 111
                if dist > radius_km:
                    continue
                img_data = cc.get('imageData', {})
                img_url = ''
                if isinstance(img_data, dict):
                    static = img_data.get('static', {})
                    if isinstance(static, dict):
                        img_url = static.get('currentImageURL', '')
                if not img_url:
                    continue
                cameras.append({
                    'name': loc.get('locationName', 'Caltrans Camera'),
                    'image_url': img_url,
                    'description': f"{loc.get('route', '')} near {loc.get('nearbyPlace', '')}".strip(),
                    'location': f'{clat},{clon}',
                    'source': 'Caltrans',
                    'distance_km': round(dist, 1),
                })
        except Exception as e:
            logger.debug('Caltrans D%s camera query failed: %s', d_num, e)
    return cameras


@app.route('/api/traffic-cams')
def api_traffic_cams():
    """Find public traffic/weather cameras near a location.
    Sources:
      - WSDOT ArcGIS (1,647 cameras across WA) — free, no key
      - Caltrans CCTV (3,000+ cameras across CA) — free, no key
    """
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    location = request.args.get('location', '').strip()
    radius = request.args.get('radius', default=50, type=int)  # km

    if lat is None or lon is None:
        if not location:
            return jsonify({'error': 'Provide "location" or "lat"+"lon"'}), 400
        geo = geocode(location)
        if not geo:
            return jsonify({'error': 'location not found'}), 404
        lat, lon, _, _ = geo

    cameras = []

    # Determine which sources to query based on approximate US region
    # WSDOT: Washington State (lat ~45.5-49, lon ~-125 to -117)
    if 45.0 <= lat <= 49.5 and -125.5 <= lon <= -116.5:
        cameras.extend(_fetch_wsdot_cameras(lat, lon, radius))

    # Caltrans: California (lat ~32-42, lon ~-125 to -114)
    if 32.0 <= lat <= 42.5 and -125.5 <= lon <= -114.0:
        cameras.extend(_fetch_caltrans_cameras(lat, lon, radius))

    # Sort by distance
    cameras.sort(key=lambda c: c['distance_km'])

    return jsonify({
        'lat': lat, 'lon': lon,
        'radius_km': radius,
        'cameras': cameras[:20],
    })


@app.route('/api/cam-proxy')
def api_cam_proxy():
    """Proxy a camera image to bypass CORS restrictions.
    Only allows image content types for safety.
    """
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'url parameter required'}), 400
    # Basic URL validation
    if not url.startswith('http://') and not url.startswith('https://'):
        return jsonify({'error': 'invalid url'}), 400

    try:
        resp = requests.get(url, timeout=10, stream=True, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; FloodWise/1.0)',
        })
        resp.raise_for_status()
        ct = resp.headers.get('Content-Type', '')
        if 'image' not in ct and 'octet-stream' not in ct:
            return jsonify({'error': 'not an image'}), 400
        from io import BytesIO
        img_data = BytesIO(resp.content)
        response = make_response(img_data.getvalue())
        response.headers['Content-Type'] = ct or 'image/jpeg'
        response.headers['Cache-Control'] = 'public, max-age=30'
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/flood-cam')
def page_flood_cam():
    return render_template('flood_cam.html')


@app.route('/credits')
def page_credits():
    return render_template('credits.html')


@app.route('/timeline')
def page_timeline():
    return render_template('timeline.html')


@app.route('/ar-flood')
def page_ar_flood():
    return render_template('ar_flood.html')


# ---------------------------------------------------------------------------
# Hyperlocal Flood Prediction API
# ---------------------------------------------------------------------------

@app.route('/api/hyperlocal')
def api_hyperlocal():
    """9-point micro-elevation grid around a location with per-cell flood risk.
    Returns a 3×3 grid (center = target) with elevation, slope, drainage score,
    soil saturation proxy, and per-cell risk level.
    """
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    location = request.args.get('location', '').strip()

    if lat is None or lon is None:
        if not location:
            return jsonify({'error': 'Provide "location" or "lat"+"lon"'}), 400
        geo = geocode(location)
        if not geo:
            return jsonify({'error': 'location not found'}), 404
        lat, lon, _, _ = geo

    step = 0.002  # ~200 m spacing
    grid_pts = []
    for r in range(-1, 2):
        for c in range(-1, 2):
            grid_pts.append({'lat': lat + r * step, 'lon': lon + c * step, 'row': r + 1, 'col': c + 1})

    # Batch elevation lookup
    lats_str = ','.join(f'{p["lat"]:.5f}' for p in grid_pts)
    lons_str = ','.join(f'{p["lon"]:.5f}' for p in grid_pts)
    elevations = [None] * len(grid_pts)
    try:
        resp = requests.get(
            'https://api.open-meteo.com/v1/elevation',
            params={'latitude': lats_str, 'longitude': lons_str},
            timeout=8,
        )
        resp.raise_for_status()
        elevations = resp.json().get('elevation', [None] * len(grid_pts))
    except Exception:
        logger.debug('Hyperlocal elevation fetch failed')

    # 7-day cumulative precipitation for soil saturation proxy
    cumulative_precip = 0.0
    try:
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        week_ago = (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d')
        w_resp = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': lat, 'longitude': lon,
            'daily': 'precipitation_sum',
            'start_date': week_ago, 'end_date': today_str,
            'timezone': 'auto',
        }, timeout=8)
        w_resp.raise_for_status()
        precips = w_resp.json().get('daily', {}).get('precipitation_sum', [])
        cumulative_precip = sum(float(v) for v in precips if v is not None)
    except Exception:
        logger.debug('Hyperlocal precip fetch failed')

    # Soil saturation proxy: 0–1 based on 7-day cumulative (>80mm → saturated)
    soil_saturation = min(1.0, cumulative_precip / 80.0)

    # Calculate per-cell metrics
    center_elev = elevations[4] if len(elevations) > 4 and elevations[4] is not None else 0
    cells = []
    for i, pt in enumerate(grid_pts):
        elev = elevations[i] if i < len(elevations) and elevations[i] is not None else center_elev
        # Slope: difference from center, normalized
        slope = (elev - center_elev) / (step * 111000)  # rise / run in meters
        # Drainage capacity: higher slope = better drainage (0–1)
        drainage = min(1.0, abs(slope) * 50)
        # Cell risk: low elevation + high saturation + poor drainage = high risk
        elev_factor = max(0, 1.0 - elev / 100.0) * 30  # max 30 pts
        sat_factor = soil_saturation * 25  # max 25 pts
        drain_factor = (1.0 - drainage) * 20  # max 20 pts
        # Below center = water flows here
        if elev < center_elev:
            low_factor = min(15, (center_elev - elev) * 2)
        else:
            low_factor = 0
        cell_risk = min(100, elev_factor + sat_factor + drain_factor + low_factor)
        cells.append({
            'row': pt['row'], 'col': pt['col'],
            'lat': round(pt['lat'], 5), 'lon': round(pt['lon'], 5),
            'elevation_m': round(elev, 1) if elev is not None else None,
            'slope': round(slope, 5),
            'drainage_capacity': round(drainage, 2),
            'cell_risk_score': round(cell_risk, 1),
            'cell_risk_level': 'high' if cell_risk >= 60 else ('moderate' if cell_risk >= 30 else 'low'),
            'is_center': i == 4,
        })

    return jsonify({
        'lat': lat, 'lon': lon,
        'grid_spacing_m': round(step * 111000),
        'soil_saturation_proxy': round(soil_saturation, 2),
        'cumulative_7day_precip_mm': round(cumulative_precip, 1),
        'grid': cells,
    })


# ---------------------------------------------------------------------------
# Alert System
# ---------------------------------------------------------------------------

_ALERT_DB = os.path.join(os.environ.get('HOME', '/tmp'), 'floodwise_alerts.db')


def _alert_db():
    conn = _sqlite3.connect(_ALERT_DB)
    conn.row_factory = _sqlite3.Row
    return conn


def _init_alert_db():
    with _alert_db() as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS alert_subscriptions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                location     TEXT    NOT NULL,
                lat          REAL,
                lon          REAL,
                threshold    INTEGER DEFAULT 50,
                created_at   TEXT    DEFAULT (datetime("now")),
                last_checked TEXT    DEFAULT "",
                last_score   INTEGER DEFAULT 0,
                active       INTEGER DEFAULT 1
            )
        ''')
        db.commit()


try:
    _init_alert_db()
except Exception as _e:
    logger.warning('Could not init alert DB: %s', _e)


@app.route('/api/alert-subscribe', methods=['POST'])
def api_alert_subscribe():
    """Subscribe to flood alerts for a location.
    Body: {location, threshold (optional, default 50)}
    """
    data = request.get_json(silent=True) or {}
    location = (data.get('location') or '').strip()
    if not location:
        return jsonify({'error': 'location is required'}), 400
    threshold = max(10, min(100, int(data.get('threshold', 50))))

    # Geocode to get lat/lon
    geo = geocode(location)
    lat, lon = (geo[0], geo[1]) if geo else (None, None)

    try:
        with _alert_db() as db:
            cur = db.execute(
                'INSERT INTO alert_subscriptions (location, lat, lon, threshold) VALUES (?,?,?,?)',
                (location, lat, lon, threshold)
            )
            db.commit()
            return jsonify({'id': cur.lastrowid, 'location': location, 'threshold': threshold}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/alert-check', methods=['GET'])
def api_alert_check():
    """Check flood risk for all active subscriptions (or a specific one by ?id=).
    Returns list of alerts that exceed their threshold.
    """
    sub_id = request.args.get('id', type=int)
    try:
        with _alert_db() as db:
            if sub_id:
                rows = db.execute('SELECT * FROM alert_subscriptions WHERE id=? AND active=1', (sub_id,)).fetchall()
            else:
                rows = db.execute('SELECT * FROM alert_subscriptions WHERE active=1 ORDER BY created_at DESC LIMIT 20').fetchall()

        alerts = []
        for row in rows:
            r = dict(row)
            lat, lon = r.get('lat'), r.get('lon')
            if lat is None or lon is None:
                geo = geocode(r['location'])
                if geo:
                    lat, lon = geo[0], geo[1]
                else:
                    continue

            # Quick risk check using current forecast
            try:
                resp = requests.get('https://api.open-meteo.com/v1/forecast', params={
                    'latitude': lat, 'longitude': lon,
                    'daily': 'precipitation_sum',
                    'forecast_days': 1,
                    'timezone': 'auto',
                }, timeout=8)
                resp.raise_for_status()
                today_precip = (resp.json().get('daily', {}).get('precipitation_sum', [0]) or [0])[0] or 0
            except Exception:
                today_precip = 0

            # Simple risk estimate
            score = 0
            if today_precip > 50:
                score += 30
            elif today_precip > 20:
                score += 20
            elif today_precip > 10:
                score += 12
            elif today_precip > 5:
                score += 7
            elev = get_elevation(lat, lon)
            if elev is not None:
                if elev < 5:
                    score += 20
                elif elev < 15:
                    score += 14
                elif elev < 30:
                    score += 8

            triggered = score >= r['threshold']

            # Update last check
            try:
                with _alert_db() as db:
                    db.execute('UPDATE alert_subscriptions SET last_checked=datetime("now"), last_score=? WHERE id=?',
                               (score, r['id']))
                    db.commit()
            except Exception:
                pass

            alerts.append({
                'id': r['id'],
                'location': r['location'],
                'threshold': r['threshold'],
                'current_score': score,
                'today_precip_mm': round(today_precip, 1),
                'triggered': triggered,
                'message': f'⚠️ Flood risk {score}/100 exceeds your threshold of {r["threshold"]} for {r["location"]}!' if triggered else f'✅ Flood risk {score}/100 is below your threshold of {r["threshold"]} for {r["location"]}.',
            })

        return jsonify({'alerts': alerts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/alert-unsubscribe', methods=['POST'])
def api_alert_unsubscribe():
    """Deactivate an alert subscription."""
    data = request.get_json(silent=True) or {}
    sub_id = data.get('id')
    if not sub_id:
        return jsonify({'error': 'id is required'}), 400
    try:
        with _alert_db() as db:
            db.execute('UPDATE alert_subscriptions SET active=0 WHERE id=?', (sub_id,))
            db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500