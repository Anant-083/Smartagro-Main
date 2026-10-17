import os
import sys
import time
import json
import uuid
import socket
import logging
import threading
import requests
from datetime import datetime
from dotenv import load_dotenv

# ── Force IPv4 on Windows ──────────────────────────────────────────────────
try:
    import urllib3.util.connection as urllib3_conn
    def _allowed_gai_family():
        return socket.AF_INET
    if sys.platform == "win32":
        urllib3_conn.allowed_gai_family = _allowed_gai_family
except Exception as _e:
    pass

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

# ── Structured Logger ───────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("smartagro")

# ── Optional heavy deps for Sentinel-2 NDVI ─────────────────────────────────
try:
    import rasterio
    import numpy as np
    from rasterio.transform import rowcol
    from rasterio.warp import transform as rasterio_warp
    _RASTERIO_AVAILABLE = True
except ImportError:
    _RASTERIO_AVAILABLE = False
    logger.info("[AgroSmart] rasterio not installed – NDVI will fall back to estimation")

BUILD_VERSION = str(int(time.time()))
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
VISUALCROSSING_API_KEY = os.getenv("VISUALCROSSING_API_KEY", "")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GEMINI_DIAGNOSE_KEY  = os.getenv("GEMINI_API_KEY_DIAGNOSE", "") or os.getenv("GEMINI_API_KEY", "")
GEMINI_TRANSLATE_KEY = os.getenv("GEMINI_API_KEY_TRANSLATE", "") or os.getenv("GEMINI_API_KEY", "")
GEMINI_TRANSLATE_MODEL = os.getenv("GEMINI_TRANSLATE_MODEL", "gemini-3.5-flash")
GEMINI_DIAGNOSIS_MODEL = os.getenv("GEMINI_DIAGNOSIS_MODEL", "gemini-3.6-flash")
NINJA_API_KEY       = os.getenv("NINJA_API_KEY", "")
DATA_GOV_API_KEY    = os.getenv("DATA_GOV_API_KEY", "")
DEBUG_MODE          = os.getenv("FLASK_DEBUG", "0") == "1"
ADMIN_SECRET_KEY    = os.getenv("ADMIN_SECRET_KEY", "")

LANG_NAMES = {
    "en":"English","hi":"Hindi","bn":"Bengali","te":"Telugu","mr":"Marathi",
    "ta":"Tamil","gu":"Gujarati","kn":"Kannada","ml":"Malayalam","pa":"Punjabi",
    "or":"Odia","as":"Assamese","ur":"Urdu","mai":"Maithili","sat":"Santali",
    "ks":"Kashmiri","ne":"Nepali","sd":"Sindhi","kok":"Konkani","mni":"Manipuri",
    "bodo":"Bodo","doi":"Dogri",
}

CITY_STATE = {
    "Delhi": "Delhi", "Mumbai": "Maharashtra", "Kolkata": "West Bengal", "Chennai": "Tamil Nadu",
    "Hyderabad": "Telangana", "Pune": "Maharashtra", "Ahmedabad": "Gujarat", "Lucknow": "Uttar Pradesh",
    "Jaipur": "Rajasthan", "Bhopal": "Madhya Pradesh", "Patna": "Bihar", "Nagpur": "Maharashtra",
    "Indore": "Madhya Pradesh", "Surat": "Gujarat", "Kanpur": "Uttar Pradesh", "Coimbatore": "Tamil Nadu",
    "Visakhapatnam": "Andhra Pradesh", "Bhubaneswar": "Odisha", "Guwahati": "Assam", "Amritsar": "Punjab",
    "Vadodara": "Gujarat", "Rajkot": "Gujarat", "Nashik": "Maharashtra", "Aurangabad": "Maharashtra",
    "Varanasi": "Uttar Pradesh", "Agra": "Uttar Pradesh", "Meerut": "Uttar Pradesh", "Prayagraj": "Uttar Pradesh",
    "Ludhiana": "Punjab", "Jalandhar": "Punjab", "Kota": "Rajasthan", "Udaipur": "Rajasthan",
    "Jodhpur": "Rajasthan", "Gwalior": "Madhya Pradesh", "Jabalpur": "Madhya Pradesh", "Cuttack": "Odisha",
    "Siliguri": "West Bengal", "Durgapur": "West Bengal", "Asansol": "West Bengal", "Gaya": "Bihar",
    "Vijayawada": "Andhra Pradesh", "Guntur": "Andhra Pradesh", "Tirupati": "Andhra Pradesh",
    "Warangal": "Telangana", "Nizamabad": "Telangana", "Madurai": "Tamil Nadu", "Salem": "Tamil Nadu",
    "Tiruchirappalli": "Tamil Nadu", "Silchar": "Assam", "Dibrugarh": "Assam",
    "Bengaluru": "Karnataka", "Mysuru": "Karnataka", "Hubli": "Karnataka",
    "Kochi": "Kerala", "Thiruvananthapuram": "Kerala", "Kozhikode": "Kerala",
    "Gurugram": "Haryana", "Faridabad": "Haryana", "Karnal": "Haryana",
    "Raipur": "Chhattisgarh", "Bilaspur": "Chhattisgarh", "Ranchi": "Jharkhand", "Jamshedpur": "Jharkhand",
    "Dehradun": "Uttarakhand", "Haridwar": "Uttarakhand", "Shimla": "Himachal Pradesh", "Panaji": "Goa",
    "Chandigarh": "Chandigarh", "Srinagar": "Jammu and Kashmir", "Jammu": "Jammu and Kashmir",
    "Puducherry": "Puducherry", "Agartala": "Tripura"
}

# ── Cache Bounding ──────────────────────────────────────────────────────────
def _bounded_cache_set(cache_dict: dict, key, value, max_entries: int = 200):
    if key not in cache_dict and len(cache_dict) >= max_entries:
        oldest_key = next(iter(cache_dict))
        cache_dict.pop(oldest_key, None)
    cache_dict[key] = value

# ── Admin Auth Helper ───────────────────────────────────────────────────────
def is_admin_authorized(request):
    if DEBUG_MODE:
        return True
    if not ADMIN_SECRET_KEY:
        return False
    token = request.headers.get("X-Admin-Key") or request.args.get("token")
    return token == ADMIN_SECRET_KEY

import re
import concurrent.futures

# ── Groq API Retry Helper ───────────────────────────────────────────────────
def post_to_groq(body, headers, max_retries=3):
    url = "https://api.groq.com/openai/v1/chat/completions"
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=25)
            if resp.status_code == 429:
                wait_time = (2 ** attempt) + 1
                logger.warning(f"[Groq] Rate limit (429), retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            return resp
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error(f"[Groq] Request failed after {max_retries} attempts: {e}")
                return None
            time.sleep(1)
    return None

# ── Multilingual Translation Engine ─────────────────────────────────────────
TRANSLATE_CHUNK_SIZE = 60
TRANSLATE_MAX_WORKERS = 2
TRANSLATE_STAGGER_SEC = 0.15

def _extract_json_object(raw_text):
    text = re.sub(r"```(?:json)?", "", raw_text).replace("```", "").strip()
    text = (text.replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2018", "'").replace("\u2019", "'"))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = match.group() if match else text
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    pairs = re.findall(r'"((?:[^"\\]|\\.)+?)"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if pairs:
        return {k: v for k, v in pairs}
    return None

def _build_translate_prompt(terms_chunk, lang_name, domain_note, lang_code=""):
    terms_json = json.dumps(terms_chunk, ensure_ascii=False)
    script_note = ""
    if lang_code in ["hi", "mai", "ne", "sd", "doi", "sa"]:
        script_note = "MUST write EXCLUSIVELY in Devanagari script (देवनागरी)."
    elif lang_code in ["bn", "as", "mni"]:
        script_note = "MUST write EXCLUSIVELY in Bengali script (বাংলা)."
    elif lang_code == "pa":
        script_note = "MUST write EXCLUSIVELY in Gurmukhi script (ਗੁਰਮੁਖੀ)."
    elif lang_code == "te":
        script_note = "MUST write EXCLUSIVELY in Telugu script (తెలుగు)."
    elif lang_code == "ta":
        script_note = "MUST write EXCLUSIVELY in Tamil script (தமிழ்)."
    elif lang_code == "gu":
        script_note = "MUST write EXCLUSIVELY in Gujarati script (ગુજરાતી)."
    elif lang_code == "kn":
        script_note = "MUST write EXCLUSIVELY in Kannada script (ಕನ್ನಡ)."
    elif lang_code == "ml":
        script_note = "MUST write EXCLUSIVELY in Malayalam script (മലയാളം)."
    elif lang_code in ["ur", "ks"]:
        script_note = "MUST write EXCLUSIVELY in Urdu/Perso-Arabic script (اردو)."
    elif lang_code == "or":
        script_note = "MUST write EXCLUSIVELY in Odia script (ଓଡ଼ିଆ)."

    return (
        f"You are an expert translator for Indian regional languages. Translate each English term below to {lang_name} ({lang_code}). "
        f"CRITICAL RULES: 1. Target Language: {lang_name} ({lang_code}). {script_note} "
        f"2. Return ONLY a raw JSON object mapping each input term to its {lang_name} translation. No markdown, no backticks, no explanation. "
        f"3. Every single key from the input list MUST appear in the output JSON, exactly as written. "
        f"4. Keep unchanged: chemical/brand names, numbers, and units (kg/ha, Rs, days, ml/L, g/ha, quintal, SL, EC, SC, WP, SG, NPK). "
        f"5. {domain_note} 6. Use natural everyday terms a {lang_name}-speaking farmer would recognize. "
        f"Input terms: {terms_json} Output: a single JSON object only."
    )

def _translate_via_gemini(prompt, sys_prompt, max_tokens=2048):
    if not GEMINI_TRANSLATE_KEY:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TRANSLATE_MODEL}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_TRANSLATE_KEY}
        body = {
            "systemInstruction": {"parts": [{"text": sys_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}
        }
        resp = requests.post(url, headers=headers, json=body, timeout=20)
        if resp.status_code == 200:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return _extract_json_object(text)
    except Exception as e:
        logger.warning(f"[TranslateGemini] error: {e}")
    return None

def _translate_via_groq(prompt, sys_prompt, max_tokens=2048):
    if not GROQ_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "openai/gpt-oss-20b",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"}
    }
    try:
        resp = post_to_groq(body, headers)
        if resp and resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return _extract_json_object(text)
    except Exception as e:
        logger.warning(f"[TranslateGroq] error: {e}")
    return None

def _translate_terms_chunk(terms_chunk, lang_name, domain_note, lang_code=""):
    prompt = _build_translate_prompt(terms_chunk, lang_name, domain_note, lang_code)
    sys_prompt = f"You are an expert Indian regional language translator. You MUST respond with valid JSON only, no other text. Translate everything to {lang_name} ({lang_code}) using its correct native script."
    max_tokens = min(8192, 300 + len(terms_chunk) * 150)
    translations = _translate_via_gemini(prompt, sys_prompt, max_tokens) or _translate_via_groq(prompt, sys_prompt, max_tokens)
    if not translations:
        return {term: term for term in terms_chunk}
    for term in terms_chunk:
        if term not in translations or not translations[term]:
            translations[term] = term
    return translations

def translate_terms(terms, lang_name, domain_note, cache_key, cache_dict, lang_code=""):
    if cache_key in cache_dict:
        return cache_dict[cache_key], True
    chunks = [terms[i:i + TRANSLATE_CHUNK_SIZE] for i in range(0, len(terms), TRANSLATE_CHUNK_SIZE)]
    results = [None] * len(chunks)

    def run_chunk(idx):
        results[idx] = _translate_terms_chunk(chunks[idx], lang_name, domain_note, lang_code)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(TRANSLATE_MAX_WORKERS, len(chunks))) as executor:
        futures = []
        for i in range(len(chunks)):
            if i > 0:
                time.sleep(TRANSLATE_STAGGER_SEC)
            futures.append(executor.submit(run_chunk, i))
        concurrent.futures.wait(futures)

    for i, chunk in enumerate(chunks):
        if results[i] is None:
            results[i] = {term: term for term in chunk}
            continue
        if all(results[i].get(term) == term for term in chunk):
            time.sleep(1.0)
            retried = _translate_terms_chunk(chunk, lang_name, domain_note, lang_code)
            if any(retried.get(term) != term for term in chunk):
                results[i] = retried

    translations = {}
    for r in results:
        translations.update(r)
    if any(translations.get(t) != t for t in terms):
        _bounded_cache_set(cache_dict, cache_key, translations, max_entries=500)
    return translations, False

# ── Usage Tracker ───────────────────────────────────────────────────────────
USAGE_LOG_PATH = os.path.join(basedir, "usage_stats.json")
_USAGE_SAVE_INTERVAL = 30
_usage_stats = None
_usage_lock = threading.Lock()
_last_usage_save = 0.0

_USAGE_FEATURE_LABELS = {
    "index":                       ("Dashboard Page",               "page"),
    "diagnose":                    ("Crop Diagnosis Page",         "page"),
    "market":                      ("Market Page",                 "page"),
    "alerts":                      ("Alerts Page",                 "page"),
    "offline":                     ("Offline Page",                "page"),
    "get_ndvi":                    ("Satellite NDVI",              "api"),
    "get_weather":                 ("Live Weather",                "api"),
    "crop_recommendations":        ("Crop Recommendations",        "api"),
    "get_market_data":             ("Mandi Market Prices",         "api"),
    "debug_market":                ("Market Debug",                "api"),
    "kisan_chat":                  ("Kisan Helper Chat",           "api"),
    "speech_to_text":              ("Voice Input (STT)",           "api"),
    "diagnose_crop":               ("Crop Diagnosis",              "api"),
    "diagnose_log":                ("Diagnosis QA Log",            "api"),
    "diagnose_log_image":          ("Diagnosis QA Image",          "api"),
    "diagnose_log_review":         ("Diagnosis Review",            "api"),
    "diagnose_log_accuracy":       ("Diagnosis Accuracy",          "api"),
    "get_alerts":                  ("Instant Alerts",              "api"),
    "alerts_forecast":             ("6-Day Forecast Alerts",       "api"),
    "monthly_alerts":              ("Monthly Outlook Alerts",      "api"),
    "seasonal_alerts":             ("Seasonal Advisories",         "api"),
    "crop_risk":                   ("Crop Risk / Harvest Window",  "api"),
    "translate_market":            ("Market Translation",          "api"),
    "clear_translation_cache":     ("Translation Cache Clear",     "api"),
    "translate_alerts":            ("Alerts Translation",          "api"),
    "translate_dashboard":         ("Dashboard Translation",       "api"),
    "translate_diagnose":          ("Diagnose Translation",        "api"),
    "translate_diagnosis_result":  ("Diagnosis Result Translation","api"),
}

def load_usage_stats():
    global _usage_stats
    if _usage_stats is not None:
        return _usage_stats
    try:
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
            _usage_stats = json.load(f)
    except Exception:
        _usage_stats = {}
    _usage_stats.setdefault("since", datetime.now().isoformat(timespec="seconds"))
    _usage_stats.setdefault("features", {})
    _usage_stats.setdefault("daily", {})
    return _usage_stats

def save_usage_stats():
    if _usage_stats is None:
        return
    try:
        tmp_path = USAGE_LOG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_usage_stats, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, USAGE_LOG_PATH)
    except Exception as e:
        logger.warning(f"[Usage] Could not persist usage stats: {e}")

def flush_usage_stats():
    global _last_usage_save
    with _usage_lock:
        load_usage_stats()
        save_usage_stats()
        _last_usage_save = time.monotonic()

def track_usage(endpoint, label=None, kind="api"):
    global _last_usage_save
    now = datetime.now()
    with _usage_lock:
        stats = load_usage_stats()
        feats = stats["features"]
        rec = feats.setdefault(endpoint, {
            "label": label or endpoint, "kind": kind, "count": 0,
        })
        rec["count"] += 1
        rec["last_used"] = now.isoformat(timespec="seconds")
        today = now.strftime("%Y-%m-%d")
        stats["daily"][today] = stats["daily"].get(today, 0) + 1
        if time.monotonic() - _last_usage_save >= _USAGE_SAVE_INTERVAL:
            _last_usage_save = time.monotonic()
            save_usage_stats()
