# 🚀 Quick Reference - Flood Risk by Date Feature

## Try It Now

### Via Browser (Easiest)
1. Open: `http://127.0.0.1:5003`
2. Click: **"Flood Risk by Date"** tab
3. Enter: Location (e.g., "Venice" or "4124 170th PL SE, Bothell, WA")
4. Pick: Date (e.g., "2024-11-10")
5. Click: **"Analyze Flood Risk for Date"**
6. View: Weather + Flood Risk with breakdown!

### Via Command Line

```bash
# Venice on a rainy day
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=Venice&date=2024-11-10'

# Bothell address
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=4124%20170th%20PL%20SE%2C%20Bothell%2C%20WA&date=2025-08-15'

# Bangkok during monsoon
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=Bangkok&date=2024-09-15'

# Any city, any date
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=London&date=2025-06-15'
```

---

## What You Get

### Response Example
```json
{
  "location": "Venice, Italy",
  "date": "2024-11-10",
  
  "weather": {
    "temperature_max_c": 15.8,
    "temperature_min_c": 4.2,
    "precipitation_mm": 0.0,
    "windspeed_max_kmh": 12.2
  },
  
  "flood_risk": {
    "risk_level": "moderate",        ← 🟠 Low/Moderate/High
    "risk_score": 58.5,             ← Score 0-100
    "precipitation_mm": 0.0,
    "cumulative_7day_precip_mm": 0.0,
    "near_water_body": true,         ← Detected!
    "water_body_name": "Venice",
    "factors": {
      "base_location_risk": 45,
      "precipitation_today_risk": 0,
      "cumulative_week_risk": 0,
      "water_proximity_multiplier": 1.3
    }
  }
}
```

---

## How It Calculates Flood Risk

```
Risk Score = (Base + Today's Rain + Week's Rain) × Water Multiplier

Example - Venice on Nov 10, 2024:
  Base risk (EU + water): 45 points
  Today's rain (0mm): 0 points
  Week's rain (0mm): 0 points
  Water multiplier: ×1.3 (Venice detected!)
  
  Final: (45 + 0 + 0) × 1.3 = 58.5 = MODERATE ⚠️
```

### Risk Levels
- 🟢 **Low** (0-30): Safe conditions
- 🟠 **Moderate** (30-60): Caution advised
- 🔴 **High** (60-100): Significant flood risk

---

## Recognized Water Bodies

| Location | Risk Multiplier |
|----------|-----------------|
| 🌊 Venice, Italy | 1.3x |
| 🌊 Amsterdam, Netherlands | 1.3x |
| 🌊 New Orleans, USA | 1.3x |
| 🌊 Miami, USA | 1.3x |
| 🌊 Bangkok, Thailand | 1.3x |

*Near these = automatic +30% risk boost*

---

## Example Scenarios

### Scenario 1: Venice During Peak Season
```bash
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=Venice&date=2024-11-10'
```
**Result**: MODERATE (58.5/100) - Water multiplier boost applied

---

### Scenario 2: Summer in London
```bash
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=London&date=2025-06-15'
```
**Result**: MODERATE (45/100) - EU zone baseline

---

### Scenario 3: Your Home Address
```bash
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=4124%20170th%20PL%20SE%2C%20Bothell%2C%20WA&date=2025-08-15'
```
**Result**: LOW (25/100) - Moderate rain day

---

## Parameters

| Parameter | Required | Format | Example |
|-----------|----------|--------|---------|
| `location` | Yes | City/Address/Landmark | "Venice" or "4124 170th PL SE, Bothell, WA" |
| `date` | Yes | YYYY-MM-DD | "2024-11-10" |

---

## Response Fields

### Weather Section
```
temperature_max_c      - High temperature (°C)
temperature_min_c      - Low temperature (°C)
precipitation_mm       - Rainfall amount (mm)
windspeed_max_kmh      - Max wind speed (km/h)
```

### Flood Risk Section
```
risk_level             - "low", "moderate", or "high"
risk_score             - Numeric score 0-100
precipitation_mm       - Daily rainfall
cumulative_7day_precip_mm  - Last 7 days total
near_water_body        - true/false
water_body_name        - Name if near water (e.g., "Venice")

factors:
  base_location_risk              - Location baseline (0-70)
  precipitation_today_risk        - Today's rain factor (0-30)
  cumulative_week_risk            - 7-day rain factor (0-25)
  water_proximity_multiplier      - 1.0 or 1.3
```

---

## Tips & Tricks

### 1️⃣ Full Address Works
```bash
# This works - smart extraction finds "Bothell"
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=4124%20170th%20PL%20SE%2C%20Bothell%2C%20WA&date=2025-08-15'
```

### 2️⃣ Historical Dates Only
```bash
# Past dates: Works ✓
curl '.../api/flood-risk-date?location=London&date=2024-06-15'

# Future dates: Fails (archive only goes back)
curl '.../api/flood-risk-date?location=London&date=2027-06-15'  # Error!
```

### 3️⃣ Browser Tab Switching
- Click "Flood Risk by Date" tab to see new interface
- Date picker defaults to today
- Enter location and pick a date
- Results show in same tab

### 4️⃣ JSON Pretty Print
```bash
curl 'http://127.0.0.1:5003/api/flood-risk-date?location=Venice&date=2024-11-10' | python3 -m json.tool
```

---

## Precipitation Risk Scale

| Range | Daily Risk | Week Risk | Level |
|-------|-----------|-----------|-------|
| 0-5mm | 0 points | 0 points | Low |
| 5-20mm | +5 points | +5 points | Low-Moderate |
| 20-50mm | +15 points | +15 points | Moderate |
| 50-80mm | +30 points | +15 points | Moderate-High |
| 80-150mm | +30 points | +25 points | High |
| >150mm | +30 points | +25 points | Very High |

---

## Error Messages & Solutions

| Error | Cause | Solution |
|-------|-------|----------|
| "location not found" | Invalid location | Try city name or "London" |
| "missing date parameter" | No date provided | Add `&date=YYYY-MM-DD` |
| "invalid date format" | Wrong format | Use `YYYY-MM-DD` (e.g., 2024-11-10) |
| "no weather data available for date" | Date too far in future | Use historical dates |
| 502 Service error | API timeout | Retry or try different location |

---

## Comparison: Old vs New Features

| Feature | Old | New |
|---------|-----|-----|
| Current weather | ✓ | ✓ |
| Any location | ✓ | ✓ |
| Addresses | ✓ | ✓ |
| **Historical weather** | Limited | ✓ **Detailed** |
| **Specific dates** | ✗ | ✓ **NEW!** |
| **Water body detection** | ✗ | ✓ **NEW!** |
| **Precipitation analysis** | ✗ | ✓ **NEW!** |
| **Risk factors** | Simple | ✓ **Detailed breakdown** |

---

## API Status

```bash
# Check if server is running
curl http://127.0.0.1:5003/health

# Expected response
{"status":"ok"}
```

---

## Browser Access

```
http://127.0.0.1:5003
```

**Tabs available:**
- Current Weather
- Historical Data  
- Flood Risk
- 🆕 **Flood Risk by Date** ← Try this!

---

## For Developers

### Add to Your App
```javascript
// Fetch flood risk
fetch('/api/flood-risk-date?location=Venice&date=2024-11-10')
  .then(r => r.json())
  .then(data => console.log(data.flood_risk));
```

### Python Example
```python
import requests

response = requests.get(
    'http://127.0.0.1:5003/api/flood-risk-date',
    params={
        'location': 'Venice',
        'date': '2024-11-10'
    }
)
risk = response.json()['flood_risk']
print(f"Risk: {risk['risk_level']} ({risk['risk_score']}/100)")
```

---

## Feature Files

For more detailed information:

- 📄 `FEATURE_SUMMARY.md` - Visual overview
- 📄 `FEATURE_IMPLEMENTATION_COMPLETE.md` - Full guide
- 📄 `FLOOD_RISK_BY_DATE_FEATURE.md` - Technical details
- 📄 `IMPLEMENTATION_CHECKLIST.md` - What's included
- 📄 `weather_app/README.md` - Main documentation

---

## Server Info

```
Endpoint: http://127.0.0.1:5003
Status: ✅ Running (Process 14703)
Port: 5003
Host: 127.0.0.1 (localhost only)
```

### To Restart
```bash
pkill -9 -f "python.*weather_app"
cd /Users/karthi_gangavarapu/Downloads/VSCode\ Projects
./.venv/bin/python weather_app/app.py --port 5003 &
```

---

## Questions?

### "How accurate is the flood risk?"
Simplified heuristic based on precipitation and location. For real-time forecasts, check local authorities or Google Flood Hub.

### "Can I use this for real emergency planning?"
This is educational/demo software. Always verify with official sources before making critical decisions.

### "Does it support my country?"
Works anywhere with historical weather data. Water body detection limited to 5 major cities (can expand).

### "How far back in history?"
Typically decades of data via Open-Meteo Archive API.

---

**Ready to get started?** 🚀

Open your browser to: **`http://127.0.0.1:5003`**

Click the **"Flood Risk by Date"** tab and try it out!

---

*Last updated: February 15, 2026*
