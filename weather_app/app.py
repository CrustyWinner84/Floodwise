from flask import Flask, request, jsonify, render_template, make_response
import requests
import logging
import os
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


def geocode(location: str):
    """Geocode a free-form location string to (lat, lon, display_name) using Nominatim."""
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
        """Try geocoding with a given query string using both services."""
        # Primary: Nominatim
        try:
            url = 'https://nominatim.openstreetmap.org/search'
            params = {'q': query_str, 'format': 'json', 'limit': 1}
            user_agent = os.environ.get('NOMINATIM_USER_AGENT', 'weather-app/1.0 (contact: example@example.com)')
            headers = {'User-Agent': user_agent}
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data:
                item = data[0]
                lat = float(item['lat'])
                lon = float(item['lon'])
                display = item.get('display_name')
                elev = get_elevation(lat, lon)
                return lat, lon, display, elev
        except requests.HTTPError as e:
            if e.response.status_code == 403:
                logger.warning('Nominatim blocked (403) — falling back to Open-Meteo for "%s"', query_str)
            else:
                logger.warning('Nominatim geocoding failed for "%s": %s', query_str, e)
        except Exception:
            logger.exception('Unexpected error calling Nominatim for "%s"', query_str)

        # Fallback: Open-Meteo geocoding API
        try:
            g_url = 'https://geocoding-api.open-meteo.com/v1/search'
            g_params = {'name': query_str, 'count': 1}
            g_resp = requests.get(g_url, params=g_params, timeout=15)
            g_resp.raise_for_status()
            g_data = g_resp.json()
            results = g_data.get('results') or []
            if results:
                item = results[0]
                display = item.get('name')
                if item.get('country'):
                    display = f"{display}, {item.get('country')}"
                lat = float(item['latitude'])
                lon = float(item['longitude'])
                elev = item.get('elevation')
                if elev is None:
                    elev = get_elevation(lat, lon)
                return lat, lon, display, elev
        except Exception:
            logger.exception('Fallback geocoding failed for "%s"', query_str)

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
    """Fetch current weather from Open-Meteo (no API key required)."""
    url = 'https://api.open-meteo.com/v1/forecast'
    params = {'latitude': lat, 'longitude': lon, 'current_weather': True}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get('current_weather')


def get_historical_weather(lat: float, lon: float, start_date: str, end_date: str):
    """Fetch historical weather from Open-Meteo Archive API.
    Dates should be in YYYY-MM-DD format.
    """
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
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # Return the daily data
    return {
        'location': {'latitude': lat, 'longitude': lon},
        'daily': data.get('daily', {}),
        'timezone': data.get('timezone')
    }


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


def get_fema_flood_history(lat: float, lon: float):
    """Query OpenFEMA API for historical flood disaster declarations near a location.
    Completely free, no API key required.
    Returns up to 5 most recent flood events and a risk score (0-35).
    """
    try:
        # Reverse geocode to get state name
        geo_url = 'https://nominatim.openstreetmap.org/reverse'
        params = {'lat': lat, 'lon': lon, 'format': 'json'}
        user_agent = os.environ.get('NOMINATIM_USER_AGENT', 'weather-app/1.0 (contact: example@example.com)')
        resp = requests.get(geo_url, params=params, headers={'User-Agent': user_agent}, timeout=10)
        resp.raise_for_status()
        addr = resp.json().get('address', {})
        state_name = addr.get('state', '')
        state_abbrev = STATE_ABBREVS.get(state_name, '')

        if not state_abbrev:
            return {
                'available': False,
                'note': 'FEMA historical data only available for US locations.',
                'events': [],
                'historical_risk_score': 0
            }

        # Query OpenFEMA for flood disasters in this state
        fema_url = 'https://www.fema.gov/api/open/v2/disasterDeclarationsSummaries'
        fema_params = {
            '$filter': f"state eq '{state_abbrev}' and incidentType eq 'Flood'",
            '$orderby': 'declarationDate desc',
            '$top': 10,
            '$select': 'disasterNumber,declarationDate,declarationTitle,incidentType,state,designatedArea,incidentBeginDate,incidentEndDate'
        }
        fema_resp = requests.get(fema_url, params=fema_params, timeout=15)
        fema_resp.raise_for_status()
        events = fema_resp.json().get('DisasterDeclarationsSummaries', [])

        # Count events in last 5 years for scoring
        cutoff = (datetime.utcnow() - timedelta(days=5 * 365)).strftime('%Y-%m-%d')
        recent = [e for e in events if (e.get('declarationDate') or '') >= cutoff]
        hist_score = min(35, len(recent) * 7)

        return {
            'available': True,
            'state': state_name,
            'state_abbrev': state_abbrev,
            'total_flood_declarations': len(events),
            'recent_5yr_count': len(recent),
            'historical_risk_score': hist_score,
            'events': events[:5]
        }
    except Exception as e:
        logger.debug('FEMA flood history lookup failed: %s', e)
        return {'available': False, 'events': [], 'historical_risk_score': 0, 'note': str(e)}


def get_usgs_stream_gauge(lat: float, lon: float):
    """Query USGS Water Services for nearby real-time stream gauge data.
    Completely free, no API key required. US only.
    Returns up to 3 nearby gauges and a live gauge risk score (0-35).
    """
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
        resp = requests.get(url, params=params, timeout=15)
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

        return {
            'available': True,
            'gauges': gauges,
            'gauge_risk_score': gauge_score
        }
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


@app.route('/')
def index():
    return render_template('index.html')


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
        
        # Fetch FEMA historical flood declarations and USGS live gauge (non-fatal)
        fema_data = get_fema_flood_history(lat, lon)
        usgs_data = get_usgs_stream_gauge(lat, lon)

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

    # 2–6. Compute date window, then fetch weather / history / FEMA / USGS in parallel.
    target_date = datetime.strptime(date_str, '%Y-%m-%d')
    hist_start  = (target_date - timedelta(days=15)).strftime('%Y-%m-%d')
    hist_end    = (target_date + timedelta(days=15)).strftime('%Y-%m-%d')

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_weather = pool.submit(get_weather, lat, lon)
        f_hist    = pool.submit(get_historical_weather, lat, lon, hist_start, hist_end)
        f_fema    = pool.submit(get_fema_flood_history, lat, lon)
        f_usgs    = pool.submit(get_usgs_stream_gauge, lat, lon)

        try:
            current_weather = f_weather.result()
        except Exception:
            current_weather = None

        try:
            historical_data = f_hist.result()
        except Exception:
            historical_data = None

        try:
            fema_data = f_fema.result()
        except Exception:
            fema_data = {'events': [], 'historical_risk_score': 0}

        try:
            usgs_data = f_usgs.result()
        except Exception:
            usgs_data = {'gauges': [], 'gauge_risk_score': 0}

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
