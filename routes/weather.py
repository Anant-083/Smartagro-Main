import os
import re
import json
import time
import requests
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

from shared import (
    logger, OPENWEATHER_API_KEY, VISUALCROSSING_API_KEY, GROQ_API_KEY,
    DEBUG_MODE, _bounded_cache_set, post_to_groq, _RASTERIO_AVAILABLE
)

if _RASTERIO_AVAILABLE:
    import rasterio
    import numpy as np
    from rasterio.transform import rowcol
    from rasterio.warp import transform as rasterio_warp

weather_bp = Blueprint("weather", __name__)

# ─── Visual Crossing Icon Mapping ───────────────────────────────────────────
_VC_ICON_TO_OWM = {
    "clear-day":            "01d",
    "clear-night":          "01n",
    "partly-cloudy-day":    "02d",
    "partly-cloudy-night":  "02n",
    "cloudy":               "03d",
    "fog":                  "50d",
    "wind":                 "50d",
    "showers-day":          "09d",
    "showers-night":        "09n",
    "rain":                 "10d",
    "thunder-rain":         "11d",
    "thunder-showers-day":  "11d",
    "thunder-showers-night":"11n",
    "snow":                 "13d",
    "snow-showers-day":     "13d",
    "snow-showers-night":   "13n",
}

def _vc_icon_to_owm(vc_icon):
    return _VC_ICON_TO_OWM.get(vc_icon, "02d")

_visualcrossing_cache = {}
_visualcrossing_rate_limited_until = 0
_VISUALCROSSING_CACHE_TTL = 6 * 3600

def _fetch_visualcrossing_forecast(lat, lon):
    if not VISUALCROSSING_API_KEY:
        return []
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return []

    cache_key = (round(lat_f, 1), round(lon_f, 1))
    cached = _visualcrossing_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _VISUALCROSSING_CACHE_TTL:
        return cached["days"]

    global _visualcrossing_rate_limited_until
    if time.monotonic() < _visualcrossing_rate_limited_until:
        return cached["days"] if cached else []

    try:
        url = (
            "https://weather.visualcrossing.com/VisualCrossingWebServices/"
            f"rest/services/timeline/{lat},{lon}"
            "?unitGroup=metric&include=days"
            "&elements=datetime,tempmax,tempmin,humidity,windspeed,precip,conditions,icon"
            f"&key={VISUALCROSSING_API_KEY}&contentType=json"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code == 429:
            _visualcrossing_rate_limited_until = time.monotonic() + 3 * 3600
            logger.warning(f"[VisualCrossing] 429 rate limited — pausing calls for 3h. {resp.text[:200]}")
            return cached["days"] if cached else []
        if resp.status_code != 200:
            logger.info(f"[VisualCrossing] non-200 status {resp.status_code}: {resp.text[:200]}")
            return cached["days"] if cached else []
        days = resp.json().get("days", [])
        out = []
        for d in days:
            try:
                out.append({
                    "date":        d["datetime"],
                    "temp_max":    d.get("tempmax"),
                    "temp_min":    d.get("tempmin"),
                    "description": d.get("conditions", ""),
                    "icon":        _vc_icon_to_owm(d.get("icon", "")),
                    "humidity":    d.get("humidity", 60),
                    "wind_speed":  d.get("windspeed", 10),
                    "rain":        d.get("precip", 0) or 0,
                    "source":      "visualcrossing",
                })
            except Exception:
                continue
        _bounded_cache_set(_visualcrossing_cache, cache_key, {"ts": time.time(), "days": out}, max_entries=500)
        return out
    except Exception as e:
        logger.warning(f"[VisualCrossing] fetch failed: {e}")
        return cached["days"] if cached else []

def _fetch_extended_forecast(lat, lon):
    return _fetch_visualcrossing_forecast(lat, lon)

@weather_bp.route("/api/debug-extended-forecast")
def debug_extended_forecast():
    if not DEBUG_MODE:
        return jsonify({"error": "Not available in production. Set FLASK_DEBUG=1 in .env"}), 403
    lat = request.args.get("lat", "28.6139")
    lon = request.args.get("lon", "77.2090")
    try:
        days = _fetch_extended_forecast(lat, lon)
        return jsonify({
            "status": "ok",
            "lat": lat, "lon": lon,
            "visualcrossing_key_set": bool(VISUALCROSSING_API_KEY),
            "parsed_day_count": len(days),
            "parsed_days":      days[:3],
        })
    except Exception as e:
        return jsonify({"error": str(e), "error_type": type(e).__name__}), 500

@weather_bp.route("/api/weather")
def get_weather():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "Location required"}), 400

    current_url  = (f"https://api.openweathermap.org/data/2.5/weather"
                    f"?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric")
    forecast_url = (f"https://api.openweathermap.org/data/2.5/forecast"
                    f"?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric&cnt=56")

    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            current_future   = ex.submit(requests.get, current_url,  timeout=10)
            forecast_future  = ex.submit(requests.get, forecast_url, timeout=10)
            extended_future  = ex.submit(_fetch_extended_forecast, lat, lon)
            current_resp  = current_future.result()
            forecast_resp = forecast_future.result()
            extended_days = extended_future.result()

        if current_resp.status_code == 429 or forecast_resp.status_code == 429:
            return jsonify({
                "error": "OpenWeather API Rate Limit Reached!",
                "limit_reached": True,
                "api_name": "OpenWeather API",
                "details": "OpenWeather API returned an HTTP 429 Rate Limit error."
            }), 429

        if current_resp.status_code != 200:
            return jsonify({"error": f"Weather API error: {current_resp.text}"}), 500

        current_data  = current_resp.json()
        forecast_data = forecast_resp.json()

        daily = {}
        if forecast_data.get("list"):
            for item in forecast_data["list"]:
                day = datetime.fromtimestamp(item["dt"]).strftime("%Y-%m-%d")
                if day not in daily:
                    daily[day] = {
                        "date":        day,
                        "temp_max":    item["main"]["temp_max"],
                        "temp_min":    item["main"]["temp_min"],
                        "description": item["weather"][0]["description"],
                        "icon":        item["weather"][0]["icon"],
                        "humidity":    item["main"]["humidity"],
                        "wind_speed":  item["wind"]["speed"],
                        "rain":        item.get("rain", {}).get("3h", 0),
                    }
                else:
                    if item["main"]["temp_max"] > daily[day]["temp_max"]:
                        daily[day]["temp_max"] = item["main"]["temp_max"]
                    if item["main"]["temp_min"] < daily[day]["temp_min"]:
                        daily[day]["temp_min"] = item["main"]["temp_min"]

        openweather_days = sorted(daily.values(), key=lambda d: d["date"])
        for d in openweather_days:
            d["source"] = "openweather"

        covered_dates = {d["date"] for d in openweather_days}
        extra_days = [d for d in extended_days if d["date"] not in covered_dates]
        forecast_list = sorted(openweather_days + extra_days, key=lambda d: d["date"])[:16]

        return jsonify({
            "current": {
                "city":        current_data.get("name", "Your Location"),
                "lat":         float(lat),
                "lon":         float(lon),
                "temp":        round(current_data["main"]["temp"]),
                "feels_like":  round(current_data["main"]["feels_like"]),
                "humidity":    current_data["main"]["humidity"],
                "description": current_data["weather"][0]["description"],
                "icon":        current_data["weather"][0]["icon"],
                "wind_speed":  current_data["wind"]["speed"],
                "pressure":    current_data["main"]["pressure"],
                "visibility":  current_data.get("visibility", 0) / 1000,
                "rain":        current_data.get("rain", {}).get("1h", 0),
            },
            "forecast": forecast_list,
        })
    except Exception as e:
        logger.warning(f"[Weather error] {e}")
        return jsonify({"error": str(e)}), 500

# ─── Vegetation Index (NDVI) ────────────────────────────────────────────────
def _ndvi_status(ndvi: float) -> str:
    if ndvi < 0.0:
        return "Water / Cloud / No Data"
    elif ndvi < 0.1:
        return "Bare Soil / Urban / Rock"
    elif ndvi < 0.2:
        return "Sparse Vegetation / Bare Soil"
    elif ndvi < 0.4:
        return "Moderate Vegetation"
    elif ndvi < 0.6:
        return "Good Vegetation Cover"
    else:
        return "Dense / Healthy Vegetation"

def _estimate_vegetation_health(lat: float, lon: float) -> dict:
    month = datetime.now().month
    base_ndvi = 0.35
    if 6 <= month <= 10:
        base_ndvi = 0.62
    elif 11 <= month or month <= 2:
        base_ndvi = 0.48
    else:
        base_ndvi = 0.30

    if lon < 74.0 and lat > 24.0:
        base_ndvi -= 0.12
    elif lat < 18.0:
        base_ndvi += 0.05

    base_ndvi = max(0.15, min(0.85, base_ndvi))
    return {
        "ndvi": round(base_ndvi, 2),
        "status": _ndvi_status(base_ndvi),
        "obs_date": datetime.now().strftime("%Y-%m-%d"),
        "source": "Agro-Climatic Model (Estimated)",
        "cloud_pct": 0,
    }

def get_sentinel2_ndvi(lat: float, lon: float) -> dict | None:
    if not _RASTERIO_AVAILABLE:
        return _estimate_vegetation_health(lat, lon)

    STAC_URL = "https://earth-search.aws.element84.com/v1/search"
    def _query_stac(max_cloud: int) -> list:
        payload = {
            "collections": ["sentinel-2-l2a"],
            "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "limit": 10,
            "query": {"eo:cloud_cover": {"lte": max_cloud}},
        }
        try:
            resp = requests.post(STAC_URL, json=payload, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("features", [])
        except Exception:
            pass
        return []

    features = _query_stac(40) or _query_stac(80)
    if not features:
        return _estimate_vegetation_health(lat, lon)

    item = min(features, key=lambda x: x["properties"].get("eo:cloud_cover", 100))
    obs_date = item["properties"]["datetime"][:10]
    cloud_pct = round(item["properties"].get("eo:cloud_cover", 0), 1)
    red_url = item["assets"]["red"]["href"]
    nir_url = item["assets"]["nir"]["href"]

    def _read_cog_pixel(cog_url: str) -> float:
        with rasterio.open(f"/vsicurl/{cog_url}") as src:
            xs, ys = rasterio_warp("EPSG:4326", src.crs, [lon], [lat])
            col_px, row_px = rowcol(src.transform, xs[0], ys[0])
            win = rasterio.windows.Window(max(0, col_px - 2), max(0, row_px - 2), 5, 5)
            data = src.read(1, window=win).astype(float)
        valid = data[data > 0]
        return float(np.nanmean(valid)) if valid.size > 0 else 0.0

    try:
        env_opts = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_USE_HEAD": "NO", "GDAL_HTTP_TIMEOUT": "10"}
        with rasterio.Env(**env_opts):
            with ThreadPoolExecutor(max_workers=2) as ex:
                rf = ex.submit(_read_cog_pixel, red_url)
                nf = ex.submit(_read_cog_pixel, nir_url)
                red_val = rf.result()
                nir_val = nf.result()
        if (nir_val + red_val) == 0:
            return _estimate_vegetation_health(lat, lon)
        ndvi = (nir_val - red_val) / (nir_val + red_val)
        return {
            "ndvi": round(ndvi, 2),
            "status": _ndvi_status(ndvi),
            "obs_date": obs_date,
            "source": "Sentinel-2 Satellite L2A",
            "cloud_pct": cloud_pct,
        }
    except Exception:
        return _estimate_vegetation_health(lat, lon)

@weather_bp.route("/api/vegetation")
def get_vegetation():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "Location required"}), 400

    def _fallback():
        try:
            return _estimate_vegetation_health(float(lat), float(lon))
        except Exception:
            return {"ndvi": None, "status": "Data Unavailable", "obs_date": None, "source": "Satellite", "cloud_pct": None}

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(get_sentinel2_ndvi, float(lat), float(lon))
            try:
                ndvi_result = future.result(timeout=8)
            except concurrent.futures.TimeoutError:
                return jsonify(_fallback())

        if not ndvi_result:
            return jsonify(_fallback())

        return jsonify(ndvi_result)
    except Exception as e:
        logger.warning(f"[Vegetation error] {e}")
        return jsonify(_fallback())

# ─── Crop Recommendations ────────────────────────────────────────────────────
_crop_ai_cache = {}
CROP_AI_CACHE_TTL_SEC = 3 * 3600

def get_season(month):
    if month in [6, 7, 8, 9]:
        return "Kharif (Monsoon)"
    elif month in [10, 11, 12, 1, 2]:
        return "Rabi (Winter)"
    else:
        return "Zaid (Summer)"

def ai_recommend_crops(city, lat, lon, temp, humidity, rain, season):
    if not GROQ_API_KEY:
        return None
    cache_key = f"{city}|{round((lat or 0), 1)}|{round((lon or 0), 1)}|{season}|{round(temp/3)*3}|{round(humidity/10)*10}"
    now = time.monotonic()
    cached = _crop_ai_cache.get(cache_key)
    if cached and (now - cached[0]) < CROP_AI_CACHE_TTL_SEC:
        return cached[1]

    prompt = f"""You are an expert Indian agronomist advising a farmer in India. Location: {city or "India"} (approx lat {lat}, lon {lon}). Season: {season}. Current weather: {temp}°C, {humidity}% humidity, {rain}mm rain. Recommend EXACTLY 6 distinct regionally appropriate crops suited to this soil and weather. Return JSON: {{"crops": [{{"name":"<crop>","icon":"<emoji>","match":"<score>%","description":"<why>","location_suitability":"<soil/region>","weather_suitability":"<temp/humidity>","season":"Kharif|Rabi|Zaid","water":"High|Medium|Low","yield":"<range>","profit":"<range>","duration":"<days>","soil":"<soil>","fertilizer":"<npk>"}}]}}"""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"}
    }
    try:
        resp = post_to_groq(body, headers)
        if not resp or resp.status_code != 200:
            return None
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        parsed = json.loads(cleaned)
        crops = parsed.get("crops")
        if isinstance(crops, list) and len(crops) >= 4:
            _bounded_cache_set(_crop_ai_cache, cache_key, (now, crops), max_entries=300)
            return crops
    except Exception as e:
        logger.warning(f"[CropAI] Error: {e}")
    return None

def recommend_crops(temp, humidity, rain, season, city="", lat=None, lon=None):
    all_crops = [
        {"name":"Rice","icon":"🌾","temp_range":(20,38),"humidity_range":(70,100),"season":"Kharif (Monsoon)","water":"High","yield":"3-5 tonnes/ha","profit":"Rs45,000-65,000/ha","duration":"90-150 days","description":"Ideal for high humidity & alluvial soil","soil":"Clay loam, alluvial","fertilizer":"NPK 120:60:60 kg/ha","region_lat":(10,30)},
        {"name":"Wheat","icon":"🌿","temp_range":(10,25),"humidity_range":(40,65),"season":"Rabi (Winter)","water":"Medium","yield":"4-6 tonnes/ha","profit":"Rs50,000-75,000/ha","duration":"100-150 days","description":"Thrives in Indo-Gangetic plains & cool winter","soil":"Well-drained loam","fertilizer":"NPK 120:60:40 kg/ha","region_lat":(20,35)},
        {"name":"Maize","icon":"🌽","temp_range":(18,35),"humidity_range":(50,80),"season":"Kharif (Monsoon)","water":"Medium","yield":"5-8 tonnes/ha","profit":"Rs40,000-60,000/ha","duration":"80-110 days","description":"Versatile crop for warm humid weather","soil":"Sandy loam to clay loam","fertilizer":"NPK 150:75:75 kg/ha","region_lat":(12,32)},
        {"name":"Cotton","icon":"☁️","temp_range":(25,40),"humidity_range":(40,70),"season":"Kharif (Monsoon)","water":"Medium","yield":"2-3 tonnes/ha","profit":"Rs60,000-90,000/ha","duration":"150-180 days","description":"Thrives in black cotton soil","soil":"Black cotton soil","fertilizer":"NPK 90:45:45 kg/ha","region_lat":(15,26)},
        {"name":"Tomato","icon":"🍅","temp_range":(18,30),"humidity_range":(60,80),"season":"Zaid (Summer)","water":"Medium","yield":"20-40 tonnes/ha","profit":"Rs80,000-1,50,000/ha","duration":"60-80 days","description":"High value vegetable crop for mild warm weather","soil":"Sandy loam","fertilizer":"NPK 100:60:60 kg/ha","region_lat":(8,32)},
        {"name":"Sugarcane","icon":"🎋","temp_range":(24,38),"humidity_range":(75,90),"season":"Kharif (Monsoon)","water":"Very High","yield":"70-100 tonnes/ha","profit":"Rs70,000-1,00,000/ha","duration":"300-360 days","description":"Requires tropical hot climate & irrigation","soil":"Deep loam","fertilizer":"NPK 250:80:100 kg/ha","region_lat":(12,28)},
    ]
    scored = []
    loc_name = city or "your location"
    for crop in all_crops:
        score = 0
        if crop["temp_range"][0] <= temp <= crop["temp_range"][1]: score += 40
        if crop["humidity_range"][0] <= humidity <= crop["humidity_range"][1]: score += 30
        if crop["season"] == season: score += 20
        crop["score"] = score
        crop["match"] = f"{min(98, max(75, score))}%"
        crop["location_suitability"] = f"Suited to {loc_name}'s regional climate"
        crop["weather_suitability"] = f"Matches current {temp}°C temp & {humidity}% humidity"
        scored.append(crop)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored

def generate_advisory_calendar(crops):
    today = datetime.now()
    activities = [
        {"week":1,  "activity":"Soil preparation & ploughing",  "type":"preparation"},
        {"week":2,  "activity":"Seed treatment & sowing",       "type":"sowing"},
        {"week":3,  "activity":"First irrigation",              "type":"irrigation"},
        {"week":4,  "activity":"Apply basal fertilizer (NPK)",  "type":"fertilizer"},
        {"week":6,  "activity":"Weeding & thinning",            "type":"maintenance"},
        {"week":8,  "activity":"Apply Urea (top dressing)",     "type":"fertilizer"},
        {"week":10, "activity":"Pest & disease inspection",     "type":"pesticide"},
        {"week":12, "activity":"Spray fungicide if required",   "type":"pesticide"},
    ]
    calendar = []
    for act in activities:
        date = today + timedelta(weeks=act["week"])
        calendar.append({"date": date.strftime("%d %b %Y"), "activity": act["activity"], "type": act["type"], "week": act["week"]})
    return calendar

def get_pesticide_guide(crops):
    guides = {
        "Rice":   [{"pest":"Brown Plant Hopper","pesticide":"Imidacloprid 17.8 SL","dose":"125 ml/ha","timing":"At 30 & 60 days after transplanting","eco":False}],
        "Wheat":  [{"pest":"Aphids","pesticide":"Dimethoate 30 EC","dose":"1 L/ha","timing":"At tillering stage","eco":False}],
        "Maize":  [{"pest":"Fall Armyworm","pesticide":"Spinetoram 11.7 SC","dose":"450 ml/ha","timing":"7-10 days after infestation","eco":False}],
    }
    generic = [{"pest": "General pests & fungal disease", "pesticide": "Neem Oil 5%", "dose": "5 ml/L", "timing": "At first sign of pest activity", "eco": True}]
    result = []
    for crop in crops:
        name = crop.get("name", "")
        result.append({"crop": name, "guides": guides.get(name, generic)})
    return result

@weather_bp.route("/api/crop-recommendations", methods=["POST"])
def crop_recommendations():
    data     = request.json or {}
    temp     = data.get("temp", 25)
    humidity = data.get("humidity", 60)
    rain     = data.get("rain", 0)
    city     = data.get("city", "")
    lat      = data.get("lat")
    lon      = data.get("lon")
    season   = get_season(datetime.now().month)

    ai_crops = ai_recommend_crops(city, lat, lon, temp, humidity, rain, season)
    fallback_crops = recommend_crops(temp, humidity, rain, season, city, lat, lon)

    if ai_crops and len(ai_crops) >= 4:
        crops, source = ai_crops, "ai"
    elif ai_crops:
        existing_names = {c.get("name", "").strip().lower() for c in ai_crops}
        merged = list(ai_crops)
        for fc in fallback_crops:
            if fc.get("name", "").strip().lower() not in existing_names:
                merged.append(fc)
            if len(merged) >= 6: break
        crops, source = merged, "ai_hybrid"
    else:
        crops, source = fallback_crops[:6], "rule_based"

    return jsonify({
        "season":     season,
        "city":       city,
        "crops":      crops,
        "calendar":   generate_advisory_calendar(crops[:3]),
        "pesticides": get_pesticide_guide(crops[:3]),
        "source":     source,
    })
