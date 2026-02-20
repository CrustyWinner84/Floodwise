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

    # 2) Try Open-Elevation (public) as before
    try:
        url = 'https://api.open-elevation.com/api/v1/lookup'
        params = {'locations': f'{lat},{lon}'}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = data.get('results') or []
        if results:
            elev = results[0].get('elevation')
            if elev is not None:
                return float(elev)
    except Exception:
        logger.debug('Open-Elevation lookup failed for %s,%s', lat, lon)

    # 3) Fallback: try Open-Meteo geocoding elevation via their geocoding API (best-effort)
    try:
        g_url = 'https://geocoding-api.open-meteo.com/v1/search'
        g_params = {'name': f'{lat},{lon}', 'count': 1}
        g_resp = requests.get(g_url, params=g_params, timeout=10)
        g_resp.raise_for_status()
        g_data = g_resp.json()
        results = g_data.get('results') or []
        if results:
            elev = results[0].get('elevation')
            if elev is not None:
                return float(elev)
    except Exception:
        logger.debug('Open-Meteo geocode-elevation fallback failed for %s,%s', lat, lon)

    # If all else fails, return None to indicate unknown elevation
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

    # Try the original location
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
        risk_score = 20  # 0-100 scale
        
        # Very simple mock: if near certain latitudes/longitudes, increase risk
        if (lat > 40 and lat < 52) and (lon > -10 and lon < 40):  # EU flood-prone areas
            risk_level = 'moderate'
            risk_score = 45
        if (lat > 25 and lat < 35) and (lon > 70 and lon < 90):  # South Asia monsoon zone
            risk_level = 'high'
            risk_score = 70
        
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


def calculate_flood_risk_for_date(lat: float, lon: float, date_str: str, daily_data: dict, elevation_m=None):
    """Calculate flood risk for a specific date considering precipitation and location."""
    try:
        # Find the index for this date
        times = daily_data.get('time', [])
        precip = daily_data.get('precipitation_sum', [])
        
        if date_str not in times:
            return None
        
        date_idx = times.index(date_str)
        precip_today = precip[date_idx] if date_idx < len(precip) else 0
        
        # Base risk from location
        base_risk = get_flood_risk(lat, lon)
        base_score = base_risk.get('risk_score', 20)
        
        # Calculate cumulative precipitation (last 7 days up to this date)
        window_start = max(0, date_idx - 6)
        cumulative_precip = sum(precip[window_start:date_idx+1])
        
        # Check water body proximity
        near_water, water_name = is_near_water_body(lat, lon)
        
        # Calculate risk increase from precipitation
        precip_risk_factor = 0
        if precip_today > 50:  # Heavy rain
            precip_risk_factor = 30
        elif precip_today > 20:  # Moderate rain
            precip_risk_factor = 15
        elif precip_today > 5:  # Light rain
            precip_risk_factor = 5
        
        # Cumulative effect (7-day rainfall)
        cumulative_risk_factor = 0
        if cumulative_precip > 150:  # Very wet week
            cumulative_risk_factor = 25
        elif cumulative_precip > 80:  # Wet week
            cumulative_risk_factor = 15
        elif cumulative_precip > 40:  # Moderate week
            cumulative_risk_factor = 5
        
        # Water body proximity (25% weightage = 25 points)
        water_proximity_risk = 25 if near_water else 0

        # Elevation effect: lower elevations increase flood risk
        elevation_risk = 0
        try:
            if elevation_m is not None:
                e = float(elevation_m)
                if e < 5:
                    elevation_risk = 15
                elif e < 20:
                    elevation_risk = 10
                elif e < 50:
                    elevation_risk = 5
                else:
                    elevation_risk = 0
        except Exception:
            elevation_risk = 0
        
        # Calculate final risk score
        final_score = min(100, base_score + precip_risk_factor + cumulative_risk_factor + water_proximity_risk + elevation_risk)
        
        # Determine risk level
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
            },
            'note': 'Flood risk calculated from precipitation patterns and location factors. For real-time forecasts, consult local authorities or Google Flood Hub.'
        }
    except Exception as e:
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
        
        # Calculate flood risk for this date (include elevation)
        flood_risk_that_day = calculate_flood_risk_for_date(lat, lon, date_str, daily, elevation_m=elevation)
        
        if not flood_risk_that_day:
            return jsonify({'error': 'could not calculate flood risk for this date'}), 500
        
        return jsonify({
            'location': display_name,
            'latitude': lat,
            'longitude': lon,
            'elevation_m': elevation,
            'date': date_str,
            'weather': weather_that_day,
            'flood_risk': flood_risk_that_day
        })
    
    except requests.HTTPError as e:
        logger.warning('HTTP error in date-based flood risk: %s', e)
        return jsonify({'error': 'service error', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Unexpected error in date-based flood risk')
        return jsonify({'error': 'flood risk calculation failed', 'detail': str(e)}), 500

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
