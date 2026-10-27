import re
import json
import time
import hashlib
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

from shared import (
    logger, GROQ_API_KEY, DEBUG_MODE, LANG_NAMES,
    _bounded_cache_set, post_to_groq, translate_terms
)

alerts_bp = Blueprint("alerts", __name__)

# ─── Alert Rule Engine ───────────────────────────────────────────────────────
def _compute_alerts_for_conditions(temp, humidity, wind_speed, rain, description, date_str=None):
    """Rule engine for a single day of weather. Generates distinct, location-and-weather-specific alerts."""
    description = (description or "").lower()
    alerts = []
    d_label = f" ({date_str})" if date_str else ""

    # 1. Temperature-based advisories
    if temp > 38:
        alerts.append({
            "type": "danger", "category": "Weather", "icon": "🌡️",
            "title": f"Extreme Heat Warning{d_label}",
            "message": f"Peak temperature forecast to reach {round(temp)}°C. High risk of crop sun scald and moisture stress.",
            "action": "Increase irrigation frequency to every 3-4 hours. Provide green shade nets for sensitive crops."
        })
    elif temp > 31:
        alerts.append({
            "type": "warning", "category": "Weather", "icon": "☀️",
            "title": f"Moderate Heat Advisory{d_label}",
            "message": f"Warm weather around {round(temp)}°C. Increased crop evapotranspiration expected.",
            "action": "Irrigate during early morning or evening hours to minimize water evaporation."
        })
    elif temp < 10:
        alerts.append({
            "type": "warning", "category": "Weather", "icon": "❄️",
            "title": f"Chill & Frost Alert{d_label}",
            "message": f"Low temperature of {round(temp)}°C detected. Risk of cold injury in young saplings.",
            "action": "Cover nursery beds with plastic mulch or straw. Apply light evening irrigation."
        })
    else:
        alerts.append({
            "type": "info", "category": "Crop Advisory", "icon": "🌱",
            "title": f"Favorable Growth Climate{d_label}",
            "message": f"Comfortable temperature of {round(temp)}°C supporting active photosynthesis.",
            "action": "Favorable day for field cultivation, top-dressing nitrogen, and weeding."
        })

    # 2. Humidity & Pest/Disease advisories
    if humidity > 75:
        alerts.append({
            "type": "warning", "category": "Disease", "icon": "🍄",
            "title": f"Fungal Blight Risk ({humidity}% Humidity)",
            "message": f"Relative humidity at {humidity}%. Moist microclimate promotes fungal spore multiplication.",
            "action": "Apply preventive systemic fungicide (Mancozeb 75 WP at 2.5 g/L) immediately."
        })
    elif humidity > 55:
        alerts.append({
            "type": "warning", "category": "Pest", "icon": "🐛",
            "title": f"Sap-Sucking Pest Watch ({humidity}% Humidity)",
            "message": f"Humidity of {humidity}% with {round(temp)}°C temp favors aphid and whitefly activity.",
            "action": "Set up yellow sticky traps (10/acre) or spray Neem oil (5 ml/L) at dusk."
        })
    else:
        alerts.append({
            "type": "warning", "category": "Pest", "icon": "🕷️",
            "title": f"Mite & Thrips Alert ({humidity}% Humidity)",
            "message": f"Dry atmospheric conditions ({humidity}% humidity) increase spider mite reproduction.",
            "action": "Spray Abamectin 1.8 EC (0.5 ml/L) and maintain soil moisture levels."
        })

    # 3. Wind & Rain advisories
    if rain > 25:
        alerts.append({
            "type": "danger", "category": "Weather", "icon": "🌧️",
            "title": f"Heavy Rain & Drainage Alert ({round(rain)} mm)",
            "message": f"Expected rainfall of {round(rain)} mm. Risk of waterlogging and root asphyxiation.",
            "action": "Ensure field drainage channels are open. Pause all chemical spraying operations."
        })
    elif rain > 2:
        alerts.append({
            "type": "info", "category": "Weather", "icon": "🌦️",
            "title": f"Light Rain Forecast ({round(rain)} mm)",
            "message": f"Intermittent light showers expected ({round(rain)} mm). Replenishes topsoil moisture.",
            "action": "Hold off on routine irrigation for 24-48 hours."
        })
    else:
        if wind_speed > 22:
            alerts.append({
                "type": "warning", "category": "Weather", "icon": "💨",
                "title": f"High Wind Warning ({round(wind_speed)} km/h)",
                "message": f"Wind speeds up to {round(wind_speed)} km/h may cause lodging in tall standing crops.",
                "action": "Avoid foliar pesticide spraying to prevent chemical drift. Stake tall crops."
            })
        else:
            alerts.append({
                "type": "info", "category": "Crop Advisory", "icon": "🌾",
                "title": f"Clear Field Work Window ({round(wind_speed)} km/h wind)",
                "message": f"Dry conditions with gentle wind ({round(wind_speed)} km/h).",
                "action": "Ideal time for fertilizer application, harvesting, and crop drying."
            })

    return alerts

# ─── AI-Enhanced Alerts via Groq ─────────────────────────────────────────────
_ai_alerts_cache = {}
AI_ALERTS_CACHE_TTL_SEC = 3 * 60 * 60  # 3 hours

def _ai_alerts_for_today(city, lat, lon, temp, humidity, wind_speed, rain, description):
    """Ask Groq for location-specific, weather-aware alerts for today."""
    if not GROQ_API_KEY:
        return None

    cache_key = f"today|{city}|{round(lat or 0, 1)}|{round(lon or 0, 1)}|{round(temp/3)*3}|{round(humidity/10)*10}"
    now = time.monotonic()
    cached = _ai_alerts_cache.get(cache_key)
    if cached and (now - cached[0]) < AI_ALERTS_CACHE_TTL_SEC:
        return cached[1]

    prompt = (
        f"You are an expert agricultural meteorologist for India. Location: {city or 'Unknown'} (lat {lat}, lon {lon}) "
        f"Today's weather: {temp}°C, {humidity}% humidity, wind {wind_speed} m/s, {rain} mm rain, {description}. "
        f"Generate 3–6 specific, actionable agricultural alerts for a farmer at this location based on TODAY's weather conditions. "
        f"Each alert must be SPECIFIC to these exact conditions. "
        f"Categories: Weather, Pest, Disease, Crop Advisory. Types: danger, warning, info. "
        f'Respond ONLY with valid JSON: {{"alerts": [{{"type": "danger|warning|info", "category": "Weather|Pest|Disease|Crop Advisory", "icon": "one emoji", "title": "Short alert title", "message": "Detailed description of the risk", "action": "Specific action"}}]}}'
    )

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 3500,
        "response_format": {"type": "json_object"}
    }
    try:
        resp = post_to_groq(body, headers)
        if resp is None or resp.status_code != 200:
            return None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        parsed = json.loads(match.group() if match else cleaned)
        alerts = parsed.get("alerts")
        if not isinstance(alerts, list) or not alerts:
            return None
        valid = [a for a in alerts if all(k in a for k in ("type", "category", "icon", "title", "message", "action"))]
        if not valid:
            return None
        _bounded_cache_set(_ai_alerts_cache, cache_key, (now, valid), max_entries=300)
        logger.info(f"[AlertsAI] OK for today/{city}: {len(valid)} alerts")
        return valid
    except Exception as e:
        logger.warning(f"[AlertsAI] error for today/{city}: {e}")
        return None

def _ai_alerts_for_forecast(city, lat, lon, forecast_days):
    """Ask Groq for location-specific alerts for ALL forecast days in a single prompt."""
    if not GROQ_API_KEY or not forecast_days:
        return None

    days_summary = "\n".join([
        f"- Date {d.get('date','?')}: Temp {d.get('temp_min',20)}°C to {d.get('temp_max',25)}°C, "
        f"Humidity {d.get('humidity',60)}%, Wind {d.get('wind_speed',10)} km/h, Rain {d.get('rain',0)} mm, {d.get('description','')}"
        for d in forecast_days
    ])

    cache_key = f"forecast|{city}|{round(lat or 0, 1)}|{round(lon or 0, 1)}|{hashlib.md5(days_summary.encode()).hexdigest()[:12]}"
    now = time.monotonic()
    cached = _ai_alerts_cache.get(cache_key)
    if cached and (now - cached[0]) < AI_ALERTS_CACHE_TTL_SEC:
        return cached[1]

    prompt = (
        f"You are an agricultural officer for {city or 'India'}. Analyze this {len(forecast_days)}-day weather forecast for local farmers: "
        f"{days_summary}. Provide specific, realistic agricultural alerts tailored to EACH day's exact weather. "
        f"You MUST generate custom alerts for EVERY SINGLE DATE listed above. "
        f'Respond ONLY with valid JSON: {{"days": [{{"date": "YYYY-MM-DD", "alerts": [{{"type": "danger|warning|info", "category": "Weather|Pest|Disease|Crop Advisory", "icon": "relevant emoji", "title": "Clear alert title", "message": "Scientific yet practical advisory", "action": "Actionable farmer recommendation"}}]}}]}}'
    )

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 7000,
        "response_format": {"type": "json_object"}
    }
    try:
        resp = post_to_groq(body, headers)
        if resp is None or resp.status_code != 200:
            return None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        parsed = json.loads(match.group() if match else cleaned)
        days_data = parsed.get("days")
        if not isinstance(days_data, list):
            return None

        result = {}
        for day_obj in days_data:
            d_str = day_obj.get("date")
            day_alerts = day_obj.get("alerts", [])
            valid = [a for a in day_alerts if all(k in a for k in ("type", "category", "icon", "title", "message", "action"))]
            if d_str and valid:
                result[d_str] = valid

        _bounded_cache_set(_ai_alerts_cache, cache_key, (now, result), max_entries=300)
        logger.info(f"[AlertsAI] OK for forecast/{city}: {len(result)} dates with AI alerts")
        return result
    except Exception as e:
        logger.warning(f"[AlertsAI] error for forecast/{city}: {e}")
        return None

# ─── Crop Risk Thresholds ────────────────────────────────────────────────────
CROP_RISK_THRESHOLDS = {
    "rice":       {"min_temp": 20, "max_temp": 38, "min_humidity": 70, "max_wind": 45, "flood_ok": True},
    "wheat":      {"min_temp": 10, "max_temp": 25, "min_humidity": 40, "max_wind": 45, "flood_ok": False},
    "maize":      {"min_temp": 18, "max_temp": 35, "min_humidity": 50, "max_wind": 40, "flood_ok": False},
    "cotton":     {"min_temp": 25, "max_temp": 40, "min_humidity": 40, "max_wind": 40, "flood_ok": False},
    "tomato":     {"min_temp": 18, "max_temp": 30, "min_humidity": 60, "max_wind": 35, "flood_ok": False},
    "sugarcane":  {"min_temp": 24, "max_temp": 38, "min_humidity": 75, "max_wind": 45, "flood_ok": True},
    "soybean":    {"min_temp": 20, "max_temp": 32, "min_humidity": 60, "max_wind": 40, "flood_ok": False},
    "mustard":    {"min_temp": 10, "max_temp": 25, "min_humidity": 40, "max_wind": 40, "flood_ok": False},
    "potato":     {"min_temp": 10, "max_temp": 22, "min_humidity": 60, "max_wind": 35, "flood_ok": False},
    "onion":      {"min_temp": 13, "max_temp": 28, "min_humidity": 50, "max_wind": 35, "flood_ok": False},
    "chilli":     {"min_temp": 20, "max_temp": 35, "min_humidity": 60, "max_wind": 35, "flood_ok": False},
    "groundnut":  {"min_temp": 22, "max_temp": 36, "min_humidity": 50, "max_wind": 40, "flood_ok": False},
    "bajra":      {"min_temp": 20, "max_temp": 42, "min_humidity": 30, "max_wind": 45, "flood_ok": False},
    "jowar":      {"min_temp": 18, "max_temp": 38, "min_humidity": 35, "max_wind": 45, "flood_ok": False},
    "gram":       {"min_temp": 10, "max_temp": 27, "min_humidity": 35, "max_wind": 40, "flood_ok": False},
}

WATER_NEED_FALLBACK = {
    "low":        {"min_temp": 10, "max_temp": 42, "min_humidity": 25, "max_wind": 45, "flood_ok": False},
    "medium":     {"min_temp": 12, "max_temp": 38, "min_humidity": 40, "max_wind": 42, "flood_ok": False},
    "high":       {"min_temp": 15, "max_temp": 38, "min_humidity": 55, "max_wind": 42, "flood_ok": False},
    "very high":  {"min_temp": 18, "max_temp": 38, "min_humidity": 65, "max_wind": 42, "flood_ok": True},
}

def _find_crop_thresholds(name, water_need):
    name_l = (name or "").lower()
    for key, thresholds in CROP_RISK_THRESHOLDS.items():
        if key in name_l or name_l in key:
            return thresholds, True
    fallback_key = (water_need or "medium").lower()
    return WATER_NEED_FALLBACK.get(fallback_key, WATER_NEED_FALLBACK["medium"]), False

def _seasonal_advisories(city, humidity=None):
    """Standalone season/alert logic shared by alerts route and kisan-helper."""
    month = datetime.now().month
    if month in [3, 4, 5]:
        season = "Summer"
    elif month in [6, 7, 8, 9]:
        season = "Monsoon"
    elif month in [10, 11]:
        season = "Post-Monsoon"
    else:
        season = "Winter"

    alerts = [
        {"type": "info", "icon": "ℹ️", "title": "Soil Preparation",
         "message": f"Good time to prepare soil for {season} crops in {city}."},
        {"type": "info", "icon": "🗓️", "title": "Seasonal Advisory",
         "message": f"General farming calendar guidance for the {season} season in {city}. "
                    f"See the Alerts and Dashboard pages for live, weather-specific risk data."},
    ]

    if isinstance(humidity, (int, float)) and humidity > 75:
        alerts.append({
            "type": "warning", "icon": "🐛", "title": "Pest & Fungal Risk",
            "message": f"Current humidity of {humidity}% is high enough to favor pest and fungal activity.",
        })

    return season, alerts

# ─── Alert Endpoints ─────────────────────────────────────────────────────────
@alerts_bp.route("/api/alerts", methods=["POST"])
def get_alerts():
    data        = request.json or {}
    temp        = data.get("temp", 25)
    humidity    = data.get("humidity", 60)
    wind_speed  = data.get("wind_speed", 10)
    rain        = data.get("rain", 0)
    description = data.get("description", "")
    city        = data.get("city", "")
    lat         = data.get("lat")
    lon         = data.get("lon")

    ai_alerts = _ai_alerts_for_today(city, lat, lon, temp, humidity, wind_speed, rain, description)
    alerts = ai_alerts if ai_alerts else _compute_alerts_for_conditions(temp, humidity, wind_speed, rain, description)
    return jsonify({"alerts": alerts, "total": len(alerts)})

@alerts_bp.route("/api/alerts-forecast", methods=["POST"])
def get_alerts_forecast():
    data     = request.json or {}
    forecast = data.get("forecast", [])
    city     = data.get("city", "")
    lat      = data.get("lat")
    lon      = data.get("lon")
    if not isinstance(forecast, list) or not forecast:
        return jsonify({"error": "forecast array required"}), 400

    today_alerts = data.get("today_alerts")
    if today_alerts and len(forecast) > 0:
        today_date = forecast[0].get("date")
        forecast_for_ai = forecast[1:]
        ai_per_day_map = _ai_alerts_for_forecast(city, lat, lon, forecast_for_ai) or {}
        ai_per_day_map[today_date] = today_alerts
    else:
        ai_per_day_map = _ai_alerts_for_forecast(city, lat, lon, forecast) or {}

    daily = []
    danger_days = []

    for i, day in enumerate(forecast):
        date        = day.get("date", "")
        temp_max    = day.get("temp_max", 25)
        temp_min    = day.get("temp_min", 20)
        temp_avg    = (temp_max + temp_min) / 2
        humidity    = day.get("humidity", 60)
        wind_speed  = day.get("wind_speed", 10)
        rain        = day.get("rain", 0)
        description = day.get("description", "")

        day_alerts = ai_per_day_map.get(date) if isinstance(ai_per_day_map, dict) else None
        if not day_alerts:
            day_alerts = _compute_alerts_for_conditions(temp_avg, humidity, wind_speed, rain, description, date_str=date)

        danger_count  = sum(1 for a in day_alerts if a["type"] == "danger")
        warning_count = sum(1 for a in day_alerts if a["type"] == "warning")
        info_count    = sum(1 for a in day_alerts if a["type"] == "info")

        daily.append({
            "date":          date,
            "temp_max":      temp_max,
            "temp_min":      temp_min,
            "description":   description,
            "icon":          day.get("icon", ""),
            "alerts":        day_alerts,
            "danger_count":  danger_count,
            "warning_count": warning_count,
            "info_count":    info_count,
            "total":         len(day_alerts),
        })

        if danger_count > 0:
            danger_days.append({
                "date":  date,
                "titles": [a["title"] for a in day_alerts if a["type"] == "danger"],
            })

    return jsonify({
        "daily": daily,
        "summary": {
            "total_danger_days": len(danger_days),
            "danger_days":       danger_days,
        }
    })

@alerts_bp.route("/api/crop-risk", methods=["POST"])
def crop_risk():
    data     = request.json or {}
    crops    = data.get("crops", [])
    forecast = data.get("forecast", [])

    if not isinstance(crops, list) or not crops:
        return jsonify({"error": "crops array required"}), 400
    if not isinstance(forecast, list) or not forecast:
        return jsonify({"error": "forecast array required"}), 400

    total_days = len(forecast)
    results = []

    for crop in crops:
        name       = crop.get("name", "Unknown")
        icon       = crop.get("icon", "🌱")
        water_need = crop.get("water", "Medium")
        thresholds, matched = _find_crop_thresholds(name, water_need)

        risky_days = []
        for day in forecast:
            temp_avg   = (day.get("temp_max", 25) + day.get("temp_min", 20)) / 2
            humidity   = day.get("humidity", 60)
            wind_speed = day.get("wind_speed", 10)
            rain       = day.get("rain", 0)

            reasons = []
            if temp_avg < thresholds["min_temp"]:
                reasons.append(f"Too cold ({round(temp_avg)}°C, needs {thresholds['min_temp']}°C+)")
            if temp_avg > thresholds["max_temp"]:
                reasons.append(f"Too hot ({round(temp_avg)}°C, tolerates up to {thresholds['max_temp']}°C)")
            if humidity < thresholds["min_humidity"]:
                reasons.append(f"Humidity too low ({humidity}%, needs {thresholds['min_humidity']}%+)")
            if wind_speed > thresholds["max_wind"]:
                reasons.append(f"Damaging winds expected ({round(wind_speed)} m/s)")
            if rain > 50 and not thresholds.get("flood_ok"):
                reasons.append(f"Heavy rain risk of waterlogging ({round(rain)} mm)")

            if reasons:
                risky_days.append({"date": day.get("date", ""), "reasons": reasons})

        risky_count    = len(risky_days)
        danger_percent = round((risky_count / total_days) * 100) if total_days else 0
        risk_level     = "High" if danger_percent >= 60 else "Medium" if danger_percent >= 30 else "Low"

        results.append({
            "name":           name,
            "icon":           icon,
            "matched_crop_db": matched,
            "danger_percent": danger_percent,
            "risk_level":     risk_level,
            "safe_days":      total_days - risky_count,
            "risky_days":     risky_days,
            "total_days":     total_days,
        })

    return jsonify({"crops": results})

@alerts_bp.route("/api/monthly-alerts", methods=["POST"])
def get_monthly_alerts():
    data = request.json or {}
    forecast = data.get("forecast", [])
    
    monthly = []
    base_date = datetime.now()
    if forecast and isinstance(forecast, list) and "date" in forecast[0]:
        try:
            base_date = datetime.strptime(forecast[0]["date"], "%Y-%m-%d")
        except ValueError:
            pass
    daily_alerts = { d.get("date"): d for d in data.get("daily_alerts", []) }
    window_days = min(len(forecast), 15) if forecast else 15

    for i in range(window_days):
        curr_date = base_date + timedelta(days=i)
        date_str = curr_date.strftime("%Y-%m-%d")
        
        if date_str in daily_alerts:
            d_alerts = daily_alerts[date_str]
            danger = d_alerts.get("danger_count", 0)
            warning = d_alerts.get("warning_count", 0)
            info = d_alerts.get("info_count", 0)
            
            if danger > 0:
                risk = "danger"
                risk_pct = min(100, 70 + danger*15)
            elif warning > 0:
                risk = "warning"
                risk_pct = min(65, 35 + warning*15)
            elif info > 0:
                risk = "warning"
                risk_pct = 25
            else:
                risk = "safe"
                risk_pct = 10
            
            alerts = [{"title": a.get("title", "")} for a in d_alerts.get("alerts", [])]
        else:
            monthly.append({
                "date":          date_str,
                "risk":          "unavailable",
                "risk_pct":      None,
                "alerts":        [],
                "data_available": False,
            })
            continue

        monthly.append({
            "date":          date_str,
            "risk":          risk,
            "risk_pct":      risk_pct,
            "alerts":        alerts,
            "data_available": True,
        })
        
    return jsonify({"monthly": monthly})

@alerts_bp.route("/api/seasonal-alerts", methods=["POST"])
def get_seasonal_alerts():
    data     = request.json or {}
    city     = data.get("city", "Unknown")
    humidity = data.get("humidity")
    season, alerts = _seasonal_advisories(city, humidity)
    return jsonify({"season": season, "city": city, "alerts": alerts})

# ─── Alerts & Dashboard Translations ─────────────────────────────────────────
_alerts_translation_cache = {}
_dashboard_translation_cache = {}

@alerts_bp.route("/api/translate-alerts", methods=["POST"])
def translate_alerts():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()
    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")
    terms = [
        "Extreme Heat Alert", "Frost Warning", "High Fungal Disease Risk",
        "High Wind Speed Alert", "Heavy Rainfall Alert", "Thunderstorm Warning",
        "Aphid & Whitefly Risk", "Spider Mite Alert", "Crops at Risk in Current Conditions",
        "Temperature above 40°C. Provide shade netting and increase irrigation frequency.",
        "Sub-zero temperatures expected. Frost can destroy standing crops overnight.",
        "Humidity above 85% creates ideal conditions for fungal diseases.",
        "Strong winds can cause lodging in tall crops like maize and wheat.",
        "Excessive rain may cause waterlogging and root rot.",
        "Thunderstorm conditions detected. Risk of lightning and hail damage.",
        "Warm humid conditions are ideal for aphid multiplication.",
        "Hot dry conditions favour rapid spider mite population growth.",
        "Schedule irrigation every 4-5 hours. Avoid afternoon spraying.",
        "Cover crops with frost cloth. Use smudge pots or sprinkler irrigation.",
        "Apply preventive fungicide (Mancozeb 75 WP at 2.5 g/L) immediately.",
        "Avoid spraying. Support tall crops with stakes.",
        "Ensure field drainage channels are open. Pause irrigation.",
        "Stay indoors. Secure farm equipment.",
        "Spray Neem oil (5 ml/L) or Imidacloprid 0.3 ml/L at dusk.",
        "Apply Abamectin 1.8 EC (0.5 ml/L). Increase soil moisture.",
        "Consider alternate crops better suited to current climate.",
        "Action", "Critical", "Warning", "Advisory", "Danger",
        "All Alerts", "Warnings", "Advisories", "Weather", "Pest", "Crop Advisory", "Disease",
        "Brown Plant Hopper", "Aphids", "Fall Armyworm", "Whitefly",
        "Red Spider Mite", "Stem Borer", "Thrips", "Mealy Bug",
        "Kharif (Jun–Oct)", "Rabi (Nov–Feb)", "Kharif (Jul–Sep)",
        "Year-round", "Zaid (Mar–May)", "Kharif (Jun–Sep)", "Rabi & Zaid",
        "Rice, Paddy", "Wheat, Mustard, Vegetables", "Maize, Sorghum",
        "Cotton, Tomato, Chilli", "Soybean, Cotton, Brinjal",
        "Rice, Sugarcane, Maize", "Onion, Chilli, Groundnut", "Cotton, Grapes, Papaya",
        "Feeds on rice plants causing \"hopperburn\". Thrives in humid conditions above 75%.",
        "Suck plant sap, transmit viral diseases. High risk in mild temperatures (15–25°C).",
        "Causes significant leaf damage and can destroy entire crops within days.",
        "Transmits leaf curl virus to cotton. Population explosion in dry hot weather.",
        "Causes bronzing/yellowing of leaves. Severe in hot, dry weather above 32°C.",
        "Bores into stems causing \"dead heart\" in vegetative stage and \"white ear\" at heading.",
        "Causes silvery white patches on leaves. Severe in dry weather.",
        "Forms white waxy colonies on plant parts. Excretes honeydew causing sooty mould.",
        "Use resistant varieties. Avoid excess nitrogen. Keep fields drained.",
        "Neem oil spray. Release ladybird beetles as biocontrol.",
        "Early detection critical. Bt-based bioinsecticide spray.",
        "Yellow sticky traps. Reflective mulch. Imidacloprid at threshold level.",
        "Increase irrigation. Abamectin 1.8 EC spray. Avoid dust on leaves.",
        "Pheromone traps. Chlorpyriphos 20 EC. Remove crop residues after harvest.",
        "Spinosad spray. Blue sticky traps. Avoid drought stress.",
        "Buprofezin spray. Introduce Cryptolaemus beetles as biocontrol.",
        "High", "Medium", "Low", "Risk", "Active Now", "Affects",
        "Chlorpyriphos 20 EC", "Imidacloprid 17.8 SL", "Mancozeb 75 WP",
        "Neem Oil 5% EC (Organic)", "Propiconazole 25 EC", "Emamectin Benzoate 5 SG",
        "Stem borer, Aphids, Termites", "Whitefly, Aphids, Brown Plant Hopper",
        "Leaf blight, Early blight, Rust, Downy mildew",
        "Aphids, Whitefly, Mites, Fungal diseases",
        "Yellow rust, Brown rust, Sheath blight",
        "Fall Armyworm, Diamond back moth, Leaf miner",
        "Target Pest", "Safe Dose", "Max Limit", "Interval", "Pre-Harvest", "PPE Required",
        "Every 14 days", "Every 21 days max", "Every 7–10 days",
        "Every 5–7 days", "Max 2 sprays per season", "Every 10–14 days",
        "15 days before harvest", "21 days before harvest", "7 days before harvest",
        "No waiting period — organic",
        "Gloves, Mask, Goggles, Full sleeve clothing",
        "Gloves, Mask, Full body protection", "Gloves, Goggles, Dust Mask",
        "Basic gloves recommended", "Full protective gear, closed shoes",
        "Full PPE, respiratory protection",
        "Highly toxic to fish and bees. Do not spray near water bodies or during flowering.",
        "Do NOT spray during bee activity (morning/evening). Highly toxic to pollinators.",
        "Causes skin and eye irritation. Do not spray on edible parts 7 days before harvest.",
        "Safe for humans and beneficial insects. May cause phytotoxicity in direct sunlight. Spray at dusk.",
        "Do not mix with alkaline pesticides. Causes groundwater contamination if overused.",
        "Highly toxic to aquatic organisms. Dispose empty containers safely. Do not reuse containers.",
        "Rice", "Wheat", "Maize", "Cotton", "Tomato", "Sugarcane",
        "Soybean", "Mustard", "Potato", "Onion", "Chilli", "Groundnut",
        "Risky", "Safe", "Suitable for", "humidity",
        "No harmful crops identified for current conditions.",
        "No fully safe crops identified — check crop calendar.",
        "Heat Stress", "Humidity Risk", "Wind Damage", "Pest Risk",
        "Disease Risk", "Pest Activity", "Overall Risk", "Current Risk Level (%)",
        "Safe to grow all 6 upcoming days", "Checking forecast...", "Upcoming Risks Checked",
        "Check Upcoming Risks (6-Day Forecast)", "No critical weather risks in the next 6 days.",
        "Too cold (min 10°C needed)", "Too cold (min 13°C needed)", "Too cold (min 18°C needed)",
        "Too cold (min 20°C needed)", "Too cold (min 22°C needed)", "Too cold (min 24°C needed)",
        "Too cold (min 25°C needed)", "Too hot (max 22°C tolerated)", "Too hot (max 25°C tolerated)",
        "Too hot (max 28°C tolerated)", "Too hot (max 30°C tolerated)", "Too hot (max 32°C tolerated)",
        "Too hot (max 35°C tolerated)", "Too hot (max 36°C tolerated)", "Too hot (max 38°C tolerated)",
        "Too hot (max 40°C tolerated)", "Humidity too low (min 40% needed)", "Humidity too low (min 50% needed)",
        "Humidity too low (min 60% needed)", "Humidity too low (min 70% needed)", "Humidity too low (min 75% needed)",
    ]
    domain_note = "Agricultural alerts page for Indian farmers. Translate preserving chemical and unit names."
    translations, cached = translate_terms(terms, lang_name, domain_note, lang, _alerts_translation_cache, lang_code=lang)
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})

@alerts_bp.route("/api/translate-dashboard", methods=["POST"])
def translate_dashboard():
    data = request.json or {}
    lang = data.get("lang", "en").strip().lower()
    if lang == "en":
        return jsonify({"lang": "en", "translations": {}, "cached": False})

    lang_name = LANG_NAMES.get(lang, "Hindi")
    terms = [
        "Dashboard","Diagnose Crop","Market Prices","Alerts","Get My Location",
        "Location Found","Fetching weather...","Awaiting location...",
        "Current Weather Conditions","Live data from your location",
        "6-Day Forecast","Temperature","Humidity","Wind","Visibility","Pressure",
        "Feels like","Calm","Light breeze","Moderate breeze","Strong breeze","Storm warning",
        "Crop Recommendations","Based on your climate & location",
        "Season","Water Need","Expected Yield","Duration","Soil Type","Fertilizer",
        "Estimated Profit","Match",
        "Crop Advisory Calendar","Week-by-week action plan for your crops",
        "Pesticide & Pest Control Guide","Safe and effective crop protection plan",
        "Quick Actions","Diagnose Crop Disease","Upload or take a photo of your crop",
        "Check Market Prices","Live mandi prices across India",
        "View Active Alerts","Weather & pest warnings for your area",
        "Empowering farmers with AI-driven precision agriculture",
        "Eco-Friendly","Chemical","Week",
        "Kharif (Monsoon)","Rabi (Winter)","Zaid (Summer)",
        "Very High","High","Medium","Low",
        "preparation","sowing","irrigation","fertilizer","maintenance","pesticide","harvest",
        "Soil preparation & ploughing","Seed treatment & sowing","First irrigation",
        "Apply basal fertilizer (NPK)","Weeding & thinning","Apply Urea (top dressing)",
        "Pest & disease inspection","Spray fungicide if required",
        "Foliar spray micronutrients","Pre-harvest irrigation stop","Harvest preparation",
        "Rice","Wheat","Maize","Cotton","Tomato","Sugarcane","Soybean","Mustard",
        "Ideal for high humidity and warm conditions", "Best suited for cool, dry winters",
        "Versatile crop for warm humid weather", "Thrives in hot dry spells with moderate rain",
        "High value crop for moderate climates", "Requires hot climate and heavy rainfall",
        "Nitrogen-fixing legume for warm monsoon", "Cool weather oil seed crop",
        "Clay loam, alluvial","Well-drained loam","Sandy loam to clay loam",
        "Black cotton soil","Sandy loam, rich organic matter","Deep loam, good drainage",
        "Brown Plant Hopper","Leaf folder","Aphids","Yellow rust",
        "Fall Armyworm","Stem borer","Bollworm","Whitefly",
        "Pest Control Plan","Crop","Timing",
    ]
    domain_note = "Crop, pest, and field-activity names should be the common name farmers actually use."
    translations, cached = translate_terms(terms, lang_name, domain_note, lang, _dashboard_translation_cache, lang_code=lang)
    return jsonify({"lang": lang, "lang_name": lang_name, "translations": translations, "cached": cached})
