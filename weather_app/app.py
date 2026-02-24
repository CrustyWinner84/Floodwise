from flask import Flask, request, jsonify, render_template, make_response
import requests
import re
import logging
import os
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


def get_fema_flood_history(lat: float, lon: float):
    """Query OpenFEMA API for historical flood disaster declarations near a location.
    Completely free, no API key required.
    Returns up to 5 most recent flood events and a risk score (0-35).
    Results are cached per-state for 24 hours to minimise Azure → FEMA round-trips.
    """
    try:
        # Reverse geocode using BigDataCloud (free, no key, fast, no Azure blocks)
        geo_url = 'https://api.bigdatacloud.net/data/reverse-geocode-client'
        params = {'latitude': lat, 'longitude': lon, 'localityLanguage': 'en'}
        resp = requests.get(geo_url, params=params, timeout=5)
        resp.raise_for_status()
        geo_data = resp.json()
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

        # --- Fetch from OpenFEMA ---
        fema_url = 'https://www.fema.gov/api/open/v2/disasterDeclarationsSummaries'
        fema_params = {
            '$filter': f"state eq '{state_abbrev}' and incidentType eq 'Flood'",
            '$orderby': 'declarationDate desc',
            '$top': 10,
            '$select': 'disasterNumber,declarationDate,declarationTitle,incidentType,state,designatedArea,incidentBeginDate,incidentEndDate'
        }
        fema_headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
        }
        fema_resp = requests.get(fema_url, params=fema_params, headers=fema_headers, timeout=4)
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
        logger.warning('FEMA flood history lookup failed: %s', e)
        return {'available': False, 'events': [], 'historical_risk_score': 0,
                'note': 'FEMA flood history data temporarily unavailable.'}


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


def is_near_water_body(lat: float, lon: float):
    """Simple heuristic to detect if location is near a water body.
    In production, use OpenStreetMap data or a dedicated API.
    """
    # Known water bodies (lakes, rivers, coastal areas) - simplified
    water_zones = [
        {'name': 'Venice', 'lat': 45.4, 'lon': 12.3, 'radius': 0.5},
        {'name': 'Amsterdam', 'lat': 52.37, 'lon': 4.9, 'radius': 0.5},
        {'name': 'New Orleans', 'lat': 29.95, 'lon': -90.07, 'radius': 1.0},
        {'name': 'Miami', 'lat': 25.76, 'lon': -80.19, 'radius': 1.0},
        {'name': 'Bangkok', 'lat': 13.73, 'lon': 100.50, 'radius': 1.0},
        {'name': 'Snohomish', 'lat': 47.91, 'lon': -122.10, 'radius': 0.8},
    ]
    
    for zone in water_zones:
        dist = ((lat - zone['lat'])**2 + (lon - zone['lon'])**2)**0.5
        if dist < zone['radius']:
            return True, zone['name']
    
    return False, None


def calculate_flood_risk_for_date(lat: float, lon: float, date_str: str, daily_data: dict,
                                   elevation_m=None, fema_data=None, usgs_data=None):
    """Calculate flood risk for a specific date.
    Factors (max pts / % of 220 uncapped total):
      Base location risk   : 50 pts (22.7%)
      Precipitation today  : 30 pts (13.6%)
      7-day cumulative     : 25 pts (11.4%)
      Water body proximity : 25 pts (11.4%)
      Elevation            : 20 pts  (9.1%)
      FEMA historical      : 35 pts (15.9%)
      USGS live gauge      : 35 pts (15.9%)
      Total uncapped: 220  -> capped at 100
    """
    try:
        times = daily_data.get('time', [])
        precip = daily_data.get('precipitation_sum', [])

        if date_str not in times:
            return None

        date_idx = times.index(date_str)
        precip_today = precip[date_idx] if date_idx < len(precip) else 0

        # Base risk from location (max 50)
        base_risk = get_flood_risk(lat, lon)
        base_score = base_risk.get('risk_score', 15)

        # 7-day cumulative precipitation
        window_start = max(0, date_idx - 6)
        cumulative_precip = sum(precip[window_start:date_idx + 1])

        # Water body proximity (max 25)
        near_water, water_name = is_near_water_body(lat, lon)

        # Today's precipitation (max 30)
        precip_risk_factor = 0
        if precip_today > 50:
            precip_risk_factor = 30
        elif precip_today > 20:
            precip_risk_factor = 15
        elif precip_today > 5:
            precip_risk_factor = 5

        # 7-day cumulative (max 25)
        cumulative_risk_factor = 0
        if cumulative_precip > 150:
            cumulative_risk_factor = 25
        elif cumulative_precip > 80:
            cumulative_risk_factor = 15
        elif cumulative_precip > 40:
            cumulative_risk_factor = 5

        # Water proximity (max 25)
        water_proximity_risk = 25 if near_water else 0

        # Elevation (max 20)
        elevation_risk = 0
        try:
            if elevation_m is not None:
                e = float(elevation_m)
                if e < 5:
                    elevation_risk = 20
                elif e < 20:
                    elevation_risk = 13
                elif e < 50:
                    elevation_risk = 7
        except Exception:
            pass

        # FEMA historical flood declarations (max 35)
        fema_risk = (fema_data or {}).get('historical_risk_score', 0)

        # USGS live stream gauge (max 35)
        usgs_risk = (usgs_data or {}).get('gauge_risk_score', 0)

        # Final score capped at 100
        final_score = min(100, base_score + precip_risk_factor + cumulative_risk_factor
                         + water_proximity_risk + elevation_risk + fema_risk + usgs_risk)

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
            'near_water_body': near_water,
            'water_body_name': water_name,
            'elevation_m': elevation_m,
            'factors': {
                'base_location_risk': base_score,
                'precipitation_today_risk': precip_risk_factor,
                'cumulative_week_risk': cumulative_risk_factor,
                'water_proximity_risk': water_proximity_risk,
                'elevation_risk': elevation_risk,
                'fema_historical_risk': fema_risk,
                'usgs_gauge_risk': usgs_risk,
            },
            'note': 'Flood risk calculated from precipitation, location, FEMA historical events, and live USGS gauge data.'
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
        # Get historical weather data for a 30-day window around the target date
        start_date_obj = target_date - timedelta(days=15)
        end_date_obj = target_date + timedelta(days=15)
        
        start_date = start_date_obj.strftime('%Y-%m-%d')
        end_date = end_date_obj.strftime('%Y-%m-%d')
        
        historical_data = get_historical_weather(lat, lon, start_date, end_date)
        
        if not historical_data or 'daily' not in historical_data:
            return jsonify({'error': 'could not retrieve historical weather data for this date'}), 500
        
        daily = historical_data['daily']
        
        # Get weather info for the target date
        if 'time' not in daily or date_str not in daily['time']:
            return jsonify({'error': f'no weather data available for date {date_str}. Available dates in archive: check Open-Meteo historical data limits'}), 404
        
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
        with _TPE(max_workers=2) as _p:
            _ff = _p.submit(get_fema_flood_history, lat, lon)
            _fu = _p.submit(get_usgs_stream_gauge, lat, lon)
            try:
                fema_data = _ff.result(timeout=max(0.1, 5.0 - (_t2.monotonic() - _t2_0)))
            except Exception:
                fema_data = {'events': [], 'historical_risk_score': 0}
            try:
                usgs_data = _fu.result(timeout=max(0.1, 5.0 - (_t2.monotonic() - _t2_0)))
            except Exception:
                usgs_data = {'gauges': [], 'gauge_risk_score': 0}

        # Calculate flood risk for this date (include elevation + FEMA + USGS)
        flood_risk_that_day = calculate_flood_risk_for_date(
            lat, lon, date_str, daily,
            elevation_m=elevation,
            fema_data=fema_data,
            usgs_data=usgs_data
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
                                   elevation_m=None, fema_data=None, usgs_data=None):
    """Return a 14-day list of daily flood risk scores from forecast data.
    Uses a small fixed geographic base so precipitation variation drives day-to-day changes.
    """
    times     = forecast_daily.get('time', [])
    precip    = forecast_daily.get('precipitation_sum', [])
    precip_p  = forecast_daily.get('precipitation_probability_max', [])

    try:
        near_water, water_name = is_near_water_body(lat, lon)
    except Exception:
        near_water, water_name = False, ''

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

    water_risk = 12 if near_water else 0
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

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_fc   = pool.submit(get_forecast_weather, lat, lon, 16)
            f_fema = pool.submit(get_fema_flood_history, lat, lon)
            f_usgs = pool.submit(get_usgs_stream_gauge, lat, lon)
            # Forecast is mandatory — raises if it fails
            forecast_daily = f_fc.result(timeout=max(0.1, _BUDGET - (_t.monotonic() - _t0)))
            fema_data = _get_fc(f_fema, {'events': [], 'historical_risk_score': 0})
            usgs_data = _get_fc(f_usgs, {'gauges': [], 'gauge_risk_score': 0})

        daily_risks = calculate_flood_risk_forecast(lat, lon, forecast_daily,
                                                     elevation_m=elev,
                                                     fema_data=fema_data,
                                                     usgs_data=usgs_data)
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

    # Gather live weather context if we have coords
    ctx_lines = []
    forecast_daily = {}
    if lat is not None and lon is not None:
        try:
            forecast_daily = get_forecast_weather(lat, lon, 7)
            times  = forecast_daily.get('time', [])
            precip = forecast_daily.get('precipitation_sum', [])
            tmax   = forecast_daily.get('temperature_2m_max', [])
            tmin   = forecast_daily.get('temperature_2m_min', [])
            wcode  = forecast_daily.get('weathercode', [])
            WMO_SHORT = {0:'clear sky',1:'mainly clear',2:'partly cloudy',3:'overcast',
                         45:'fog',51:'light drizzle',53:'drizzle',55:'heavy drizzle',
                         61:'light rain',63:'rain',65:'heavy rain',71:'light snow',
                         73:'snow',75:'heavy snow',80:'showers',81:'moderate showers',
                         82:'heavy showers',95:'thunderstorm',96:'thunderstorm+hail'}
            for i, d in enumerate(times[:7]):
                wdesc = WMO_SHORT.get(int(wcode[i]) if i < len(wcode) and wcode[i] is not None else 0, '')
                pr    = precip[i] if i < len(precip) and precip[i] is not None else 0
                tx    = tmax[i]   if i < len(tmax)   and tmax[i]   is not None else '?'
                tn    = tmin[i]   if i < len(tmin)   and tmin[i]   is not None else '?'
                ctx_lines.append(f'{d}: {wdesc}, max {tx}°C, min {tn}°C, precip {pr:.1f}mm')
            ctx_lines.insert(0, f'Location: {loc_name or f"{lat:.3f},{lon:.3f}"}')
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
                      "and I'll check the 7-day forecast for you.")
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
    elif any(w in q_lower for w in ['rain','precip','shower','drizzle','wet','umbrella']):
        if no_loc:
            answer = "Try asking: **'Will it rain in Seattle this week?'** — I'll pull the live forecast!"
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

    # --- Generic fallback ---
    else:
        if not no_loc and ctx_lines:
            answer = (f"Here's a quick weather summary for **{place}**:\n{chr(10).join(ctx_lines[1:4])}\n\n"
                      "Ask me about rain, flooding, temperature, wind, snow, ski conditions, UV, or evacuation safety!")
        elif no_loc:
            answer = ("I'm your **FloodWise AI** weather assistant! 🌊\n\n"
                      "Just ask me about any place — no need to search first! Try:\n"
                      "• *Will it rain in Seattle this week?*\n"
                      "• *Is there flood risk in New Orleans?*\n"
                      "• *How cold will Denver be this week?*")
        else:
            answer = (f"I'm your FloodWise weather assistant for **{place}**! "
                      "Ask me about rain, flooding, temperature, wind, snow, ski conditions, UV, or evacuation safety.")

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

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_weather = pool.submit(get_weather, lat, lon)
        f_hist    = pool.submit(get_historical_weather, lat, lon, hist_start, hist_end)
        f_fema    = pool.submit(get_fema_flood_history, lat, lon)
        f_usgs    = pool.submit(get_usgs_stream_gauge, lat, lon)

        current_weather = _get(f_weather, None)
        historical_data = _get(f_hist,    None)
        fema_data       = _get(f_fema,    {'events': [], 'historical_risk_score': 0})
        usgs_data       = _get(f_usgs,    {'gauges': [], 'gauge_risk_score': 0})

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
