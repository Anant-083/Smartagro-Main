import os
import re
import sys
import time
import json
import uuid
import socket
import logging
import threading
import concurrent.futures
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
except Exception:
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
# The "or os.getenv('GEMINI_API_KEY')" part is only a safety net while you switch over.
# Delete it once both new keys work, so a missing key can never silently share the old one.
GEMINI_DIAGNOSE_KEY  = os.getenv("GEMINI_API_KEY_DIAGNOSE", "") or os.getenv("GEMINI_API_KEY", "")
GEMINI_TRANSLATE_KEY = os.getenv("GEMINI_API_KEY_TRANSLATE", "") or os.getenv("GEMINI_API_KEY", "")
GEMINI_TRANSLATE_MODEL = os.getenv("GEMINI_TRANSLATE_MODEL", "gemini-3.5-flash")
GEMINI_DIAGNOSIS_MODEL = os.getenv("GEMINI_DIAGNOSIS_MODEL", "gemini-3.6-flash")
GEMMA_DIAGNOSE_KEY = os.getenv("GEMMA_API_KEY_DIAGNOSE", "") or os.getenv("GEMMA_API_KEY", "")
GEMMA_DIAGNOSIS_MODEL = os.getenv("GEMMA_DIAGNOSIS_MODEL", "gemma-3-27b-it")
NINJA_API_KEY       = os.getenv("NINJA_API_KEY", "")
DATA_GOV_API_KEY    = os.getenv("DATA_GOV_API_KEY", "")
DEBUG_MODE          = os.getenv("FLASK_DEBUG", "0") == "1"
ADMIN_SECRET_KEY    = os.getenv("ADMIN_SECRET_KEY", "")

LANG_NAMES = {
    "en":"English","hi":"Hindi","bn":"Bengali","te":"Telugu","mr":"Marathi",
    "ta":"Tamil","gu":"Gujarati","kn":"Kannada","ml":"Malayalam","pa":"Punjabi",
    "or":"Odia","as":"Assamese","ur":"Urdu","mai":"Maithili","sat":"Santali",
    "ks":"Kashmiri","ne":"Nepali","sd":"Sindhi","kok":"Konkani","mni":"Manipuri",
    "bodo":"Bodo","doi":"Dogri","sa":"Sanskrit",
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

# ═══════════════════════════════════════════════════════════════════════════
# Multilingual Translation Engine
#
# Order of work for every fixed-text request:
#   1. memory cache          (instant, lost on restart)
#   2. saved file on disk    translations/<lang>.json   (instant, survives restarts,
#                            commit these files so Render has them too)
#   3. Gemini (translate key) -> Groq fallback, ONLY for terms not saved yet
# Only terms the model really answered are saved, so a rate-limit failure can
# never get stored as "English forever".
# ═══════════════════════════════════════════════════════════════════════════
TRANSLATE_CHUNK_SIZE = 60
TRANSLATE_MAX_WORKERS = 2
TRANSLATE_STAGGER_SEC = 0.15
GROQ_TRANSLATE_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]

TRANSLATIONS_DIR = os.path.join(basedir, "translations")
os.makedirs(TRANSLATIONS_DIR, exist_ok=True)
_lang_file_lock = threading.Lock()
_lang_file_memo = {}

# After a Gemini 429 we stop calling Gemini for translation for a short while
# and use Groq instead, so a per-minute limit never turns into a wall of errors.
_gemini_translate_paused_until = 0.0
_gemini_translate_lock = threading.Lock()
GEMINI_TRANSLATE_PAUSE_SEC = 90


def _lang_file_path(lang_code):
    safe = re.sub(r"[^a-z]", "", (lang_code or "").lower())
    return os.path.join(TRANSLATIONS_DIR, f"{safe}.json") if safe else None


def _read_lang_file_unlocked(lang_code):
    path = _lang_file_path(lang_code)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_saved_translations(lang_code):
    """Return a copy of the saved English -> translation map for a language."""
    if not lang_code:
        return {}
    with _lang_file_lock:
        if lang_code not in _lang_file_memo:
            _lang_file_memo[lang_code] = _read_lang_file_unlocked(lang_code)
        return dict(_lang_file_memo[lang_code])


def save_new_translations(lang_code, new_pairs):
    """Merge new term translations into translations/<lang>.json (atomic write)."""
    path = _lang_file_path(lang_code)
    if not path or not new_pairs:
        return
    with _lang_file_lock:
        data = _lang_file_memo.get(lang_code)
        if data is None:
            data = _read_lang_file_unlocked(lang_code)
        data.update(new_pairs)
        _lang_file_memo[lang_code] = data
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
            logger.info(f"[Translate] saved {len(new_pairs)} new terms to {os.path.basename(path)} (total {len(data)})")
        except Exception as e:
            logger.warning(f"[Translate] could not save {path}: {e}")


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


_SCRIPT_NOTES = {
    "Devanagari script (देवनागरी)": ["hi", "mr", "mai", "ne", "doi", "sa", "kok", "bodo"],
    "Bengali script (বাংলা)": ["bn", "as"],
    "Gurmukhi script (ਗੁਰਮੁਖੀ)": ["pa"],
    "Telugu script (తెలుగు)": ["te"],
    "Tamil script (தமிழ்)": ["ta"],
    "Gujarati script (ગુજરાતી)": ["gu"],
    "Kannada script (ಕನ್ನಡ)": ["kn"],
    "Malayalam script (മലയാളം)": ["ml"],
    "Urdu/Perso-Arabic script (اردو)": ["ur", "ks", "sd"],
    "Odia script (ଓଡ଼ିଆ)": ["or"],
    "Ol Chiki script (ᱚᱞ ᱪᱤᱠᱤ)": ["sat"],
    "Meitei Mayek script (ꯃꯩꯇꯩ ꯃꯌꯦꯛ)": ["mni"],
}


def _build_translate_prompt(terms_chunk, lang_name, domain_note, lang_code=""):
    terms_json = json.dumps(terms_chunk, ensure_ascii=False)
    script_note = ""
    for script, codes in _SCRIPT_NOTES.items():
        if lang_code in codes:
            script_note = f"MUST write EXCLUSIVELY in {script}."
            break

    return (
        f"You are an expert translator for Indian regional languages. Translate each English term below to {lang_name} ({lang_code}). "
        f"CRITICAL RULES: 1. Target Language: {lang_name} ({lang_code}). {script_note} "
        f"2. Return ONLY a raw JSON object mapping each input term to its {lang_name} translation. No markdown, no backticks, no explanation. "
        f"3. Every single key from the input list MUST appear in the output JSON, exactly as written. "
        f"4. Keep unchanged: chemical/brand names, numbers, emojis, symbols and units (kg/ha, Rs, days, ml/L, g/ha, quintal, SL, EC, SC, WP, SG, NPK). "
        f"5. {domain_note} 6. Use natural everyday terms a {lang_name}-speaking farmer would recognize. "
        f"Input terms: {terms_json} Output: a single JSON object only."
    )


def _translate_via_gemini(prompt, sys_prompt, max_tokens=2048):
    """Gemini translation with its own key. Returns a dict, or None on any failure.
    A 429 pauses Gemini translation for GEMINI_TRANSLATE_PAUSE_SEC seconds."""
    global _gemini_translate_paused_until
    if not GEMINI_TRANSLATE_KEY:
        return None
    with _gemini_translate_lock:
        if time.time() < _gemini_translate_paused_until:
            return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TRANSLATE_MODEL}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_TRANSLATE_KEY}
        body = {
            "systemInstruction": {"parts": [{"text": sys_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        if resp.status_code == 429:
            with _gemini_translate_lock:
                _gemini_translate_paused_until = time.time() + GEMINI_TRANSLATE_PAUSE_SEC
            logger.warning(f"[TranslateGemini] 429 - pausing Gemini translation for {GEMINI_TRANSLATE_PAUSE_SEC}s, using Groq")
            return None
        if resp.status_code != 200:
            logger.warning(f"[TranslateGemini] HTTP {resp.status_code}: {resp.text[:150]}")
            return None
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return _extract_json_object(text)
    except Exception as e:
        logger.warning(f"[TranslateGemini] error: {e}")
    return None


def _translate_via_groq(prompt, sys_prompt, max_tokens=2048):
    if not GROQ_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    for model in GROQ_TRANSLATE_MODELS:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = post_to_groq(body, headers)
            if resp is not None and resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"].strip()
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                parsed = _extract_json_object(text)
                if parsed:
                    return parsed
        except Exception as e:
            logger.warning(f"[TranslateGroq] {model} error: {e}")
    return None


def _translate_terms_chunk_full(terms_chunk, lang_name, domain_note, lang_code=""):
    """Translate one chunk. Returns (translations, returned) where `returned` is the
    set of terms the model genuinely answered (safe to save). Everything else falls
    back to the English term so the page still renders."""
    prompt = _build_translate_prompt(terms_chunk, lang_name, domain_note, lang_code)
    sys_prompt = (f"You are an expert Indian regional language translator. You MUST respond with valid JSON only, "
                  f"no other text. Translate everything to {lang_name} ({lang_code}) using its correct native script.")
    max_tokens = min(8192, 300 + len(terms_chunk) * 150)
    raw = _translate_via_gemini(prompt, sys_prompt, max_tokens) or _translate_via_groq(prompt, sys_prompt, max_tokens)
    translations, returned = {}, set()
    for term in terms_chunk:
        val = raw.get(term) if isinstance(raw, dict) else None
        if isinstance(val, str) and val.strip():
            translations[term] = val
            returned.add(term)
        else:
            translations[term] = term
    return translations, returned


def _translate_terms_chunk(terms_chunk, lang_name, domain_note, lang_code=""):
    """Backwards-compatible wrapper: returns only the translations dict."""
    return _translate_terms_chunk_full(terms_chunk, lang_name, domain_note, lang_code)[0]


def translate_terms(terms, lang_name, domain_note, cache_key, cache_dict, lang_code=""):
    """Returns (translations, cached).
    Fixed page text (cache_key is just the language code) is read from / saved to
    translations/<lang>.json. Dynamic text (cache_key contains '::', e.g. a diagnosis
    result) is never written to disk."""
    if cache_key in cache_dict:
        return cache_dict[cache_key], True

    terms = list(dict.fromkeys(t for t in terms if isinstance(t, str) and t.strip()))
    persist = bool(lang_code) and "::" not in str(cache_key)
    saved = load_saved_translations(lang_code) if persist else {}

    translations, missing = {}, []
    for term in terms:
        if term in saved and saved[term]:
            translations[term] = saved[term]
        else:
            missing.append(term)

    if not missing:
        _bounded_cache_set(cache_dict, cache_key, translations, max_entries=500)
        return translations, True

    chunks = [missing[i:i + TRANSLATE_CHUNK_SIZE] for i in range(0, len(missing), TRANSLATE_CHUNK_SIZE)]
    results = [None] * len(chunks)

    def run_chunk(idx):
        results[idx] = _translate_terms_chunk_full(chunks[idx], lang_name, domain_note, lang_code)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(TRANSLATE_MAX_WORKERS, len(chunks))) as executor:
        futures = []
        for i in range(len(chunks)):
            if i > 0:
                time.sleep(TRANSLATE_STAGGER_SEC)
            futures.append(executor.submit(run_chunk, i))
        concurrent.futures.wait(futures)

    to_save, all_returned = {}, set()
    for i, chunk in enumerate(chunks):
        res = results[i] or ({t: t for t in chunk}, set())
        trans, returned = res
        if not returned:
            # whole chunk failed - one patient retry (Gemini may be paused, Groq takes over)
            time.sleep(1.0)
            trans2, returned2 = _translate_terms_chunk_full(chunk, lang_name, domain_note, lang_code)
            if returned2:
                trans, returned = trans2, returned2
        translations.update(trans)
        all_returned |= returned
        for t in returned:
            to_save[t] = trans[t]

    if persist and to_save:
        save_new_translations(lang_code, to_save)

    # Only keep it in memory if every term got a real answer; otherwise the next
    # request retries just the missing ones (saved ones are already on disk).
    if all(t in all_returned for t in missing):
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


def usage_label_for(endpoint):
    """Blueprint endpoints look like 'market.get_market_data'. Use this in app.py's
    before_request instead of _USAGE_FEATURE_LABELS.get(ep) so the Usage page keeps
    its friendly names after the split."""
    key = (endpoint or "").split(".")[-1]
    return key, _USAGE_FEATURE_LABELS.get(key, (None, "api"))


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