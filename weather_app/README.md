# Weather & Environmental Data App (free APIs)

Flask app that takes a location (city, address, or landmark), geocodes it using OpenStreetMap Nominatim or Open-Meteo, and fetches:
- **Current weather** from Open-Meteo
- **Historical weather data** from Open-Meteo Archive API
- **Flood risk assessment** (simple heuristic; can be extended with Google Flood Hub API)

## Quick Start

1. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the server:

```bash
python app.py
```

Open http://127.0.0.1:5000 in your browser.

3. Or run CLI mode:

```bash
python app.py --cli
```

## Configuration

- `--host` (default: 0.0.0.0)
- `--port` (default: 5000)
- `--debug` (default: off)

Or use environment variables:
- `HOST=127.0.0.1 PORT=8080 python app.py`

## API Endpoints

### 1. **Current Weather** `/api/weather/current`

Get current weather for a location.

```bash
curl 'http://127.0.0.1:5000/api/weather/current?location=London'
```

Query parameters:
- `location` or `q`: place name, address, or landmark (required)

Response:
```json
{
  "location": "London, United Kingdom",
  "latitude": 51.50853,
  "longitude": -0.12574,
  "current_weather": {
    "temperature": 9.0,
    "windspeed": 15.1,
    "winddirection": 258,
    "weathercode": 80,
    "time": "2026-02-15T19:15"
  }
}
```

### 2. **Historical Weather** `/api/weather/historical`

Fetch historical weather for a date range.

```bash
curl 'http://127.0.0.1:5000/api/weather/historical?location=Tokyo&start_date=2026-01-15&end_date=2026-02-15'
```

Query parameters:
- `location` or `q`: place name or address (required)
- `start_date`: YYYY-MM-DD (default: 30 days ago)
- `end_date`: YYYY-MM-DD (default: today)

Response includes daily max/min temps, precipitation, and wind speed.

```json
{
  "location_name": "Tokyo, Japan",
  "daily": {
    "time": ["2026-01-15", "2026-01-16", ...],
    "temperature_2m_max": [10.6, 10.7, ...],
    "temperature_2m_min": [0.6, 2.3, ...],
    "precipitation_sum": [0.0, 0.0, ...],
    "windspeed_10m_max": [10.3, 15.0, ...]
  }
}
```

### 3. **Flood Risk** `/api/flood-risk`

Get a flood risk assessment for a location.

```bash
curl 'http://127.0.0.1:5000/api/flood-risk?location=Venice'
```

Query parameters:
- `location` or `q`: place name or address (required)

Response:
```json
{
  "location_name": "Venice, Italy",
  "latitude": 45.43713,
  "longitude": 12.33265,
  "risk_level": "moderate",
  "risk_score": 45,
  "note": "This is a simplified flood risk estimate. For accurate flood forecasts, consult local authorities or Google Flood Hub."
}
```

**Risk levels**: `low` (0-30), `moderate` (30-60), `high` (60-100)

### 4. **Flood Risk by Date** `/api/flood-risk-date`

Get detailed flood risk analysis for a specific date, including weather conditions, precipitation, water body proximity, and cumulative 7-day rainfall patterns.

```bash
curl 'http://127.0.0.1:5000/api/flood-risk-date?location=London&date=2025-06-15'
```

Query parameters:
- `location` or `q`: place name or address (required)
- `date`: YYYY-MM-DD (required) — must be within historical data range

Response:
```json
{
  "location": "London, United Kingdom",
  "latitude": 51.50853,
  "longitude": -0.12574,
  "date": "2025-06-15",
  "weather": {
    "date": "2025-06-15",
    "temperature_max_c": 23.5,
    "temperature_min_c": 13.2,
    "precipitation_mm": 0.2,
    "windspeed_max_kmh": 16.2
  },
  "flood_risk": {
    "date": "2025-06-15",
    "risk_level": "moderate",
    "risk_score": 45.0,
    "precipitation_mm": 0.2,
    "cumulative_7day_precip_mm": 6.6,
    "near_water_body": false,
    "water_body_name": null,
    "factors": {
      "base_location_risk": 45,
      "precipitation_today_risk": 0,
      "cumulative_week_risk": 0,
      "water_proximity_multiplier": 1.0
    }
  }
}
```

**Flood risk calculation includes**:
- **Base location risk**: Geographic area prone to flooding (e.g., EU flood zones, monsoon regions, near water bodies)
- **Precipitation today**: +5 points for light rain, +15 for moderate, +30 for heavy (>50mm)
- **7-day cumulative precipitation**: +5 for moderate week (40-80mm), +15 for wet week (80-150mm), +25 for very wet (>150mm)
- **Water body proximity**: 1.3x multiplier if near known water bodies (Venice, Amsterdam, New Orleans, Miami, Bangkok, etc.)

### 5. **All Data** `/api/all`

Get current + historical + flood data in a single request.

```bash
curl 'http://127.0.0.1:5000/api/all?location=Paris&start_date=2026-01-15&end_date=2026-02-15'
```

### 6. **Health Check** `/api/health`

```bash
curl 'http://127.0.0.1:5000/health'
```

Response: `{"status": "ok"}`

## Geocoding

The app uses a fallback geocoding strategy:

1. **Primary**: OpenStreetMap Nominatim (requires proper User-Agent, can be slow)
2. **Fallback**: Open-Meteo Geocoding API (faster, no API key needed)

Handles:
- Cities: "London", "Paris", "Tokyo"
- Countries: "France", "Japan"
- Addresses: "1600 Amphitheatre Parkway, Mountain View"
- Landmarks: "Eiffel Tower" → tries "Paris", "Big Ben" → tries "London", etc.

Set `NOMINATIM_USER_AGENT` env var to customize the user agent:

```bash
NOMINATIM_USER_AGENT="my-app/1.0 (contact: me@example.com)" python app.py
```

## Free APIs Used

| Service | Purpose | Limits |
|---------|---------|--------|
| OpenStreetMap Nominatim | Geocoding | ~1 req/sec, requires User-Agent |
| Open-Meteo | Current weather, Archive | Free tier: no key needed, ~10k req/day |
| Open-Meteo Geocoding | Fallback geocoding | ~1k req/day free |

## Files

- `app.py`: Flask server with all endpoints
- `templates/index.html`: Web UI with tabs (current, historical, flood)
- `requirements.txt`: Python dependencies

## Notes

- The flood risk is a simplified heuristic based on latitude/longitude. For production, integrate Google Flood Hub API or local flood forecast services.
- The app respects rate limits of free APIs. For high-volume use, consider caching or premium APIs.
- Historical data availability depends on Open-Meteo's archive (typically decades of data).
