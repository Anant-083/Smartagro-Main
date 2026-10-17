import os
import re
import json
import requests
from flask import Blueprint, request, jsonify

from shared import (
    logger, GROQ_API_KEY, OPENWEATHER_API_KEY, LANG_NAMES, CITY_STATE, basedir
)
from routes.market import fetch_agmarknet_prices
from routes.weather import recommend_crops, get_season

chat_bp = Blueprint("chat", __name__)

MAX_AUDIO_B64_LEN = 10 * 1024 * 1024

_CHAT_ON_TOPIC_WORDS = [
    "crop", "crops", "farm", "farming", "farmer", "agriculture", "khet", "kheti", "fasal", "kisan",
    "weather", "mausam", "rain", "barish", "rainfall", "temperature", "temp", "humidity", "monsoon",
    "market", "mandi", "price", "rate", "bhav", "bhaav", "daam", "dam", "apmc", "msp",
    "soil", "mitti", "fertilizer", "khad", "urea", "dap", "npk", "pesticide", "keetnashak",
    "disease", "pest", "keeda", "rog", "fungus", "blight", "rust", "rot", "wilt",
    "irrigation", "sinchai", "water", "pani", "sow", "sowing", "seed", "beej", "harvest",
    "wheat", "rice", "paddy", "maize", "cotton", "soybean", "mustard", "potato", "onion", "tomato",
    "sugarcane", "chilli", "groundnut", "gram", "chana", "arhar", "tur", "moong", "urad",
]

_CHAT_OFF_TOPIC_WORDS = [
    "movie", "cinema", "cricket", "ipl", "football", "song", "music", "actor", "actress",
    "politics", "election", "modi", "rahul", "trump", "bjp", "congress", "crypto", "bitcoin",
    "stock", "share market", "gaming", "pubg", "free fire", "recipe", "cooking", "restaurant",
    "love", "relationship", "joke", "comedy", "shayari", "poem", "horoscope", "astrology",
]

def _chat_message_on_topic(text: str) -> bool:
    if not text or len(text.strip()) < 2:
        return True
    low = text.lower()
    if any(re.search(r'\b' + re.escape(w) + r'\b', low) for w in _CHAT_ON_TOPIC_WORDS):
        return True
    if any(re.search(r'\b' + re.escape(w) + r'\b', low) for w in _CHAT_OFF_TOPIC_WORDS):
        return False
    return True

_OFF_TOPIC_REPLIES = {
    "en": "I'm Kisan Helper — I can only help with farming, crops, weather, market prices, and government schemes. Ask me something about your farm!",
    "hi": "मैं किसान हेल्पर हूं — मैं केवल खेती, फसल, मौसम, मंडी भाव और सरकारी योजनाओं में मदद कर सकता हूं। अपने खेत के बारे में कुछ पूछें!",
    "pa": "ਮੈਂ ਕਿਸਾਨ ਹੈਲਪਰ ਹਾਂ — ਮੈਂ ਸਿਰਫ਼ ਖੇਤੀ, ਫਸਲਾਂ, ਮੌਸਮ, ਮੰਡੀ ਭਾਅ ਅਤੇ ਸਰਕਾਰੀ ਸਕੀਮਾਂ ਵਿੱਚ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ।",
}

def _off_topic_reply(lang: str) -> str:
    return _OFF_TOPIC_REPLIES.get(lang, _OFF_TOPIC_REPLIES["en"])

def _geocode_city(name):
    if not name:
        return None
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en", "countryCode": "IN"},
            timeout=5,
        )
        if resp.status_code == 200:
            results = resp.json().get("results")
            if results:
                r = results[0]
                return {"lat": r["latitude"], "lon": r["longitude"], "name": r.get("name", name), "state": r.get("admin1")}
    except Exception:
        pass
    return None

try:
    with open(os.path.join(basedir, "chat_city_aliases.json"), "r", encoding="utf-8") as f:
        _CHAT_CITY_ALIASES = json.load(f)
except Exception:
    _CHAT_CITY_ALIASES = {}

_RECOGNISED_CITIES = sorted(CITY_STATE.keys(), key=len, reverse=True)

def _chat_detect_city(text: str):
    if not text:
        return None
    low = text.lower()
    for alias, city in _CHAT_CITY_ALIASES.items():
        if alias.lower() in low:
            return city
    for city in _RECOGNISED_CITIES:
        if city.lower() in low:
            return city
    m = re.search(r'\b(?:in|near|at|for|around|mein|me)\s+([A-Za-z\u0900-\u097F]{3,25})\b', low)
    if m:
        candidate = m.group(1).strip()
        stop_words = {"the", "my", "this", "our", "india", "today", "tomorrow", "mandi", "market", "price", "crops", "farm", "khet", "bhav", "rate"}
        if candidate.lower() not in stop_words:
            geo = _geocode_city(candidate)
            if geo and geo.get("name") and geo.get("state"):
                return geo["name"]
    return None

_CHAT_COMMODITY_ALIASES = {
    "Wheat": ["wheat", "gehun", "gehu"],
    "Rice": ["rice", "paddy", "chawal", "dhan"],
    "Maize": ["maize", "corn", "makka", "bhutta"],
    "Potato": ["potato", "aloo", "alu"],
    "Onion": ["onion", "pyaz", "piyaz"],
    "Tomato": ["tomato", "tamatar"],
    "Cotton": ["cotton", "kapas"],
    "Sugarcane": ["sugarcane", "ganna"],
    "Soybean": ["soybean", "soya"],
    "Mustard": ["mustard", "sarson"],
    "Groundnut": ["groundnut", "peanut", "mungfali"],
    "Gram": ["gram", "chana", "chickpea"],
}

def _chat_detect_commodity(text: str):
    low = (text or "").lower()
    for name, aliases in _CHAT_COMMODITY_ALIASES.items():
        if any(a.lower() in low for a in aliases):
            return name
    return None

def _fetch_current_weather(lat, lon):
    if not lat or not lon or not OPENWEATHER_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"},
            timeout=8,
        )
        if resp.status_code == 200:
            d = resp.json()
            return {
                "city": d.get("name", "Your Location"),
                "temp": round(d["main"]["temp"]),
                "feels_like": round(d["main"]["feels_like"]),
                "humidity": d["main"]["humidity"],
                "description": d["weather"][0]["description"],
                "wind_speed": d["wind"]["speed"],
                "rain": d.get("rain", {}).get("1h", 0),
            }
    except Exception:
        pass
    return None

def _ensure_reply_punctuation(text: str) -> str:
    lines = text.split('\n')
    fixed = []
    _VALID_ENDS = ('.', '!', '?', '।', ':', ']', ')')
    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith('•') and stripped:
            last_char = stripped[-1] if stripped else ''
            if last_char not in _VALID_ENDS:
                stripped = stripped + '.'
        fixed.append(stripped)
    return '\n'.join(fixed)

@chat_bp.route("/api/chat", methods=["POST"])
def kisan_chat():
    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY is not configured"}), 500

    data = request.json or {}
    messages = data.get("messages", [])
    lang = data.get("lang", "en")
    if not messages:
        return jsonify({"error": "No messages"}), 400

    lang_name = LANG_NAMES.get(lang, "English")
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = (m.get("content") or "").lower()
            break

    _RESTRICTED_CROP_TERMS = ("tobacco", "opium", "poppy", "cannabis", "hemp", "marijuana", "ganja")
    if any(term in last_user_msg for term in _RESTRICTED_CROP_TERMS):
        return jsonify({"reply": "This app focuses on common food and commercial crops and doesn't advise on licensed/controlled crops like tobacco, opium, or cannabis. I'd be happy to suggest a legal alternative food or cash crop for your area instead!"})

    if not _chat_message_on_topic(last_user_msg):
        return jsonify({"reply": _off_topic_reply(lang)})

    wx = data.get("weather_context") or {}
    location_block = ""
    if wx:
        city = wx.get("city") or wx.get("location") or ""
        temp = wx.get("temp") or ""
        humidity = wx.get("humidity", "")
        parts = []
        if city: parts.append(f"Location: {city}")
        if temp: parts.append(f"Temperature: {temp}°C")
        if humidity: parts.append(f"Humidity: {humidity}%")
        if parts:
            location_block = "\n\nFARMER'S CURRENT LOCATION & WEATHER:\n" + "\n".join(parts)

    named_city = _chat_detect_city(last_user_msg)
    commodity = _chat_detect_commodity(last_user_msg)
    market_notes = []
    if named_city:
        state = CITY_STATE.get(named_city)
        if state:
            prices = fetch_agmarknet_prices(state)
            if prices:
                if commodity:
                    filtered = [p for p in prices if commodity.lower() in p["crop"].lower()]
                    if filtered: prices = filtered
                summary = "; ".join(f"{p['crop']}: ₹{p['price']}/quintal" for p in prices[:6])
                location_block += f"\n\n[LIVE MANDI PRICES for {named_city} ({state})]: {summary}"
                market_notes.append(f"💰 Live Mandi prices ({named_city}): {summary}")

    system_prompt = f"""You are Kisan Helper, a friendly AI agricultural advisory assistant in the SmartAgro app. Answer strictly agricultural queries in {lang_name} using native script. Be concise, practical, and helpful (4 to 5 bullet points). Each bullet must start with • and end with a full stop. If asked to navigate the app, use Markdown links: [Dashboard](/), [Diagnose Crop](/diagnose), [Market Prices](/market), [Alerts](/alerts).{location_block}"""

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "temperature": 0.4,
        "max_tokens": 600,
    }
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body, timeout=25)
        if resp.status_code == 200:
            reply = _ensure_reply_punctuation(resp.json()["choices"][0]["message"]["content"].strip())
            if market_notes:
                reply += "\n\n" + "\n".join(market_notes)
            return jsonify({"reply": reply})
        else:
            return jsonify({"error": f"Groq HTTP {resp.status_code}"}), 500
    except Exception as e:
        logger.warning(f"[Chat] Groq call failed: {e}")
        return jsonify({"error": str(e)}), 500

@chat_bp.route("/api/stt", methods=["POST"])
def speech_to_text():
    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY not configured"}), 500

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "No audio received"}), 400

    audio_bytes = audio_file.read()
    if len(audio_bytes) > MAX_AUDIO_B64_LEN:
        return jsonify({"error": "Recording too long"}), 413
    if len(audio_bytes) < 500:
        return jsonify({"error": "Recording too short"}), 400

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (audio_file.filename or "voice.webm", audio_bytes, audio_file.mimetype or "audio/webm")}
    form_data = {"model": "whisper-large-v3-turbo", "response_format": "json", "temperature": 0}
    try:
        resp = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=form_data, timeout=30)
        if resp.status_code == 200:
            return jsonify({"text": resp.json().get("text", "").strip()})
        return jsonify({"error": "Could not transcribe audio"}), resp.status_code
    except Exception as e:
        logger.warning(f"[STT] Exception: {e}")
        return jsonify({"error": str(e)}), 500
